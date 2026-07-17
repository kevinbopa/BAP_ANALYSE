from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any
import zlib

from spe_ingestion.clients.theoddsapi import TheOddsApiClient
from spe_ingestion.config import TheOddsApiSettings
from spe_ingestion.odds_matching import LEAGUE_NAME_ALIASES, canonical_label, compare_team_names
from spe_ingestion.payload_pipeline import record_payload_normalization, store_provider_payload
from spe_ingestion.run_journal import run_metadata


# Nom de ligue TheSportsDB (core.leagues.league_name) -> sport key The Odds API.
# Les ligues sans cle ici (Tchequie, Croatie, Ukraine, Serbie, Roumanie,
# Hongrie) n'ont pas de cotes chez The Odds API : elles nourrissent quand
# meme l'Elo, les correlations et le ML.
SPORT_KEY_BY_LEAGUE_NAME: dict[str, str] = {
    # Big 5 + international
    "English Premier League": "soccer_epl",
    "Spanish La Liga": "soccer_spain_la_liga",
    "German Bundesliga": "soccer_germany_bundesliga",
    "Italian Serie A": "soccer_italy_serie_a",
    "French Ligue 1": "soccer_france_ligue_one",
    "FIFA World Cup": "soccer_fifa_world_cup",
    "UEFA European Championships": "soccer_uefa_european_championship",
    "UEFA Nations League": "soccer_uefa_nations_league",
    "World Cup Qualifying UEFA": "soccer_fifa_world_cup_qualifiers_europe",
    # Championnats europeens
    "English League Championship": "soccer_efl_champ",
    "Dutch Eredivisie": "soccer_netherlands_eredivisie",
    "Portuguese Primeira Liga": "soccer_portugal_primeira_liga",
    "Belgian Pro League": "soccer_belgium_first_div",
    "Scottish Premier League": "soccer_spl",
    "Turkish Super Lig": "soccer_turkey_super_league",
    # Ameriques
    "American Major League Soccer": "soccer_usa_mls",
    "Mexican Primera League": "soccer_mexico_ligamx",
    "Brazilian Serie A": "soccer_brazil_campeonato",
    "Argentinian Primera Division": "soccer_argentina_primera_division",
    # Asie / Oceanie
    "Japanese J1 League": "soccer_japan_j_league",
    "South Korean K League 1": "soccer_korea_kleague1",
    "Saudi-Arabian Pro League": "soccer_saudi_arabia_pro_league",
    "Chinese Super League": "soccer_china_superleague",
    "Australian A-League": "soccer_australia_aleague",
    "Austrian Bundesliga": "soccer_austria_bundesliga",
    "Swiss Super League": "soccer_switzerland_superleague",
    "Danish Superliga": "soccer_denmark_superliga",
    "Norwegian Eliteserien": "soccer_norway_eliteserien",
    "Swedish Allsvenskan": "soccer_sweden_allsvenskan",
    "Greek Super League 1": "soccer_greece_super_league",
    "Polish Ekstraklasa": "soccer_poland_ekstraklasa",
    "Russian Football Premier League": "soccer_russia_premier_league",
    "Finnish Veikkausliiga": "soccer_finland_veikkausliiga",
    "Irish Premier Division": "soccer_league_of_ireland",
    # Coupes d'Europe de clubs
    "UEFA Champions League": "soccer_uefa_champs_league",
    "UEFA Europa League": "soccer_uefa_europa_league",
    "UEFA Conference League": "soccer_uefa_europa_conference_league",
}

# Sport keys SUPPLEMENTAIRES par ligue : The Odds API separe certains tours
# (les qualifications CL de juillet-aout vivent sous leur propre cle alors
# que TheSportsDB les range dans la saison UEFA Champions League).
EXTRA_SPORT_KEYS_BY_LEAGUE_NAME: dict[str, tuple[str, ...]] = {
    "UEFA Champions League": ("soccer_uefa_champs_league_qualification",),
}


@dataclass(frozen=True)
class TheOddsApiIngestionSummary:
    sports_requested: int = 0
    events_received: int = 0
    fixtures_matched: int = 0
    raw_payloads: int = 0
    odds_written: int = 0
    fixtures_without_1x2: int = 0
    totals_odds_written: int = 0
    btts_odds_written: int = 0
    market_odds_written: int = 0


@dataclass(frozen=True)
class DbFixtureCandidate:
    fixture_id: int
    league_name: str
    home_team_name: str
    away_team_name: str
    kickoff_utc: datetime | None


@dataclass(frozen=True)
class ExtractedOddsRow:
    bookmaker_code: str
    bookmaker_name: str
    captured_at: datetime
    home_odd: float
    draw_odd: float
    away_odd: float
    source_reference: str


@dataclass(frozen=True)
class ExtractedTotalsRow:
    bookmaker_code: str
    bookmaker_name: str
    captured_at: datetime
    total_line: float
    over_odd: float
    under_odd: float
    source_reference: str


@dataclass(frozen=True)
class ExtractedBttsRow:
    bookmaker_code: str
    bookmaker_name: str
    captured_at: datetime
    yes_odd: float
    no_odd: float
    source_reference: str


@dataclass(frozen=True)
class ExtractedMarketRow:
    """Ligne generique two-way : handicap (SPREAD), double chance, DNB."""
    bookmaker_code: str
    bookmaker_name: str
    captured_at: datetime
    market_code: str      # SPREAD | DOUBLE_CHANCE | DNB
    line: float           # handicap ; 0 si sans ligne
    selection_code: str   # HOME | AWAY | DC_1X | DC_12 | DC_X2
    decimal_odd: float
    source_reference: str


class TheOddsApiOddsIngestor:
    def __init__(self, client: TheOddsApiClient, settings: TheOddsApiSettings) -> None:
        self._client = client
        self._settings = settings
        self._allowed_bookmakers = {
            item.strip().lower()
            for item in settings.bookmakers.split(",")
            if item.strip()
        }

    def ingest_upcoming_1x2(self, connection) -> TheOddsApiIngestionSummary:
        summary = TheOddsApiIngestionSummary()
        self._btts_calls_made = 0
        with connection.cursor() as cursor:
            provider_id = self._fetch_provider_id(cursor)
            endpoint_id = self._fetch_endpoint_id(cursor, "CURRENT_ODDS_H2H")
            run_id = self._start_run(cursor, provider_id, endpoint_id)
            fixture_index = self._load_db_fixtures(cursor)

            commence_from = datetime.now(UTC) - timedelta(hours=self._settings.lookback_hours)
            commence_to = datetime.now(UTC) + timedelta(days=self._settings.lookahead_days)

            # Mode "auto" : ne requeter que les sports ayant des matchs a
            # venir en base — une ligue en intersaison ne coute aucun credit.
            # La sync TheSportsDB precede la sync cotes dans le cycle, donc
            # les calendriers sont frais.
            sport_keys = tuple(self._settings.sport_keys)
            if sport_keys == ("auto",):
                sport_keys = tuple(
                    sorted(key for key, candidates in fixture_index.items() if candidates)
                )

            try:
                for sport_key in sport_keys:
                    summary = replace(summary, sports_requested=summary.sports_requested + 1)

                    db_candidates = fixture_index.get(sport_key, [])

                    events = self._client.get_odds(
                        sport_key,
                        commence_from=commence_from,
                        commence_to=commence_to,
                    )
                    for event in events:
                        summary = replace(summary, events_received=summary.events_received + 1)

                        event_id = str(event.get("id") or "").strip()
                        if not event_id:
                            continue

                        payload_id = self._store_raw_payload(
                            cursor=cursor,
                            provider_id=provider_id,
                            endpoint_id=endpoint_id,
                            ingestion_run_id=run_id,
                            object_type="ODDS_EVENT",
                            object_id=event_id,
                            natural_key=f"theoddsapi:event:{sport_key}:{event_id}",
                            payload=event,
                            request_path=f"/sports/{sport_key}/odds",
                            request_params={
                                "sport_key": sport_key,
                                "event_id": event_id,
                                "regions": self._settings.regions,
                                "markets": self._settings.markets,
                            },
                        )
                        summary = replace(summary, raw_payloads=summary.raw_payloads + 1)

                        matched_fixture = self._match_fixture(db_candidates, event)
                        if matched_fixture is None:
                            matched_fixture = self._ensure_fixture_from_event(cursor, sport_key, event)
                            if matched_fixture is not None:
                                db_candidates.append(matched_fixture)
                        if matched_fixture is None:
                            record_payload_normalization(
                                cursor,
                                provider_payload_id=payload_id,
                                ingestion_run_id=run_id,
                                normalization_target="core.fixture_market_bundle",
                                status_code="SKIPPED",
                                error_message="No matching fixture found for The Odds API event",
                            )
                            continue
                        summary = replace(summary, fixtures_matched=summary.fixtures_matched + 1)
                        event_records_written = 0

                        # Marches derives : totals (toutes lignes) et BTTS.
                        # Ecrits AVANT le bloc 1X2 car son "continue" (aucune
                        # cote 1X2) ne doit pas faire perdre ces marches.
                        for totals_row in extract_totals_rows(
                            event=event,
                            allowed_bookmakers=self._allowed_bookmakers or None,
                        ):
                            bookmaker_id = self._ensure_bookmaker(
                                cursor,
                                bookmaker_code=totals_row.bookmaker_code,
                                bookmaker_name=totals_row.bookmaker_name,
                            )
                            self._upsert_fixture_odds_totals(
                                cursor=cursor,
                                fixture_id=matched_fixture.fixture_id,
                                bookmaker_id=bookmaker_id,
                                row=totals_row,
                            )
                            event_records_written += 1
                            summary = replace(
                                summary,
                                totals_odds_written=summary.totals_odds_written + 1,
                            )
                        # BTTS : absent du bulk /odds (marche non "featured"),
                        # servi uniquement par l'endpoint par evenement. On ne
                        # paie ce quota que pour les matchs proches du kickoff.
                        btts_event = self._fetch_btts_event(sport_key, event)
                        for btts_row in extract_btts_rows(
                            event=btts_event,
                            allowed_bookmakers=self._allowed_bookmakers or None,
                        ):
                            bookmaker_id = self._ensure_bookmaker(
                                cursor,
                                bookmaker_code=btts_row.bookmaker_code,
                                bookmaker_name=btts_row.bookmaker_name,
                            )
                            self._upsert_fixture_odds_btts(
                                cursor=cursor,
                                fixture_id=matched_fixture.fixture_id,
                                bookmaker_id=bookmaker_id,
                                row=btts_row,
                            )
                            event_records_written += 1
                            summary = replace(
                                summary,
                                btts_odds_written=summary.btts_odds_written + 1,
                            )
                        # Meme payload event : lignes O/U alternatives (0.5/1.5/3.5)
                        # -> reutilisent fixture_odds_totals (colonne total_line).
                        for alt_totals_row in extract_totals_rows(
                            event=btts_event,
                            allowed_bookmakers=self._allowed_bookmakers or None,
                        ):
                            bookmaker_id = self._ensure_bookmaker(
                                cursor,
                                bookmaker_code=alt_totals_row.bookmaker_code,
                                bookmaker_name=alt_totals_row.bookmaker_name,
                            )
                            self._upsert_fixture_odds_totals(
                                cursor=cursor,
                                fixture_id=matched_fixture.fixture_id,
                                bookmaker_id=bookmaker_id,
                                row=alt_totals_row,
                            )
                            event_records_written += 1
                            summary = replace(
                                summary,
                                totals_odds_written=summary.totals_odds_written + 1,
                            )
                        # Handicap (spreads), double chance, DNB -> table generique.
                        for market_row in extract_market_rows(
                            event=btts_event,
                            allowed_bookmakers=self._allowed_bookmakers or None,
                        ):
                            bookmaker_id = self._ensure_bookmaker(
                                cursor,
                                bookmaker_code=market_row.bookmaker_code,
                                bookmaker_name=market_row.bookmaker_name,
                            )
                            self._upsert_fixture_odds_market(
                                cursor=cursor,
                                fixture_id=matched_fixture.fixture_id,
                                bookmaker_id=bookmaker_id,
                                row=market_row,
                            )
                            event_records_written += 1
                            summary = replace(
                                summary,
                                market_odds_written=summary.market_odds_written + 1,
                            )

                        rows = extract_1x2_rows(
                            event=event,
                            home_team_name=matched_fixture.home_team_name,
                            away_team_name=matched_fixture.away_team_name,
                            allowed_bookmakers=self._allowed_bookmakers or None,
                        )
                        if not rows:
                            record_payload_normalization(
                                cursor,
                                provider_payload_id=payload_id,
                                ingestion_run_id=run_id,
                                normalization_target="core.fixture_market_bundle",
                                status_code="PARTIAL_SUCCESS" if event_records_written > 0 else "SKIPPED",
                                records_written=event_records_written,
                                error_message=(
                                    "No 1X2 rows found in primary event payload"
                                    if event_records_written > 0
                                    else "No supported odds extracted from event payload"
                                ),
                                metadata={"fixture_id": matched_fixture.fixture_id},
                            )
                            summary = replace(
                                summary,
                                fixtures_without_1x2=summary.fixtures_without_1x2 + 1,
                            )
                            continue

                        for row in rows:
                            # Garde anti-cote invalide : un book peut envoyer
                            # une cote <= 1.0 (placeholder / marche gele). La
                            # laisser passer viole la contrainte SQL et AVORTE
                            # toute la transaction -> plus aucune mise a jour.
                            # On saute la ligne pourrie, le run continue.
                            if any(
                                odd is None or float(odd) <= 1.0
                                for odd in (row.home_odd, row.draw_odd, row.away_odd)
                            ):
                                continue
                            bookmaker_id = self._ensure_bookmaker(
                                cursor,
                                bookmaker_code=row.bookmaker_code,
                                bookmaker_name=row.bookmaker_name,
                            )
                            self._upsert_fixture_odds(
                                cursor=cursor,
                                fixture_id=matched_fixture.fixture_id,
                                bookmaker_id=bookmaker_id,
                                row=row,
                            )
                            event_records_written += 1
                            summary = replace(summary, odds_written=summary.odds_written + 1)
                        record_payload_normalization(
                            cursor,
                            provider_payload_id=payload_id,
                            ingestion_run_id=run_id,
                            normalization_target="core.fixture_market_bundle",
                            status_code="SUCCESS",
                            records_written=event_records_written,
                            metadata={"fixture_id": matched_fixture.fixture_id, "sport_key": sport_key},
                        )

                self._finish_run(cursor, run_id, summary)
                connection.commit()
                return summary
            except Exception as exc:
                self._fail_run(cursor, run_id, summary, str(exc))
                connection.commit()
                raise

    def _fetch_provider_id(self, cursor) -> int:
        cursor.execute("SELECT provider_id FROM ops.providers WHERE provider_code = 'THEODDSAPI'")
        row = cursor.fetchone()
        if not row:
            raise RuntimeError("THEODDSAPI provider is missing from ops.providers")
        return int(row[0])

    def _fetch_endpoint_id(self, cursor, endpoint_code: str) -> int:
        cursor.execute(
            """
            SELECT e.endpoint_id
            FROM ops.provider_endpoints e
            JOIN ops.providers p ON p.provider_id = e.provider_id
            WHERE p.provider_code = 'THEODDSAPI'
              AND e.endpoint_code = %s
            """,
            (endpoint_code,),
        )
        row = cursor.fetchone()
        if not row:
            raise RuntimeError(f"Missing The Odds API endpoint metadata for {endpoint_code}")
        return int(row[0])

    def _start_run(self, cursor, provider_id: int, endpoint_id: int) -> str:
        meta = run_metadata(
            {
                "sport_keys": list(self._settings.sport_keys),
                "regions": self._settings.regions,
                "markets": self._settings.markets,
                "odds_format": self._settings.odds_format,
                "date_format": self._settings.date_format,
            },
            default_trigger="MANUAL",
        )
        cursor.execute(
            """
            INSERT INTO ops.ingestion_runs (
                provider_id,
                endpoint_id,
                run_scope,
                request_params,
                started_at,
                heartbeat_at,
                status_code,
                request_fingerprint,
                application_name,
                trigger_source,
                host_name,
                process_id
            )
            VALUES (%s, %s, %s, %s::jsonb, now(), now(), 'RUNNING', %s, %s, %s, %s, %s)
            RETURNING ingestion_run_id
            """,
            (
                provider_id,
                endpoint_id,
                "theoddsapi_upcoming_1x2",
                meta["request_params_json"],
                meta["request_fingerprint"],
                meta["application_name"],
                meta["trigger_source"],
                meta["host_name"],
                meta["process_id"],
            ),
        )
        return str(cursor.fetchone()[0])

    def _finish_run(self, cursor, run_id: str, summary: TheOddsApiIngestionSummary) -> None:
        cursor.execute(
            """
            UPDATE ops.ingestion_runs
            SET finished_at = now(),
                heartbeat_at = now(),
                status_code = 'SUCCESS',
                records_received = %s,
                records_written = %s
            WHERE ingestion_run_id = %s
            """,
            (
                summary.raw_payloads,
                summary.odds_written,
                run_id,
            ),
        )

    def _fail_run(self, cursor, run_id: str, summary: TheOddsApiIngestionSummary, error_message: str) -> None:
        cursor.execute(
            """
            UPDATE ops.ingestion_runs
            SET finished_at = now(),
                heartbeat_at = now(),
                status_code = 'FAILED',
                records_received = %s,
                records_written = %s,
                error_message = %s
            WHERE ingestion_run_id = %s
            """,
            (
                summary.raw_payloads,
                summary.odds_written,
                error_message[:1000],
                run_id,
            ),
        )

    def _load_db_fixtures(self, cursor) -> dict[str, list[DbFixtureCandidate]]:
        commence_from = datetime.now(UTC) - timedelta(hours=self._settings.lookback_hours)
        commence_to = datetime.now(UTC) + timedelta(days=self._settings.lookahead_days)
        cursor.execute(
            """
            SELECT
                f.fixture_id,
                l.league_name,
                home.team_name,
                away.team_name,
                f.kickoff_utc
            FROM core.fixtures f
            JOIN core.leagues l ON l.league_id = f.league_id
            JOIN core.teams home ON home.team_id = f.home_team_id
            JOIN core.teams away ON away.team_id = f.away_team_id
            WHERE f.kickoff_utc IS NOT NULL
              AND f.kickoff_utc >= %s
              AND f.kickoff_utc <= %s
            ORDER BY l.league_name, f.kickoff_utc, f.fixture_id
            """,
            (commence_from, commence_to),
        )

        grouped: dict[str, list[DbFixtureCandidate]] = {}
        for fixture_id, league_name, home_team_name, away_team_name, kickoff_utc in cursor.fetchall():
            sport_key = self._resolve_sport_key_from_league(str(league_name))
            if sport_key is None:
                continue
            candidate = DbFixtureCandidate(
                fixture_id=int(fixture_id),
                league_name=str(league_name),
                home_team_name=str(home_team_name),
                away_team_name=str(away_team_name),
                kickoff_utc=kickoff_utc,
            )
            grouped.setdefault(sport_key, []).append(candidate)
            # Les tours separes chez The Odds API (ex: qualifs CL) voient
            # les memes candidats : le matching par equipes fait le tri.
            for extra_key in EXTRA_SPORT_KEYS_BY_LEAGUE_NAME.get(str(league_name), ()):
                grouped.setdefault(extra_key, []).append(candidate)
        return grouped

    def _resolve_sport_key_from_league(self, league_name: str) -> str | None:
        direct = SPORT_KEY_BY_LEAGUE_NAME.get(league_name)
        if direct:
            return direct

        league_label = canonical_label(league_name)
        for known_league, sport_key in SPORT_KEY_BY_LEAGUE_NAME.items():
            aliases = {canonical_label(known_league)}
            aliases.update(canonical_label(alias) for alias in LEAGUE_NAME_ALIASES.get(known_league, ()))
            if league_label in aliases:
                return sport_key
        return None

    def _match_fixture(self, candidates: list[DbFixtureCandidate], event: dict[str, Any]) -> DbFixtureCandidate | None:
        home_team = str(event.get("home_team") or "")
        away_team = str(event.get("away_team") or "")
        kickoff_utc = parse_iso_datetime(event.get("commence_time"))
        if not home_team or not away_team or kickoff_utc is None:
            return None

        best_candidate: DbFixtureCandidate | None = None
        best_score = -1
        for candidate in candidates:
            if candidate.kickoff_utc is None:
                continue
            delta_minutes = abs((candidate.kickoff_utc - kickoff_utc).total_seconds()) / 60.0
            if delta_minutes > self._settings.match_window_minutes:
                continue
            home_score = compare_team_names(candidate.home_team_name, home_team)
            away_score = compare_team_names(candidate.away_team_name, away_team)
            if home_score == 0 or away_score == 0:
                continue
            total_score = int(home_score + away_score)
            if total_score > best_score:
                best_candidate = candidate
                best_score = total_score
        return best_candidate

    def _ensure_fixture_from_event(
        self,
        cursor,
        sport_key: str,
        event: dict[str, Any],
    ) -> DbFixtureCandidate | None:
        league_name = league_name_for_sport_key(sport_key)
        if league_name is None:
            return None

        home_team_name = str(event.get("home_team") or "").strip()
        away_team_name = str(event.get("away_team") or "").strip()
        kickoff_utc = parse_iso_datetime(event.get("commence_time"))
        event_id = str(event.get("id") or "").strip()
        if not home_team_name or not away_team_name or kickoff_utc is None or not event_id:
            return None

        league_id = self._upsert_synthetic_league(
            cursor=cursor,
            league_name=league_name,
            sport_key=sport_key,
            season_name=str(kickoff_utc.year),
        )
        season_id = self._upsert_synthetic_season(
            cursor=cursor,
            league_id=league_id,
            season_name=str(kickoff_utc.year),
        )
        home_team_id = self._upsert_synthetic_team(cursor, home_team_name)
        away_team_id = self._upsert_synthetic_team(cursor, away_team_name)

        cursor.execute(
            """
            INSERT INTO core.fixtures (
                thesportsdb_event_id,
                league_id,
                season_id,
                home_team_id,
                away_team_id,
                event_name,
                kickoff_utc,
                event_date_utc,
                event_time_utc,
                status_code,
                status_text,
                country_name,
                raw_last_snapshot_at,
                last_synced_at
            )
            VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, 'SCHEDULED', 'SCHEDULED', 'World', now(), now()
            )
            ON CONFLICT (thesportsdb_event_id) DO UPDATE
            SET league_id = EXCLUDED.league_id,
                season_id = EXCLUDED.season_id,
                home_team_id = EXCLUDED.home_team_id,
                away_team_id = EXCLUDED.away_team_id,
                event_name = EXCLUDED.event_name,
                kickoff_utc = EXCLUDED.kickoff_utc,
                event_date_utc = EXCLUDED.event_date_utc,
                event_time_utc = EXCLUDED.event_time_utc,
                status_code = EXCLUDED.status_code,
                status_text = EXCLUDED.status_text,
                country_name = EXCLUDED.country_name,
                raw_last_snapshot_at = now(),
                last_synced_at = now()
            RETURNING fixture_id
            """,
            (
                synthetic_bigint(f"fixture:{sport_key}:{event_id}"),
                league_id,
                season_id,
                home_team_id,
                away_team_id,
                f"{home_team_name} vs {away_team_name}",
                kickoff_utc,
                kickoff_utc.date(),
                kickoff_utc.time().replace(tzinfo=None),
            ),
        )
        fixture_id = int(cursor.fetchone()[0])

        return DbFixtureCandidate(
            fixture_id=fixture_id,
            league_name=league_name,
            home_team_name=home_team_name,
            away_team_name=away_team_name,
            kickoff_utc=kickoff_utc,
        )

    def _upsert_synthetic_league(self, cursor, league_name: str, sport_key: str, season_name: str) -> int:
        cursor.execute(
            """
            INSERT INTO core.leagues (
                thesportsdb_league_id,
                league_name,
                sport_name,
                country_name,
                current_season_name,
                is_active,
                last_synced_at
            )
            VALUES (%s, %s, 'Soccer', 'World', %s, true, now())
            ON CONFLICT (thesportsdb_league_id) DO UPDATE
            SET league_name = EXCLUDED.league_name,
                sport_name = EXCLUDED.sport_name,
                country_name = EXCLUDED.country_name,
                current_season_name = EXCLUDED.current_season_name,
                is_active = true,
                last_synced_at = now()
            RETURNING league_id
            """,
            (
                synthetic_bigint(f"league:{sport_key}:{league_name}"),
                league_name,
                season_name,
            ),
        )
        return int(cursor.fetchone()[0])

    def _upsert_synthetic_season(self, cursor, league_id: int, season_name: str) -> int:
        cursor.execute(
            """
            INSERT INTO core.seasons (
                league_id,
                season_name,
                season_label,
                is_current
            )
            VALUES (%s, %s, %s, true)
            ON CONFLICT (league_id, season_name) DO UPDATE
            SET season_label = EXCLUDED.season_label,
                is_current = true
            RETURNING season_id
            """,
            (league_id, season_name, season_name),
        )
        return int(cursor.fetchone()[0])

    def _upsert_synthetic_team(self, cursor, team_name: str) -> int:
        cursor.execute(
            """
            INSERT INTO core.teams (
                thesportsdb_team_id,
                team_name,
                sport_name,
                country_name,
                is_active,
                last_synced_at
            )
            VALUES (%s, %s, 'Soccer', 'World', true, now())
            ON CONFLICT (thesportsdb_team_id) DO UPDATE
            SET team_name = EXCLUDED.team_name,
                sport_name = EXCLUDED.sport_name,
                country_name = EXCLUDED.country_name,
                is_active = true,
                last_synced_at = now()
            RETURNING team_id
            """,
            (
                synthetic_bigint(f"team:{team_name}"),
                team_name,
            ),
        )
        return int(cursor.fetchone()[0])

    def _store_raw_payload(
        self,
        cursor,
        provider_id: int,
        endpoint_id: int,
        ingestion_run_id: str,
        object_type: str,
        object_id: str,
        natural_key: str,
        payload: dict[str, Any],
        request_path: str | None = None,
        request_params: dict[str, Any] | None = None,
    ) -> int:
        return store_provider_payload(
            cursor,
            provider_id=provider_id,
            endpoint_id=endpoint_id,
            ingestion_run_id=ingestion_run_id,
            object_type=object_type,
            object_id=object_id,
            natural_key=natural_key,
            payload=payload,
            request_path=request_path,
            request_params=request_params,
        )

    def _fetch_btts_event(self, sport_key: str, event: dict[str, Any]) -> dict[str, Any]:
        """Payload event-odds pour le marche btts, ou {} si hors fenetre.

        Chaque appel coute [regions] x 1 credit : on le reserve aux matchs
        dont le kickoff tombe dans btts_lookahead_hours (0 = jamais), avec
        un plafond d'appels par sync (protection quota en pleine saison).
        """
        lookahead_hours = getattr(self._settings, "btts_lookahead_hours", 0)
        if lookahead_hours <= 0:
            return {}
        max_events = getattr(self._settings, "btts_max_events_per_sync", 0)
        if max_events > 0 and self._btts_calls_made >= max_events:
            return {}
        commence_time = parse_iso_datetime(event.get("commence_time"))
        if commence_time is None:
            return {}
        if commence_time > datetime.now(UTC) + timedelta(hours=lookahead_hours):
            return {}
        event_id = str(event.get("id") or "").strip()
        if not event_id:
            return {}
        self._btts_calls_made += 1
        try:
            # Un seul appel event sert TOUS les marches additionnels (btts, O/U
            # alternatifs, handicap, double chance, DNB) -> quota mutualise.
            # region 'eu' seule pour limiter le cout (markets x regions x events).
            return self._client.get_event_odds(
                sport_key, event_id,
                markets="btts,alternate_totals,double_chance,draw_no_bet,spreads,alternate_spreads",
                regions="eu",
            )
        except Exception:
            # Marches additionnels indisponibles pour cet evenement (404/422) :
            # on ne fait pas echouer le run de cotes pour des marches optionnels.
            return {}

    def _ensure_bookmaker(self, cursor, bookmaker_code: str, bookmaker_name: str) -> int:
        cursor.execute(
            """
            INSERT INTO core.bookmakers (
                bookmaker_code,
                bookmaker_name,
                is_active
            )
            VALUES (%s, %s, true)
            ON CONFLICT (bookmaker_code) DO UPDATE
            SET bookmaker_name = EXCLUDED.bookmaker_name,
                is_active = true
            RETURNING bookmaker_id
            """,
            (bookmaker_code.upper(), bookmaker_name),
        )
        return int(cursor.fetchone()[0])

    def _upsert_fixture_odds(self, cursor, fixture_id: int, bookmaker_id: int, row: ExtractedOddsRow) -> None:
        cursor.execute(
            """
            INSERT INTO core.fixture_odds_1x2 (
                fixture_id,
                bookmaker_id,
                captured_at,
                home_odd,
                draw_odd,
                away_odd,
                is_closing_line,
                source_system,
                source_reference
            )
            VALUES (%s, %s, %s, %s, %s, %s, false, 'THEODDSAPI_V4', %s)
            ON CONFLICT (fixture_id, bookmaker_id, captured_at) DO UPDATE
            SET home_odd = EXCLUDED.home_odd,
                draw_odd = EXCLUDED.draw_odd,
                away_odd = EXCLUDED.away_odd,
                source_system = EXCLUDED.source_system,
                source_reference = EXCLUDED.source_reference
            """,
            (
                fixture_id,
                bookmaker_id,
                row.captured_at,
                row.home_odd,
                row.draw_odd,
                row.away_odd,
                row.source_reference,
            ),
        )

    def _upsert_fixture_odds_totals(
        self, cursor, fixture_id: int, bookmaker_id: int, row: ExtractedTotalsRow
    ) -> None:
        # Cote invalide (<= 1.0) = placeholder book : on saute au lieu de
        # violer la contrainte SQL et d'avorter toute la transaction.
        if any(o is None or float(o) <= 1.0 for o in (row.over_odd, row.under_odd)):
            return
        cursor.execute(
            """
            INSERT INTO core.fixture_odds_totals (
                fixture_id,
                bookmaker_id,
                captured_at,
                total_line,
                over_odd,
                under_odd,
                is_closing_line,
                source_system,
                source_reference
            )
            VALUES (%s, %s, %s, %s, %s, %s, false, 'THEODDSAPI_V4', %s)
            ON CONFLICT (fixture_id, bookmaker_id, total_line, captured_at) DO UPDATE
            SET over_odd = EXCLUDED.over_odd,
                under_odd = EXCLUDED.under_odd,
                source_system = EXCLUDED.source_system,
                source_reference = EXCLUDED.source_reference
            """,
            (
                fixture_id,
                bookmaker_id,
                row.captured_at,
                row.total_line,
                row.over_odd,
                row.under_odd,
                row.source_reference,
            ),
        )

    def _upsert_fixture_odds_btts(
        self, cursor, fixture_id: int, bookmaker_id: int, row: ExtractedBttsRow
    ) -> None:
        cursor.execute(
            """
            INSERT INTO core.fixture_odds_btts (
                fixture_id,
                bookmaker_id,
                captured_at,
                yes_odd,
                no_odd,
                is_closing_line,
                source_system,
                source_reference
            )
            VALUES (%s, %s, %s, %s, %s, false, 'THEODDSAPI_V4', %s)
            ON CONFLICT (fixture_id, bookmaker_id, captured_at) DO UPDATE
            SET yes_odd = EXCLUDED.yes_odd,
                no_odd = EXCLUDED.no_odd,
                source_system = EXCLUDED.source_system,
                source_reference = EXCLUDED.source_reference
            """,
            (
                fixture_id,
                bookmaker_id,
                row.captured_at,
                row.yes_odd,
                row.no_odd,
                row.source_reference,
            ),
        )

    def _upsert_fixture_odds_market(
        self, cursor, fixture_id: int, bookmaker_id: int, row: ExtractedMarketRow
    ) -> None:
        if row.decimal_odd is None or float(row.decimal_odd) <= 1.0:
            return
        cursor.execute(
            """
            INSERT INTO core.fixture_odds_market (
                fixture_id, bookmaker_id, market_code, line, selection_code,
                decimal_odd, captured_at, source_system, source_reference
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'THEODDSAPI_V4', %s)
            ON CONFLICT (fixture_id, bookmaker_id, market_code, line, selection_code, captured_at)
            DO UPDATE SET decimal_odd = EXCLUDED.decimal_odd,
                          source_reference = EXCLUDED.source_reference
            """,
            (
                fixture_id, bookmaker_id, row.market_code, row.line,
                row.selection_code, row.decimal_odd, row.captured_at,
                row.source_reference,
            ),
        )


def extract_1x2_rows(
    event: dict[str, Any],
    home_team_name: str,
    away_team_name: str,
    allowed_bookmakers: set[str] | None = None,
) -> list[ExtractedOddsRow]:
    rows: list[ExtractedOddsRow] = []
    event_id = str(event.get("id") or "")
    bookmakers = event.get("bookmakers") or []
    if not isinstance(bookmakers, list):
        return rows

    for bookmaker in bookmakers:
        if not isinstance(bookmaker, dict):
            continue
        bookmaker_key = str(bookmaker.get("key") or "").strip()
        if allowed_bookmakers is not None and bookmaker_key.lower() not in allowed_bookmakers:
            continue
        bookmaker_title = str(bookmaker.get("title") or bookmaker_key).strip()
        captured_at = parse_iso_datetime(bookmaker.get("last_update")) or datetime.now(UTC)
        markets = bookmaker.get("markets") or []
        if not isinstance(markets, list):
            continue
        for market in markets:
            if not isinstance(market, dict):
                continue
            if str(market.get("key") or "") != "h2h":
                continue
            mapped = map_outcomes_to_1x2(
                outcomes=market.get("outcomes") or [],
                home_team_name=home_team_name,
                away_team_name=away_team_name,
            )
            if mapped is None:
                continue
            rows.append(
                ExtractedOddsRow(
                    bookmaker_code=bookmaker_key,
                    bookmaker_name=bookmaker_title,
                    captured_at=captured_at,
                    home_odd=float(mapped["HOME"]),
                    draw_odd=float(mapped["DRAW"]),
                    away_odd=float(mapped["AWAY"]),
                    source_reference=f"{event_id}|{bookmaker_key}|h2h",
                )
            )
    return rows


def extract_totals_rows(
    event: dict[str, Any],
    allowed_bookmakers: set[str] | None = None,
) -> list[ExtractedTotalsRow]:
    """Cotes over/under par ligne. The Odds API renvoie les outcomes du
    marche "totals" avec name Over/Under et point = ligne de buts."""
    rows: list[ExtractedTotalsRow] = []
    event_id = str(event.get("id") or "")
    bookmakers = event.get("bookmakers") or []
    if not isinstance(bookmakers, list):
        return rows

    for bookmaker in bookmakers:
        if not isinstance(bookmaker, dict):
            continue
        bookmaker_key = str(bookmaker.get("key") or "").strip()
        if allowed_bookmakers is not None and bookmaker_key.lower() not in allowed_bookmakers:
            continue
        bookmaker_title = str(bookmaker.get("title") or bookmaker_key).strip()
        captured_at = parse_iso_datetime(bookmaker.get("last_update")) or datetime.now(UTC)
        markets = bookmaker.get("markets") or []
        if not isinstance(markets, list):
            continue
        for market in markets:
            # "totals" = ligne principale (bulk) ; "alternate_totals" = toutes
            # les lignes (0.5/1.5/3.5...) via l'endpoint par evenement.
            if not isinstance(market, dict) or str(market.get("key") or "") not in {"totals", "alternate_totals"}:
                continue
            # Regroupe Over/Under par ligne (point) : un book peut coter
            # plusieurs lignes (1.5, 2.5, 3.5) dans le meme marche.
            by_line: dict[float, dict[str, float]] = {}
            for outcome in market.get("outcomes") or []:
                if not isinstance(outcome, dict):
                    continue
                name = str(outcome.get("name") or "").strip().lower()
                price = outcome.get("price")
                point = outcome.get("point")
                if price is None or point is None or name not in {"over", "under"}:
                    continue
                try:
                    line = float(point)
                    odd = float(price)
                except (TypeError, ValueError):
                    continue
                if odd <= 1.0:
                    continue
                by_line.setdefault(line, {})[name] = odd
            for line, sides in sorted(by_line.items()):
                if "over" not in sides or "under" not in sides:
                    continue
                rows.append(
                    ExtractedTotalsRow(
                        bookmaker_code=bookmaker_key,
                        bookmaker_name=bookmaker_title,
                        captured_at=captured_at,
                        total_line=line,
                        over_odd=sides["over"],
                        under_odd=sides["under"],
                        source_reference=f"{event_id}|{bookmaker_key}|totals|{line}",
                    )
                )
    return rows


def extract_btts_rows(
    event: dict[str, Any],
    allowed_bookmakers: set[str] | None = None,
) -> list[ExtractedBttsRow]:
    """Cotes both-teams-to-score (marche "btts", outcomes Yes/No)."""
    rows: list[ExtractedBttsRow] = []
    event_id = str(event.get("id") or "")
    bookmakers = event.get("bookmakers") or []
    if not isinstance(bookmakers, list):
        return rows

    for bookmaker in bookmakers:
        if not isinstance(bookmaker, dict):
            continue
        bookmaker_key = str(bookmaker.get("key") or "").strip()
        if allowed_bookmakers is not None and bookmaker_key.lower() not in allowed_bookmakers:
            continue
        bookmaker_title = str(bookmaker.get("title") or bookmaker_key).strip()
        captured_at = parse_iso_datetime(bookmaker.get("last_update")) or datetime.now(UTC)
        markets = bookmaker.get("markets") or []
        if not isinstance(markets, list):
            continue
        for market in markets:
            if not isinstance(market, dict) or str(market.get("key") or "") != "btts":
                continue
            sides: dict[str, float] = {}
            for outcome in market.get("outcomes") or []:
                if not isinstance(outcome, dict):
                    continue
                name = str(outcome.get("name") or "").strip().lower()
                price = outcome.get("price")
                if price is None or name not in {"yes", "no"}:
                    continue
                try:
                    odd = float(price)
                except (TypeError, ValueError):
                    continue
                if odd > 1.0:
                    sides[name] = odd
            if "yes" not in sides or "no" not in sides:
                continue
            rows.append(
                ExtractedBttsRow(
                    bookmaker_code=bookmaker_key,
                    bookmaker_name=bookmaker_title,
                    captured_at=captured_at,
                    yes_odd=sides["yes"],
                    no_odd=sides["no"],
                    source_reference=f"{event_id}|{bookmaker_key}|btts",
                )
            )
    return rows


def extract_market_rows(
    event: dict[str, Any],
    allowed_bookmakers: set[str] | None = None,
) -> list[ExtractedMarketRow]:
    """Handicap (spreads/alternate_spreads), double chance, draw-no-bet.
    Les noms d'equipe sont mappes via event.home_team / event.away_team."""
    rows: list[ExtractedMarketRow] = []
    event_id = str(event.get("id") or "")
    home = str(event.get("home_team") or "").strip()
    away = str(event.get("away_team") or "").strip()
    bookmakers = event.get("bookmakers") or []
    if not isinstance(bookmakers, list) or not home or not away:
        return rows
    home_l, away_l = home.lower(), away.lower()
    for bookmaker in bookmakers:
        if not isinstance(bookmaker, dict):
            continue
        bk = str(bookmaker.get("key") or "").strip()
        if allowed_bookmakers is not None and bk.lower() not in allowed_bookmakers:
            continue
        title = str(bookmaker.get("title") or bk).strip()
        captured = parse_iso_datetime(bookmaker.get("last_update")) or datetime.now(UTC)
        for market in bookmaker.get("markets") or []:
            if not isinstance(market, dict):
                continue
            key = str(market.get("key") or "")
            for outcome in market.get("outcomes") or []:
                if not isinstance(outcome, dict):
                    continue
                name = str(outcome.get("name") or "").strip()
                try:
                    odd = float(outcome.get("price"))
                except (TypeError, ValueError):
                    continue
                if odd <= 1.0:
                    continue
                market_code = selection = None
                line = 0.0
                if key in ("spreads", "alternate_spreads"):
                    try:
                        line = float(outcome.get("point"))
                    except (TypeError, ValueError):
                        continue
                    selection = "HOME" if name == home else "AWAY" if name == away else None
                    market_code = "SPREAD"
                elif key == "draw_no_bet":
                    selection = "HOME" if name == home else "AWAY" if name == away else None
                    market_code = "DNB"
                elif key == "double_chance":
                    low = name.lower()
                    if home_l in low and "draw" in low:
                        selection = "DC_1X"
                    elif away_l in low and "draw" in low:
                        selection = "DC_X2"
                    elif home_l in low and away_l in low:
                        selection = "DC_12"
                    market_code = "DOUBLE_CHANCE"
                else:
                    continue
                if selection is None:
                    continue
                rows.append(ExtractedMarketRow(
                    bookmaker_code=bk, bookmaker_name=title, captured_at=captured,
                    market_code=market_code, line=line, selection_code=selection,
                    decimal_odd=odd,
                    source_reference=f"{event_id}|{bk}|{key}|{selection}|{line}",
                ))
    return rows


def map_outcomes_to_1x2(
    outcomes: Any,
    home_team_name: str,
    away_team_name: str,
) -> dict[str, float] | None:
    if not isinstance(outcomes, list):
        return None

    mapped: dict[str, float] = {}
    for outcome in outcomes:
        if not isinstance(outcome, dict):
            return None
        selection_name = str(outcome.get("name") or "")
        price = outcome.get("price")
        if price is None:
            return None
        selection_code = classify_selection(selection_name, home_team_name, away_team_name)
        if selection_code is None:
            return None
        mapped[selection_code] = float(price)

    if {"HOME", "DRAW", "AWAY"} <= mapped.keys():
        return mapped
    return None


def classify_selection(selection_name: str, home_team_name: str, away_team_name: str) -> str | None:
    normalized = canonical_label(selection_name)
    if normalized in {"draw", "tie", "x"}:
        return "DRAW"
    if compare_team_names(selection_name, home_team_name) > 0:
        return "HOME"
    if compare_team_names(selection_name, away_team_name) > 0:
        return "AWAY"
    return None


def parse_iso_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def league_name_for_sport_key(sport_key: str) -> str | None:
    for league_name, mapped_sport_key in SPORT_KEY_BY_LEAGUE_NAME.items():
        if mapped_sport_key == sport_key:
            return league_name
    return None


def synthetic_bigint(value: str) -> int:
    # Negative synthetic ids avoid collisions with real positive TheSportsDB ids.
    return -int(zlib.crc32(value.encode("utf-8")) & 0x7FFFFFFF)
