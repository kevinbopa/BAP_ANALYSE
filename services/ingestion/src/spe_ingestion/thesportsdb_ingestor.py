from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from spe_ingestion.clients.thesportsdb import TheSportsDBClient
from spe_ingestion.payload_pipeline import record_payload_normalization, store_provider_payload
from spe_ingestion.run_journal import run_metadata, touch_run


PREFERRED_DOMESTIC_LEAGUE_NAMES = {
    "English Premier League",
    "Spanish La Liga",
    "French Ligue 1",
    "German Bundesliga",
    "Italian Serie A",
}

INTERNATIONAL_REGION_NAMES = {
    "World",
    "Worldwide",  # etiquette utilisee par la liste premium (ex: FIFA World Cup)
    "Europe",
    "Africa",
    "Asia",
    "Oceania",
    "South America",
    "North & Central America",
    "North America",
    "Central America",
    "International",
}

INTERNATIONAL_LEAGUE_KEYWORDS = (
    # Major tournaments
    "world cup",
    "world championship",
    "euro",
    "uefa european championship",
    "uefa nations",
    "nations league",
    "copa america",
    "gold cup",
    "asian cup",
    "african cup",
    "africa cup",
    "afcon",
    "confederations cup",
    "arab cup",
    "fifa arab cup",
    "fifa world cup",
    # Qualifiers (les qualifs contiennent toutes "world cup"/"qualifying"/le nom
    # de la coupe — les prefixes bruts de confederation attrapaient des
    # competitions de CLUBS comme "AFC Challenge League", retires)
    "qualification",
    "qualifier",
    "qualifiers",
    "qualifying",
    "wcq",
    "wc qualification",
    "finalissima",
    # Friendlies and lower-tier internationals
    "friendly",
    "friendlies",
    "international friendly",
    "fifa series",
    # Multi-sport / Olympics
    "olympic",
    "olympics",
    # Youth (kept for completeness, can be filtered downstream if noisy)
    "u-23",
    "u23",
    "u-21",
    "u21",
    "u-20",
    "u20",
)


@dataclass(frozen=True)
class IngestionSummary:
    leagues: int = 0
    seasons: int = 0
    teams: int = 0
    fixtures: int = 0


class TheSportsDBIngestor:
    def __init__(self, client: TheSportsDBClient) -> None:
        self._client = client
        self._history_start_year = client._settings.history_start_year
        self._active_run_id: str | None = None

    def ingest_reference_bundle(self, connection) -> IngestionSummary:
        summary = IngestionSummary()
        with connection.cursor() as cursor:
            provider_id = self._fetch_provider_id(cursor)
            run_id = self._start_run(cursor, provider_id)
            self._active_run_id = run_id
            connection.commit()
        try:
            summary = self._ingest_all_leagues(connection, provider_id)
        except Exception as exc:
            # La transaction en cours est avortee : rollback obligatoire avant
            # de pouvoir ecrire le statut FAILED.
            connection.rollback()
            with connection.cursor() as cursor:
                self._finish_run(cursor, run_id, "FAILED", summary, error=str(exc)[:500])
            connection.commit()
            raise
        with connection.cursor() as cursor:
            self._finish_run(cursor, run_id, "SUCCESS", summary)
        connection.commit()
        self._active_run_id = None
        return summary

    def _start_run(self, cursor, provider_id: int) -> str:
        meta = run_metadata(
            {"history_start_year": self._history_start_year},
            default_trigger="MANUAL",
        )
        cursor.execute(
            """
            INSERT INTO ops.ingestion_runs (
                provider_id, run_scope, status_code, started_at, heartbeat_at,
                request_params, request_fingerprint, application_name,
                trigger_source, host_name, process_id
            )
            VALUES (%s, 'REFERENCE_BUNDLE', 'RUNNING', now(), now(), %s::jsonb, %s, %s, %s, %s, %s)
            RETURNING ingestion_run_id
            """,
            (
                provider_id,
                meta["request_params_json"],
                meta["request_fingerprint"],
                meta["application_name"],
                meta["trigger_source"],
                meta["host_name"],
                meta["process_id"],
            ),
        )
        return str(cursor.fetchone()[0])

    def _finish_run(self, cursor, run_id: str, status_code: str, summary: IngestionSummary, error: str | None = None) -> None:
        cursor.execute(
            """
            UPDATE ops.ingestion_runs
            SET status_code = %s,
                finished_at = now(),
                heartbeat_at = now(),
                records_received = %s,
                records_written = %s,
                error_message = %s
            WHERE ingestion_run_id = %s
            """,
            (
                status_code,
                summary.fixtures,
                summary.fixtures + summary.teams + summary.seasons + summary.leagues,
                error,
                run_id,
            ),
        )

    def _ingest_all_leagues(self, connection, provider_id: int) -> IngestionSummary:
        summary = IngestionSummary()
        with connection.cursor() as cursor:
            for league in self._load_target_leagues():
                if self._active_run_id:
                    touch_run(cursor, self._active_run_id)
                payload_id = self._store_raw_payload(
                    cursor=cursor,
                    provider_id=provider_id,
                    object_type="LEAGUE",
                    object_id=league.get("idLeague"),
                    natural_key=f"league:{league.get('idLeague')}",
                    payload=league,
                )
                league_id = self._upsert_league(cursor, league)
                record_payload_normalization(
                    cursor,
                    provider_payload_id=payload_id,
                    ingestion_run_id=self._active_run_id,
                    normalization_target="core.leagues",
                    status_code="SUCCESS",
                    records_written=1,
                    metadata={"league_id": league_id},
                )
                summary = IngestionSummary(
                    leagues=summary.leagues + 1,
                    seasons=summary.seasons,
                    teams=summary.teams,
                    fixtures=summary.fixtures,
                )

                seasons = self._load_target_seasons_for_league(league)
                summary = self._upsert_seasons(cursor, provider_id, league_id, league, seasons, summary)
                summary = self._ingest_teams_for_league(cursor, provider_id, league_id, league, summary)
                for season_name in seasons:
                    summary = self._ingest_fixtures_for_season(
                        cursor=cursor,
                        provider_id=provider_id,
                        league_id=league_id,
                        league=league,
                        season_name=season_name,
                        summary=summary,
                    )
                # Commit par ligue : un crash sur une ligue ne perd pas les
                # heures de sync des ligues precedentes.
                connection.commit()

        connection.commit()
        return summary

    def _load_target_leagues(self) -> list[dict[str, Any]]:
        payload = self._client.get_json("all_leagues.php")
        leagues = payload.get("leagues") or payload.get("countries") or []
        results: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for league in leagues:
            if not isinstance(league, dict):
                continue
            if (league.get("strSport") or "") != "Soccer":
                continue
            if _is_target_league(league):
                results.append(league)
                seen_ids.add(str(league.get("idLeague") or ""))

        # La cle gratuite tronque all_leagues.php a ~10 ligues domestiques.
        # On force les competitions internationales par ID direct
        # (lookupleague.php repond meme pour les ligues absentes de la liste).
        for league_id in getattr(self._client._settings, "extra_league_ids", ()):  # type: ignore[attr-defined]
            if str(league_id) in seen_ids:
                continue
            payload = self._client.get_json("lookupleague.php", {"id": str(league_id)})
            entries = payload.get("leagues") or []
            if not entries or not isinstance(entries[0], dict):
                continue
            league = entries[0]
            if (league.get("strSport") or "") != "Soccer":
                continue
            results.append(league)
            seen_ids.add(str(league_id))

        results.sort(key=lambda item: ((item.get("strCountry") or ""), (item.get("strLeague") or "")))
        return results

    def _load_target_seasons_for_league(self, league: dict[str, Any]) -> list[str]:
        payload = self._client.get_json(
            "search_all_seasons.php",
            {"id": str(league["idLeague"])},
        )
        seasons = payload.get("seasons") or []
        season_names: list[str] = []
        current_season = str(league.get("strCurrentSeason") or "").strip()
        for season in seasons:
            season_name = str(season.get("strSeason") or "").strip()
            if not season_name:
                continue
            start_year = _season_start_year(season_name)
            if start_year is None or start_year < self._history_start_year:
                continue
            season_names.append(season_name)
        if current_season and current_season not in season_names:
            start_year = _season_start_year(current_season)
            if start_year is not None and start_year >= self._history_start_year:
                season_names.append(current_season)
        return sorted(set(season_names))

    def _fetch_provider_id(self, cursor) -> int:
        cursor.execute("SELECT provider_id FROM ops.providers WHERE provider_code = 'THESPORTSDB'")
        row = cursor.fetchone()
        if not row:
            raise RuntimeError("THESPORTSDB provider is missing from ops.providers")
        return int(row[0])

    def _store_raw_payload(
        self,
        cursor,
        provider_id: int,
        object_type: str,
        object_id: Any,
        natural_key: str,
        payload: dict[str, Any],
    ) -> int:
        return store_provider_payload(
            cursor,
            provider_id=provider_id,
            ingestion_run_id=self._active_run_id,
            object_type=object_type,
            object_id=object_id,
            natural_key=natural_key,
            payload=payload,
            request_path=f"/thesportsdb/{str(object_type).lower()}",
            request_params={"object_id": object_id, "object_type": object_type},
        )

    def _upsert_league(self, cursor, league: dict[str, Any]) -> int:
        cursor.execute(
            """
            INSERT INTO core.leagues (
                thesportsdb_league_id,
                league_name,
                alternate_name,
                sport_name,
                country_name,
                current_season_name,
                formed_year,
                gender_code,
                description_en,
                website_url,
                facebook_url,
                twitter_url,
                youtube_url,
                badge_url,
                logo_url,
                poster_url,
                trophy_url,
                fanart_url,
                last_synced_at
            )
            VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now()
            )
            ON CONFLICT (thesportsdb_league_id) DO UPDATE
            SET
                league_name = EXCLUDED.league_name,
                alternate_name = EXCLUDED.alternate_name,
                sport_name = EXCLUDED.sport_name,
                country_name = EXCLUDED.country_name,
                current_season_name = EXCLUDED.current_season_name,
                formed_year = EXCLUDED.formed_year,
                gender_code = EXCLUDED.gender_code,
                description_en = EXCLUDED.description_en,
                website_url = EXCLUDED.website_url,
                facebook_url = EXCLUDED.facebook_url,
                twitter_url = EXCLUDED.twitter_url,
                youtube_url = EXCLUDED.youtube_url,
                badge_url = EXCLUDED.badge_url,
                logo_url = EXCLUDED.logo_url,
                poster_url = EXCLUDED.poster_url,
                trophy_url = EXCLUDED.trophy_url,
                fanart_url = EXCLUDED.fanart_url,
                last_synced_at = now()
            RETURNING league_id
            """,
            (
                int(league["idLeague"]),
                league.get("strLeague"),
                league.get("strLeagueAlternate"),
                league.get("strSport"),
                league.get("strCountry"),
                league.get("strCurrentSeason"),
                _to_int(league.get("intFormedYear")),
                league.get("strGender"),
                league.get("strDescriptionEN"),
                _normalize_url(league.get("strWebsite")),
                _normalize_url(league.get("strFacebook")),
                _normalize_url(league.get("strTwitter")),
                _normalize_url(league.get("strYoutube")),
                _normalize_url(league.get("strBadge")),
                _normalize_url(league.get("strLogo")),
                _normalize_url(league.get("strPoster")),
                _normalize_url(league.get("strTrophy")),
                _normalize_url(league.get("strFanart1")),
            ),
        )
        return int(cursor.fetchone()[0])

    def _upsert_seasons(
        self,
        cursor,
        provider_id: int,
        league_id: int,
        league: dict[str, Any],
        season_names: list[str],
        summary: IngestionSummary,
    ) -> IngestionSummary:
        count = summary.seasons
        current_season = str(league.get("strCurrentSeason") or "").strip()
        for season_name in season_names:
            payload_id = self._store_raw_payload(
                cursor=cursor,
                provider_id=provider_id,
                object_type="SEASON",
                object_id=league.get("idLeague"),
                natural_key=f"league:{league.get('idLeague')}:season:{season_name}",
                payload={"strSeason": season_name},
            )
            cursor.execute(
                """
                INSERT INTO core.seasons (
                    league_id,
                    season_name,
                    is_current
                )
                VALUES (%s, %s, %s)
                ON CONFLICT (league_id, season_name) DO UPDATE
                SET is_current = EXCLUDED.is_current
                """,
                (
                    league_id,
                    season_name,
                    season_name == current_season,
                ),
            )
            record_payload_normalization(
                cursor,
                provider_payload_id=payload_id,
                ingestion_run_id=self._active_run_id,
                normalization_target="core.seasons",
                status_code="SUCCESS",
                records_written=1,
                metadata={"league_id": league_id, "season_name": season_name},
            )
            count += 1
        return IngestionSummary(
            leagues=summary.leagues,
            seasons=count,
            teams=summary.teams,
            fixtures=summary.fixtures,
        )

    def _ingest_teams_for_league(
        self,
        cursor,
        provider_id: int,
        league_id: int,
        league: dict[str, Any],
        summary: IngestionSummary,
    ) -> IngestionSummary:
        payload = self._client.get_json("search_all_teams.php", {"l": league.get("strLeague", "")})
        teams = payload.get("teams") or []
        count = summary.teams
        for team in teams:
            if (team.get("strSport") or "") != "Soccer":
                continue
            payload_id = self._store_raw_payload(
                cursor=cursor,
                provider_id=provider_id,
                object_type="TEAM",
                object_id=team.get("idTeam"),
                natural_key=f"team:{team.get('idTeam')}",
                payload=team,
            )
            venue_id = self._upsert_team_venue(cursor, team)
            team_id = self._upsert_team(cursor, team, venue_id)
            current_season = str(league.get("strCurrentSeason") or "").strip()
            season_id = self._lookup_season_id(cursor, league_id, current_season)
            cursor.execute(
                """
                INSERT INTO core.team_season_memberships (
                    team_id,
                    league_id,
                    season_id,
                    membership_role
                )
                VALUES (%s, %s, %s, 'PARTICIPANT')
                ON CONFLICT (team_id, league_id, season_id) DO NOTHING
                """,
                (team_id, league_id, season_id),
            )
            record_payload_normalization(
                cursor,
                provider_payload_id=payload_id,
                ingestion_run_id=self._active_run_id,
                normalization_target="core.teams",
                status_code="SUCCESS",
                records_written=1,
                metadata={"team_id": team_id, "league_id": league_id, "season_id": season_id},
            )
            count += 1
        return IngestionSummary(
            leagues=summary.leagues,
            seasons=summary.seasons,
            teams=count,
            fixtures=summary.fixtures,
        )

    def _upsert_team_venue(self, cursor, team: dict[str, Any]) -> int | None:
        venue_external_id = _to_int(team.get("idVenue"))
        venue_name = team.get("strStadium")
        if not venue_name:
            return None
        cursor.execute(
            """
            INSERT INTO core.venues (
                thesportsdb_venue_id,
                venue_name,
                country_name,
                city_name,
                capacity,
                image_url,
                thumbnail_url,
                last_synced_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (thesportsdb_venue_id) DO UPDATE
            SET
                venue_name = EXCLUDED.venue_name,
                country_name = EXCLUDED.country_name,
                city_name = EXCLUDED.city_name,
                capacity = EXCLUDED.capacity,
                image_url = EXCLUDED.image_url,
                thumbnail_url = EXCLUDED.thumbnail_url,
                last_synced_at = now()
            RETURNING venue_id
            """,
            (
                venue_external_id,
                venue_name,
                team.get("strCountry"),
                team.get("strLocation"),
                _to_int(team.get("intStadiumCapacity")),
                _normalize_url(team.get("strStadiumThumb")),
                _normalize_url(team.get("strStadiumThumb")),
            ),
        )
        return int(cursor.fetchone()[0])

    def _upsert_team(self, cursor, team: dict[str, Any], venue_id: int | None) -> int:
        cursor.execute(
            """
            INSERT INTO core.teams (
                thesportsdb_team_id,
                apifootball_team_id,
                team_name,
                short_name,
                alternate_names,
                formed_year,
                sport_name,
                gender_code,
                country_name,
                stadium_name,
                venue_id,
                location_name,
                stadium_capacity,
                website_url,
                facebook_url,
                twitter_url,
                instagram_url,
                description_en,
                badge_url,
                jersey_url,
                logo_url,
                fanart_url,
                last_synced_at
            )
            VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now()
            )
            ON CONFLICT (thesportsdb_team_id) DO UPDATE
            SET
                apifootball_team_id = EXCLUDED.apifootball_team_id,
                team_name = EXCLUDED.team_name,
                short_name = EXCLUDED.short_name,
                alternate_names = EXCLUDED.alternate_names,
                formed_year = EXCLUDED.formed_year,
                sport_name = EXCLUDED.sport_name,
                gender_code = EXCLUDED.gender_code,
                country_name = EXCLUDED.country_name,
                stadium_name = EXCLUDED.stadium_name,
                venue_id = EXCLUDED.venue_id,
                location_name = EXCLUDED.location_name,
                stadium_capacity = EXCLUDED.stadium_capacity,
                website_url = EXCLUDED.website_url,
                facebook_url = EXCLUDED.facebook_url,
                twitter_url = EXCLUDED.twitter_url,
                instagram_url = EXCLUDED.instagram_url,
                description_en = EXCLUDED.description_en,
                badge_url = EXCLUDED.badge_url,
                jersey_url = EXCLUDED.jersey_url,
                logo_url = EXCLUDED.logo_url,
                fanart_url = EXCLUDED.fanart_url,
                last_synced_at = now()
            RETURNING team_id
            """,
            (
                int(team["idTeam"]),
                _to_int(team.get("idAPIfootball")),
                team.get("strTeam"),
                team.get("strTeamShort"),
                team.get("strAlternate"),
                _to_int(team.get("intFormedYear")),
                team.get("strSport"),
                team.get("strGender"),
                team.get("strCountry"),
                team.get("strStadium"),
                venue_id,
                team.get("strLocation"),
                _to_int(team.get("intStadiumCapacity")),
                _normalize_url(team.get("strWebsite")),
                _normalize_url(team.get("strFacebook")),
                _normalize_url(team.get("strTwitter")),
                _normalize_url(team.get("strInstagram")),
                team.get("strDescriptionEN"),
                _normalize_url(team.get("strBadge")),
                _normalize_url(team.get("strTeamJersey")),
                _normalize_url(team.get("strLogo")),
                _normalize_url(team.get("strFanart1")),
            ),
        )
        return int(cursor.fetchone()[0])

    def _ingest_fixtures_for_season(
        self,
        cursor,
        provider_id: int,
        league_id: int,
        league: dict[str, Any],
        season_name: str,
        summary: IngestionSummary,
    ) -> IngestionSummary:
        payload = self._client.get_json(
            "eventsseason.php",
            {"id": str(league["idLeague"]), "s": season_name},
        )
        events = payload.get("events") or []
        count = summary.fixtures
        season_id = self._lookup_season_id(cursor, league_id, season_name)
        for event in events:
            if (event.get("strSport") or "") != "Soccer":
                continue
            if not _is_target_event(event):
                continue

            # Savepoint par evenement : une ligne corrompue (donnee sale
            # TheSportsDB) est sautee sans perdre la ligue ni la sync.
            cursor.execute("SAVEPOINT event_ingest")
            payload_id: int | None = None
            try:
                payload_id = self._store_raw_payload(
                    cursor=cursor,
                    provider_id=provider_id,
                    object_type="FIXTURE",
                    object_id=event.get("idEvent"),
                    natural_key=f"fixture:{event.get('idEvent')}",
                    payload=event,
                )

                home_team_id = self._ensure_team_present(cursor, provider_id, _to_int(event.get("idHomeTeam")))
                away_team_id = self._ensure_team_present(cursor, provider_id, _to_int(event.get("idAwayTeam")))
                if home_team_id is None or away_team_id is None:
                    if payload_id is not None:
                        record_payload_normalization(
                            cursor,
                            provider_payload_id=payload_id,
                            ingestion_run_id=self._active_run_id,
                            normalization_target="core.fixtures",
                            status_code="SKIPPED",
                            error_message="Missing home or away team bridge",
                        )
                    cursor.execute("RELEASE SAVEPOINT event_ingest")
                    continue
                if home_team_id == away_team_id:
                    # Donnee corrompue TheSportsDB (ex: "England Women vs England
                    # Women") - violerait le CHECK home <> away de core.fixtures.
                    if payload_id is not None:
                        record_payload_normalization(
                            cursor,
                            provider_payload_id=payload_id,
                            ingestion_run_id=self._active_run_id,
                            normalization_target="core.fixtures",
                            status_code="SKIPPED",
                            error_message="Corrupt fixture row with identical home and away teams",
                        )
                    cursor.execute("RELEASE SAVEPOINT event_ingest")
                    continue

                venue_id = self._find_venue_id(cursor, _to_int(event.get("idVenue")))
                fixture_id = self._upsert_fixture(
                    cursor,
                    event,
                    league_id=league_id,
                    season_id=season_id,
                    venue_id=venue_id,
                    home_team_id=home_team_id,
                    away_team_id=away_team_id,
                )
                self._upsert_fixture_score(cursor, fixture_id, event)
                if payload_id is not None:
                    record_payload_normalization(
                        cursor,
                        provider_payload_id=payload_id,
                        ingestion_run_id=self._active_run_id,
                        normalization_target="core.fixtures",
                        status_code="SUCCESS",
                        records_written=2,
                        metadata={"fixture_id": fixture_id, "season_id": season_id},
                    )
            except Exception:
                if payload_id is not None:
                    record_payload_normalization(
                        cursor,
                        provider_payload_id=payload_id,
                        ingestion_run_id=self._active_run_id,
                        normalization_target="core.fixtures",
                        status_code="FAILED",
                        error_message="Fixture normalization failed",
                    )
                cursor.execute("ROLLBACK TO SAVEPOINT event_ingest")
                continue
            cursor.execute("RELEASE SAVEPOINT event_ingest")
            count += 1

        return IngestionSummary(
            leagues=summary.leagues,
            seasons=summary.seasons,
            teams=summary.teams,
            fixtures=count,
        )

    def _ensure_team_present(self, cursor, provider_id: int, thesportsdb_team_id: int | None) -> int | None:
        if thesportsdb_team_id is None:
            return None
        team_id = self._find_team_id(cursor, thesportsdb_team_id)
        if team_id is not None:
            return team_id

        payload = self._client.get_json("lookupteam.php", {"id": str(thesportsdb_team_id)})
        teams = payload.get("teams") or []
        if not teams:
            return None
        team = teams[0]
        payload_id = self._store_raw_payload(
            cursor=cursor,
            provider_id=provider_id,
            object_type="TEAM",
            object_id=team.get("idTeam"),
            natural_key=f"team:{team.get('idTeam')}",
            payload=team,
        )
        venue_id = self._upsert_team_venue(cursor, team)
        team_id = self._upsert_team(cursor, team, venue_id)
        record_payload_normalization(
            cursor,
            provider_payload_id=payload_id,
            ingestion_run_id=self._active_run_id,
            normalization_target="core.teams",
            status_code="SUCCESS",
            records_written=1,
            metadata={"team_id": team_id},
        )
        return team_id

    def _lookup_season_id(self, cursor, league_id: int, season_name: str | None) -> int | None:
        if not season_name:
            return None
        cursor.execute(
            "SELECT season_id FROM core.seasons WHERE league_id = %s AND season_name = %s",
            (league_id, season_name),
        )
        row = cursor.fetchone()
        return int(row[0]) if row else None

    def _find_team_id(self, cursor, thesportsdb_team_id: int | None) -> int | None:
        if thesportsdb_team_id is None:
            return None
        cursor.execute("SELECT team_id FROM core.teams WHERE thesportsdb_team_id = %s", (thesportsdb_team_id,))
        row = cursor.fetchone()
        return int(row[0]) if row else None

    def _find_venue_id(self, cursor, thesportsdb_venue_id: int | None) -> int | None:
        if thesportsdb_venue_id is None:
            return None
        cursor.execute("SELECT venue_id FROM core.venues WHERE thesportsdb_venue_id = %s", (thesportsdb_venue_id,))
        row = cursor.fetchone()
        return int(row[0]) if row else None

    def _upsert_fixture(
        self,
        cursor,
        event: dict[str, Any],
        *,
        league_id: int,
        season_id: int | None,
        venue_id: int | None,
        home_team_id: int,
        away_team_id: int,
    ) -> int:
        kickoff_utc = _build_timestamp(event.get("dateEvent"), event.get("strTime"))
        cursor.execute(
            """
            INSERT INTO core.fixtures (
                thesportsdb_event_id,
                league_id,
                season_id,
                home_team_id,
                away_team_id,
                venue_id,
                event_name,
                event_alternate_name,
                filename_key,
                kickoff_utc,
                event_date_utc,
                event_time_utc,
                round_number,
                status_code,
                status_text,
                postponed_flag,
                locked_flag,
                official_name,
                country_name,
                video_url,
                raw_last_snapshot_at,
                last_synced_at
            )
            VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now(), now()
            )
            ON CONFLICT (thesportsdb_event_id) DO UPDATE
            SET
                league_id = EXCLUDED.league_id,
                season_id = EXCLUDED.season_id,
                home_team_id = EXCLUDED.home_team_id,
                away_team_id = EXCLUDED.away_team_id,
                venue_id = EXCLUDED.venue_id,
                event_name = EXCLUDED.event_name,
                event_alternate_name = EXCLUDED.event_alternate_name,
                filename_key = EXCLUDED.filename_key,
                kickoff_utc = EXCLUDED.kickoff_utc,
                event_date_utc = EXCLUDED.event_date_utc,
                event_time_utc = EXCLUDED.event_time_utc,
                round_number = EXCLUDED.round_number,
                status_code = EXCLUDED.status_code,
                status_text = EXCLUDED.status_text,
                postponed_flag = EXCLUDED.postponed_flag,
                locked_flag = EXCLUDED.locked_flag,
                official_name = EXCLUDED.official_name,
                country_name = EXCLUDED.country_name,
                video_url = EXCLUDED.video_url,
                raw_last_snapshot_at = now(),
                last_synced_at = now()
            RETURNING fixture_id
            """,
            (
                int(event["idEvent"]),
                league_id,
                season_id,
                home_team_id,
                away_team_id,
                venue_id,
                event.get("strEvent") or f"{event.get('strHomeTeam')} vs {event.get('strAwayTeam')}",
                event.get("strEventAlternate"),
                event.get("strFilename"),
                kickoff_utc,
                _to_date(event.get("dateEvent")),
                _to_time(event.get("strTime")),
                _to_int(event.get("intRound")),
                event.get("strStatus"),
                event.get("strStatus"),
                _to_bool(event.get("strPostponed")),
                _to_bool(event.get("strLocked")),
                event.get("strOfficial"),
                event.get("strCountry"),
                _normalize_url(event.get("strVideo")),
            ),
        )
        return int(cursor.fetchone()[0])

    def _upsert_fixture_score(self, cursor, fixture_id: int, event: dict[str, Any]) -> None:
        winner_code = _winner_code(event.get("intHomeScore"), event.get("intAwayScore"))
        cursor.execute(
            """
            INSERT INTO core.fixture_scores (
                fixture_id,
                home_score,
                away_score,
                winner_code,
                score_status
            )
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (fixture_id) DO UPDATE
            SET
                home_score = EXCLUDED.home_score,
                away_score = EXCLUDED.away_score,
                winner_code = EXCLUDED.winner_code,
                score_status = EXCLUDED.score_status
            """,
            (
                fixture_id,
                _to_int(event.get("intHomeScore")),
                _to_int(event.get("intAwayScore")),
                winner_code,
                event.get("strStatus"),
            ),
        )


# Variantes hors scope V1 (football masculin senior a 11). Sans ces exclusions,
# une cle premium (liste complete des ligues) ferait entrer les competitions
# feminines, le beach soccer, le futsal et l'esport car leurs noms contiennent
# aussi "world cup" / "euro" / "qualifying".
EXCLUDED_LEAGUE_KEYWORDS = (
    "women",
    "womens",
    "female",
    "ladies",
    "girls",
    "beach",
    "futsal",
    "esport",
    "e-sport",
    "efootball",
    "fifae",
    "amputee",
    "indoor",
    # Competitions de CLUBS (hors scope equipes nationales V1)
    "club",
    "champions league",
    "europa league",
    "conference league",
    "libertadores",
    "sudamericana",
    "super cup",
    # Jeunes en dessous de U-20 (les U-20/21/23 restent admis, ponderes 0.65)
    "u-17",
    "u17",
    "u-16",
    "u16",
    "u-15",
    "u15",
    "youth league",
    # Variantes feminines supplementaires (" w " = "CONCACAF W Gold Cup")
    "femenin",
    "feminin",
    " w ",
    # Anciennes competitions de clubs europeennes (defuntes, zero saison 2015+)
    "european cup",
    "uefa cup",
)


EXCLUDED_EVENT_KEYWORDS = (
    "women",
    "womens",
    "female",
    "ladies",
    " w vs ",
    " w @ ",
    "u-17",
    "u17",
    "u-16",
    "u16",
    "u-15",
    "u15",
)


def _is_target_event(event: dict[str, Any]) -> bool:
    """Filtre au niveau EVENEMENT : certaines ligues mixtes (International
    Friendlies) contiennent des matchs feminins ou de tres jeunes categories
    que le filtre de ligue ne peut pas voir."""
    combined = " ".join(
        _normalized_text(event.get(key))
        for key in ("strEvent", "strHomeTeam", "strAwayTeam")
    )
    return not any(keyword in combined for keyword in EXCLUDED_EVENT_KEYWORDS)


def _is_target_league(league: dict[str, Any]) -> bool:
    league_name = _normalized_text(league.get("strLeague"))
    alternate_name = _normalized_text(league.get("strLeagueAlternate"))
    country_name = str(league.get("strCountry") or "").strip()
    formed_year = _to_int(league.get("intFormedYear"))

    combined_for_exclusion = f"{league_name} {alternate_name}"
    if any(keyword in combined_for_exclusion for keyword in EXCLUDED_LEAGUE_KEYWORDS):
        return False

    if str(league.get("strLeague") or "") in PREFERRED_DOMESTIC_LEAGUE_NAMES:
        return True

    combined = f"{league_name} {alternate_name}".strip()

    # La liste premium all_leagues.php est allegee : strCountry/intFormedYear
    # sont absents. Quand le pays manque, les mots-cles internationaux decident
    # seuls (les exclusions clubs/femmes/jeunes ont deja filtre au-dessus).
    if not country_name:
        return any(keyword in combined for keyword in INTERNATIONAL_LEAGUE_KEYWORDS)

    if formed_year is not None and formed_year > 0 and formed_year < 2010 and country_name not in INTERNATIONAL_REGION_NAMES:
        # Keep domestic expansions conservative for old local leagues.
        return False

    if country_name in INTERNATIONAL_REGION_NAMES:
        return any(keyword in combined for keyword in INTERNATIONAL_LEAGUE_KEYWORDS)

    return "friendly" in combined and "international" in combined


def _season_start_year(season_name: str) -> int | None:
    match = re.search(r"(19|20)\d{2}", season_name or "")
    if not match:
        return None
    return int(match.group(0))


def _normalized_text(value: Any) -> str:
    return str(value or "").strip().lower()


def _to_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def _to_bool(value: Any) -> bool:
    if value in (True, "true", "True", "1", 1, "yes", "YES"):
        return True
    return False


def _normalize_url(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.startswith("http://") or text.startswith("https://"):
        return text
    return f"https://{text}"


def _to_date(value: Any):
    if not value:
        return None
    import datetime as _dt

    return _dt.date.fromisoformat(str(value))


def _normalize_time_text(value: Any) -> str | None:
    """TheSportsDB renvoie parfois des horaires malformes ("15:00:00:00",
    "90'+3"...). On garde HH:MM:SS et on rejette le reste."""
    text = str(value or "").strip().replace("Z", "")
    if not text:
        return None
    parts = text.split(":")[:3]
    if len(parts) == 2:
        parts.append("00")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        return None
    return ":".join(f"{int(p):02d}" for p in parts)


def _to_time(value: Any):
    import datetime as _dt

    normalized = _normalize_time_text(value)
    if normalized is None:
        return None
    try:
        return _dt.time.fromisoformat(normalized)
    except ValueError:
        return None


def _build_timestamp(date_value: Any, time_value: Any):
    if not date_value:
        return None
    import datetime as _dt

    normalized = _normalize_time_text(time_value)
    if normalized is None:
        return None
    try:
        naive = _dt.datetime.fromisoformat(f"{date_value}T{normalized}")
    except ValueError:
        return None
    return naive.replace(tzinfo=_dt.timezone.utc)


def _winner_code(home_score: Any, away_score: Any) -> str | None:
    home = _to_int(home_score)
    away = _to_int(away_score)
    if home is None or away is None:
        return None
    if home > away:
        return "HOME"
    if away > home:
        return "AWAY"
    return "DRAW"
