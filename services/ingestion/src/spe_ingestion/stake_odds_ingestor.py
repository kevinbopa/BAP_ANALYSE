from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import json
from typing import Any

from spe_ingestion.clients.stake import StakeClient
from spe_ingestion.config import StakeSettings
from spe_ingestion.odds_matching import LEAGUE_NAME_ALIASES, canonical_label, compare_team_names, slugify

TRUSTED_GROUP_NAMES = {
    "1x2 up",
    "winner",
    "threeway",
    "main",
}

TRUSTED_MARKET_NAMES = {
    "1x2",
    "match winner",
    "winner",
    "threeway",
}

@dataclass(frozen=True)
class StakeOddsIngestionSummary:
    leagues_scanned: int = 0
    tournaments_matched: int = 0
    fixtures_scanned: int = 0
    fixtures_matched: int = 0
    raw_payloads: int = 0
    odds_written: int = 0
    fixtures_without_1x2: int = 0


@dataclass(frozen=True)
class DbLeagueTarget:
    league_id: int
    league_name: str
    country_name: str


@dataclass(frozen=True)
class DbFixtureCandidate:
    fixture_id: int
    league_id: int
    league_name: str
    home_team_name: str
    away_team_name: str
    kickoff_utc: datetime | None


@dataclass(frozen=True)
class StakeTournamentTarget:
    league: DbLeagueTarget
    category_slug: str
    tournament_name: str
    tournament_slug: str


@dataclass(frozen=True)
class ExtractedOdds1X2:
    home_odd: float
    draw_odd: float
    away_odd: float
    captured_at: datetime
    market_name: str
    group_name: str


class StakeOddsIngestor:
    def __init__(self, client: StakeClient, settings: StakeSettings) -> None:
        self._client = client
        self._settings = settings

    def ingest_upcoming_1x2(self, connection) -> StakeOddsIngestionSummary:
        summary = StakeOddsIngestionSummary()
        with connection.cursor() as cursor:
            provider_id = self._fetch_provider_id(cursor)
            endpoint_id = self._fetch_endpoint_id(cursor, "SPORTSBOOK_FIXTURE_DETAILS")
            bookmaker_id = self._ensure_bookmaker(cursor)
            run_id = self._start_run(cursor, provider_id, endpoint_id)
            fixtures_by_league = self._load_db_fixtures(cursor)

            try:
                for league_id, db_fixtures in fixtures_by_league.items():
                    if not db_fixtures:
                        continue

                    league = DbLeagueTarget(
                        league_id=league_id,
                        league_name=db_fixtures[0].league_name,
                        country_name=self._lookup_league_country(cursor, league_id),
                    )
                    summary = StakeOddsIngestionSummary(
                        leagues_scanned=summary.leagues_scanned + 1,
                        tournaments_matched=summary.tournaments_matched,
                        fixtures_scanned=summary.fixtures_scanned,
                        fixtures_matched=summary.fixtures_matched,
                        raw_payloads=summary.raw_payloads,
                        odds_written=summary.odds_written,
                        fixtures_without_1x2=summary.fixtures_without_1x2,
                    )

                    target = self._resolve_tournament_target(league)
                    if target is None:
                        continue

                    summary = StakeOddsIngestionSummary(
                        leagues_scanned=summary.leagues_scanned,
                        tournaments_matched=summary.tournaments_matched + 1,
                        fixtures_scanned=summary.fixtures_scanned,
                        fixtures_matched=summary.fixtures_matched,
                        raw_payloads=summary.raw_payloads,
                        odds_written=summary.odds_written,
                        fixtures_without_1x2=summary.fixtures_without_1x2,
                    )

                    stake_fixtures = self._client.get_fixtures(
                        self._settings.sport_slug,
                        target.category_slug,
                        target.tournament_slug,
                    )
                    candidate_pool = list(db_fixtures)
                    for stake_fixture in stake_fixtures:
                        summary = StakeOddsIngestionSummary(
                            leagues_scanned=summary.leagues_scanned,
                            tournaments_matched=summary.tournaments_matched,
                            fixtures_scanned=summary.fixtures_scanned + 1,
                            fixtures_matched=summary.fixtures_matched,
                            raw_payloads=summary.raw_payloads,
                            odds_written=summary.odds_written,
                            fixtures_without_1x2=summary.fixtures_without_1x2,
                        )
                        matched = self._match_fixture(candidate_pool, stake_fixture)
                        if matched is None:
                            continue
                        candidate_pool.remove(matched)
                        summary = StakeOddsIngestionSummary(
                            leagues_scanned=summary.leagues_scanned,
                            tournaments_matched=summary.tournaments_matched,
                            fixtures_scanned=summary.fixtures_scanned,
                            fixtures_matched=summary.fixtures_matched + 1,
                            raw_payloads=summary.raw_payloads,
                            odds_written=summary.odds_written,
                            fixtures_without_1x2=summary.fixtures_without_1x2,
                        )

                        fixture_slug = str(stake_fixture.get("slug") or "").strip()
                        if not fixture_slug:
                            continue
                        payload = self._client.get_fixture_odds(fixture_slug)
                        self._store_raw_payload(
                            cursor=cursor,
                            provider_id=provider_id,
                            endpoint_id=endpoint_id,
                            ingestion_run_id=run_id,
                            object_type="ODDS_FIXTURE",
                            object_id=fixture_slug,
                            natural_key=f"stake:fixture:{fixture_slug}",
                            payload=payload,
                        )
                        summary = StakeOddsIngestionSummary(
                            leagues_scanned=summary.leagues_scanned,
                            tournaments_matched=summary.tournaments_matched,
                            fixtures_scanned=summary.fixtures_scanned,
                            fixtures_matched=summary.fixtures_matched,
                            raw_payloads=summary.raw_payloads + 1,
                            odds_written=summary.odds_written,
                            fixtures_without_1x2=summary.fixtures_without_1x2,
                        )

                        extracted = extract_1x2_odds(
                            payload=payload,
                            home_team_name=matched.home_team_name,
                            away_team_name=matched.away_team_name,
                        )
                        if extracted is None:
                            summary = StakeOddsIngestionSummary(
                                leagues_scanned=summary.leagues_scanned,
                                tournaments_matched=summary.tournaments_matched,
                                fixtures_scanned=summary.fixtures_scanned,
                                fixtures_matched=summary.fixtures_matched,
                                raw_payloads=summary.raw_payloads,
                                odds_written=summary.odds_written,
                                fixtures_without_1x2=summary.fixtures_without_1x2 + 1,
                            )
                            continue

                        self._upsert_fixture_odds(
                            cursor=cursor,
                            fixture_id=matched.fixture_id,
                            bookmaker_id=bookmaker_id,
                            fixture_slug=fixture_slug,
                            extracted=extracted,
                        )
                        summary = StakeOddsIngestionSummary(
                            leagues_scanned=summary.leagues_scanned,
                            tournaments_matched=summary.tournaments_matched,
                            fixtures_scanned=summary.fixtures_scanned,
                            fixtures_matched=summary.fixtures_matched,
                            raw_payloads=summary.raw_payloads,
                            odds_written=summary.odds_written + 1,
                            fixtures_without_1x2=summary.fixtures_without_1x2,
                        )

                self._finish_run(cursor, run_id, summary)
                connection.commit()
                return summary
            except Exception as exc:
                self._fail_run(cursor, run_id, summary, str(exc))
                connection.commit()
                raise

    def _fetch_provider_id(self, cursor) -> int:
        cursor.execute("SELECT provider_id FROM ops.providers WHERE provider_code = 'STAKE'")
        row = cursor.fetchone()
        if not row:
            raise RuntimeError("STAKE provider is missing from ops.providers")
        return int(row[0])

    def _fetch_endpoint_id(self, cursor, endpoint_code: str) -> int:
        cursor.execute(
            """
            SELECT e.endpoint_id
            FROM ops.provider_endpoints e
            JOIN ops.providers p ON p.provider_id = e.provider_id
            WHERE p.provider_code = 'STAKE'
              AND e.endpoint_code = %s
            """,
            (endpoint_code,),
        )
        row = cursor.fetchone()
        if not row:
            raise RuntimeError(f"Missing Stake endpoint metadata for {endpoint_code}")
        return int(row[0])

    def _ensure_bookmaker(self, cursor) -> int:
        cursor.execute(
            """
            INSERT INTO core.bookmakers (
                bookmaker_code,
                bookmaker_name,
                website_url,
                is_active
            )
            VALUES ('STAKE', 'Stake', 'https://stake.com', true)
            ON CONFLICT (bookmaker_code) DO UPDATE
            SET bookmaker_name = EXCLUDED.bookmaker_name,
                website_url = EXCLUDED.website_url,
                is_active = true
            RETURNING bookmaker_id
            """
        )
        return int(cursor.fetchone()[0])

    def _start_run(self, cursor, provider_id: int, endpoint_id: int) -> str:
        cursor.execute(
            """
            INSERT INTO ops.ingestion_runs (
                provider_id,
                endpoint_id,
                run_scope,
                request_params,
                started_at,
                status_code
            )
            VALUES (%s, %s, %s, %s::jsonb, now(), 'RUNNING')
            RETURNING ingestion_run_id
            """,
            (
                provider_id,
                endpoint_id,
                "stake_upcoming_1x2",
                json.dumps(
                    {
                        "sport": self._settings.sport_slug,
                        "lookahead_days": self._settings.lookahead_days,
                        "match_window_minutes": self._settings.match_window_minutes,
                    }
                ),
            ),
        )
        return str(cursor.fetchone()[0])

    def _finish_run(self, cursor, run_id: str, summary: StakeOddsIngestionSummary) -> None:
        cursor.execute(
            """
            UPDATE ops.ingestion_runs
            SET finished_at = now(),
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

    def _fail_run(self, cursor, run_id: str, summary: StakeOddsIngestionSummary, error_message: str) -> None:
        cursor.execute(
            """
            UPDATE ops.ingestion_runs
            SET finished_at = now(),
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

    def _load_db_fixtures(self, cursor) -> dict[int, list[DbFixtureCandidate]]:
        now_utc = datetime.now(UTC)
        max_kickoff = now_utc + timedelta(days=self._settings.lookahead_days)
        cursor.execute(
            """
            SELECT
                f.fixture_id,
                f.league_id,
                l.league_name,
                home.team_name AS home_team_name,
                away.team_name AS away_team_name,
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
            (now_utc - timedelta(days=1), max_kickoff),
        )
        grouped: dict[int, list[DbFixtureCandidate]] = {}
        for fixture_id, league_id, league_name, home_team_name, away_team_name, kickoff_utc in cursor.fetchall():
            grouped.setdefault(int(league_id), []).append(
                DbFixtureCandidate(
                    fixture_id=int(fixture_id),
                    league_id=int(league_id),
                    league_name=str(league_name),
                    home_team_name=str(home_team_name),
                    away_team_name=str(away_team_name),
                    kickoff_utc=kickoff_utc,
                )
            )
        return grouped

    def _lookup_league_country(self, cursor, league_id: int) -> str:
        cursor.execute(
            "SELECT country_name FROM core.leagues WHERE league_id = %s",
            (league_id,),
        )
        row = cursor.fetchone()
        return str(row[0] or "")

    def _resolve_tournament_target(self, league: DbLeagueTarget) -> StakeTournamentTarget | None:
        category_slug = slugify(league.country_name)
        if not category_slug:
            return None

        tournaments = self._client.get_tournaments(self._settings.sport_slug, category_slug)
        if not tournaments:
            return None

        expected_names = {
            canonical_label(league.league_name),
            *{canonical_label(name) for name in LEAGUE_NAME_ALIASES.get(league.league_name, ())},
        }
        for tournament in tournaments:
            tournament_name = str(tournament.get("name") or "")
            tournament_slug = str(tournament.get("slug") or "")
            tournament_label = canonical_label(tournament_name)
            if tournament_label in expected_names:
                return StakeTournamentTarget(
                    league=league,
                    category_slug=category_slug,
                    tournament_name=tournament_name,
                    tournament_slug=tournament_slug,
                )
        return None

    def _match_fixture(
        self,
        candidates: list[DbFixtureCandidate],
        stake_fixture: dict[str, Any],
    ) -> DbFixtureCandidate | None:
        competitors = stake_fixture.get("competitors") or []
        if not isinstance(competitors, list) or len(competitors) != 2:
            return None

        home_team = str(competitors[0])
        away_team = str(competitors[1])
        kickoff_utc = epoch_millis_to_datetime(stake_fixture.get("startTime"))
        if kickoff_utc is None:
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
    ) -> None:
        payload_text = json.dumps(payload, sort_keys=True, ensure_ascii=True)
        checksum = sha256(payload_text.encode("utf-8")).hexdigest()
        cursor.execute(
            """
            INSERT INTO raw.provider_payloads (
                provider_id,
                endpoint_id,
                ingestion_run_id,
                provider_object_type,
                provider_object_id,
                natural_key,
                payload,
                payload_checksum
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s)
            """,
            (
                provider_id,
                endpoint_id,
                ingestion_run_id,
                object_type,
                object_id,
                natural_key,
                payload_text,
                checksum,
            ),
        )

    def _upsert_fixture_odds(
        self,
        cursor,
        fixture_id: int,
        bookmaker_id: int,
        fixture_slug: str,
        extracted: ExtractedOdds1X2,
    ) -> None:
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
            VALUES (%s, %s, %s, %s, %s, %s, false, 'STAKE_ODDS_API', %s)
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
                extracted.captured_at,
                extracted.home_odd,
                extracted.draw_odd,
                extracted.away_odd,
                f"{fixture_slug}|{extracted.group_name}|{extracted.market_name}",
            ),
        )


def extract_1x2_odds(
    payload: dict[str, Any],
    home_team_name: str,
    away_team_name: str,
) -> ExtractedOdds1X2 | None:
    best_candidate: ExtractedOdds1X2 | None = None
    best_priority = -1
    groups = payload.get("groups") or []
    if not isinstance(groups, list):
        return None

    for group in groups:
        if not isinstance(group, dict):
            continue
        group_name = str(group.get("name") or "")
        group_priority = 2 if canonical_label(group_name) in TRUSTED_GROUP_NAMES else 0
        for market in flatten_markets(group.get("markets")):
            if not isinstance(market, dict):
                continue
            outcomes = market.get("outcomes") or []
            if not isinstance(outcomes, list) or len(outcomes) != 3:
                continue
            mapped = map_outcomes_to_1x2(outcomes, home_team_name, away_team_name)
            if mapped is None:
                continue
            market_name = str(market.get("name") or "")
            market_priority = 1 if canonical_label(market_name) in TRUSTED_MARKET_NAMES else 0
            priority = group_priority + market_priority
            captured_at = epoch_millis_to_datetime(market.get("updatedAt"))
            if captured_at is None:
                fixture = payload.get("fixture") or {}
                fixture_updated_at = fixture.get("updatedAt") if isinstance(fixture, dict) else None
                captured_at = epoch_millis_to_datetime(fixture_updated_at) or datetime.now(UTC)
            candidate = ExtractedOdds1X2(
                home_odd=float(mapped["HOME"]),
                draw_odd=float(mapped["DRAW"]),
                away_odd=float(mapped["AWAY"]),
                captured_at=captured_at,
                market_name=market_name or "unknown",
                group_name=group_name or "unknown",
            )
            if priority > best_priority:
                best_candidate = candidate
                best_priority = priority
    return best_candidate


def flatten_markets(markets: Any) -> list[dict[str, Any]]:
    flattened: list[dict[str, Any]] = []
    if isinstance(markets, dict):
        return [markets]
    if not isinstance(markets, list):
        return flattened
    for item in markets:
        if isinstance(item, dict):
            flattened.append(item)
        elif isinstance(item, list):
            for nested in item:
                if isinstance(nested, dict):
                    flattened.append(nested)
    return flattened


def map_outcomes_to_1x2(
    outcomes: list[dict[str, Any]],
    home_team_name: str,
    away_team_name: str,
) -> dict[str, float] | None:
    mapped: dict[str, float] = {}
    for outcome in outcomes:
        if not isinstance(outcome, dict):
            return None
        selection_name = str(outcome.get("name") or "")
        odd_value = outcome.get("odds")
        if odd_value is None:
            return None
        selection_code = classify_selection(selection_name, home_team_name, away_team_name)
        if selection_code is None:
            return None
        mapped[selection_code] = float(odd_value)

    if {"HOME", "DRAW", "AWAY"} <= mapped.keys():
        return mapped
    return None


def classify_selection(selection_name: str, home_team_name: str, away_team_name: str) -> str | None:
    normalized = canonical_label(selection_name)
    if normalized in {"draw", "tie", "x"}:
        return "DRAW"
    if normalized in {"1", "home"}:
        return "HOME"
    if normalized in {"2", "away"}:
        return "AWAY"

    if compare_team_names(selection_name, home_team_name) > 0:
        return "HOME"
    if compare_team_names(selection_name, away_team_name) > 0:
        return "AWAY"
    return None


def epoch_millis_to_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        return datetime.fromtimestamp(float(value) / 1000.0, tz=UTC)
    except (TypeError, ValueError, OSError):
        return None
