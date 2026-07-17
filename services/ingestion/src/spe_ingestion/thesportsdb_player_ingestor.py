"""TheSportsDB Premium player-data ingestor.

Two jobs:

  1. ``ingest_team_rosters``  — full player rosters for every team in
     core.teams (lookup_all_players.php), with the API-Football bridge
     (idAPIfootball) captured when TheSportsDB provides it.

  2. ``backfill_fixture_details`` — for every completed fixture: lineups
     (who played, starter/sub), timeline (goals WITH assists, cards, subs)
     and team match stats (shots, possession...). Interruptible and
     resumable via ops.player_backfill_progress; international fixtures
     first, most recent first.

Hardening baked in from day one (journal E12-E14): savepoint per fixture,
commit per fixture, tolerant parsing, run journaling in ops.ingestion_runs.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

from spe_ingestion.clients.thesportsdb import TheSportsDBClient
from spe_ingestion.run_journal import run_metadata, touch_run

DOMESTIC_LEAGUE_NAMES = (
    "English Premier League",
    "Spanish La Liga",
    "French Ligue 1",
    "German Bundesliga",
    "Italian Serie A",
)


@dataclass(frozen=True)
class RosterSummary:
    teams_processed: int = 0
    players_upserted: int = 0


@dataclass(frozen=True)
class ContextSummary:
    fixtures_processed: int = 0
    fixtures_with_context: int = 0
    weather_filled: int = 0
    attendance_filled: int = 0
    fixtures_no_data: int = 0
    fixtures_error: int = 0


def extract_event_context(event: dict[str, Any]) -> dict[str, Any]:
    """Extrait les champs de contexte de la fiche evenement complete.

    Ne retourne que les champs REELLEMENT remplis — la couverture de
    TheSportsDB est inegale et on ne veut pas ecraser avec du vide.
    """
    context: dict[str, Any] = {}
    attendance = _to_int(event.get("intSpectators"))
    if attendance and attendance > 0:
        context["attendance"] = attendance
    weather = str(event.get("strWeather") or "").strip()
    if weather:
        context["weather_text"] = weather[:120]
    city = str(event.get("strCity") or "").strip()
    if city:
        context["venue_city"] = city[:120]
    country = str(event.get("strCountry") or "").strip()
    if country:
        context["venue_country"] = country[:120]
    return context


def extract_event_venue(event: dict[str, Any]) -> dict[str, Any]:
    """Stade du match (fiche complete uniquement — eventsseason ne l'a pas)."""
    venue: dict[str, Any] = {}
    tsdb_venue_id = _to_int(event.get("idVenue"))
    if tsdb_venue_id and tsdb_venue_id > 0:
        venue["thesportsdb_venue_id"] = tsdb_venue_id
    name = str(event.get("strVenue") or "").strip()
    if name:
        venue["venue_name"] = name[:200]
    return venue


@dataclass(frozen=True)
class BackfillSummary:
    fixtures_processed: int = 0
    fixtures_with_lineup: int = 0
    lineup_rows: int = 0
    timeline_rows: int = 0
    stat_rows: int = 0
    fixtures_no_data: int = 0
    fixtures_error: int = 0


class TheSportsDBPlayerIngestor:
    def __init__(self, client: TheSportsDBClient) -> None:
        self._client = client
        # Cache thesportsdb_player_id -> core player_id pour eviter un
        # aller-retour SQL par ligne de composition.
        self._player_cache: dict[int, int] = {}

    # ==================================================================
    # 1. Rosters complets
    # ==================================================================
    def ingest_team_rosters(self, connection) -> RosterSummary:
        teams_processed = 0
        players_upserted = 0
        with connection.cursor() as cursor:
            run_id = self._start_run(cursor, "PLAYER_ROSTERS")
            connection.commit()
            cursor.execute(
                """
                SELECT team_id, thesportsdb_team_id
                FROM core.teams
                WHERE thesportsdb_team_id > 0
                ORDER BY team_id
                """
            )
            teams = [(int(r[0]), int(r[1])) for r in cursor.fetchall()]

        try:
            for team_id, tsdb_team_id in teams:
                with connection.cursor() as cursor:
                    touch_run(cursor, run_id)
                    cursor.execute("SAVEPOINT roster_team")
                    try:
                        payload = self._client.get_json(
                            "lookup_all_players.php", {"id": str(tsdb_team_id)}
                        )
                        for player in payload.get("player") or []:
                            if not isinstance(player, dict):
                                continue
                            if (player.get("strSport") or "Soccer") != "Soccer":
                                continue
                            player_id = self._upsert_player_full(cursor, player)
                            if player_id is None:
                                continue
                            players_upserted += 1
                            cursor.execute(
                                """
                                INSERT INTO core.team_squad_members (
                                    team_id, player_id, season_year, competition_name,
                                    shirt_number, position_code
                                )
                                VALUES (%s, %s, 0, 'ROSTER', %s, %s)
                                ON CONFLICT (team_id, player_id, season_year, competition_name)
                                DO UPDATE SET shirt_number = EXCLUDED.shirt_number,
                                              position_code = EXCLUDED.position_code
                                """,
                                (
                                    team_id,
                                    player_id,
                                    _to_int(player.get("strNumber")),
                                    _position_code(player.get("strPosition")),
                                ),
                            )
                    except Exception:
                        cursor.execute("ROLLBACK TO SAVEPOINT roster_team")
                        continue
                    cursor.execute("RELEASE SAVEPOINT roster_team")
                    teams_processed += 1
                connection.commit()
        finally:
            summary = RosterSummary(teams_processed=teams_processed, players_upserted=players_upserted)
            with connection.cursor() as cursor:
                self._finish_run(cursor, run_id, "SUCCESS", received=teams_processed, written=players_upserted)
            connection.commit()
        return summary

    # ==================================================================
    # 2. Backfill lineups / timeline / stats
    # ==================================================================
    def backfill_fixture_details(
        self,
        connection,
        limit: int | None = None,
        international_only: bool = False,
    ) -> BackfillSummary:
        with connection.cursor() as cursor:
            run_id = self._start_run(cursor, "PLAYER_BACKFILL")
            connection.commit()
            fixtures = self._load_pending_backfill(cursor, limit, international_only)

        processed = 0
        with_lineup = 0
        lineup_rows = 0
        timeline_rows = 0
        stat_rows = 0
        no_data = 0
        errors = 0

        try:
            for fixture_id, tsdb_event_id, home_team_id, away_team_id in fixtures:
                with connection.cursor() as cursor:
                    touch_run(cursor, run_id)
                    cursor.execute("SAVEPOINT fixture_detail")
                    try:
                        n_lineup, n_timeline, n_stats = self._ingest_one_fixture(
                            cursor, fixture_id, tsdb_event_id, home_team_id, away_team_id
                        )
                        status = "DONE" if (n_lineup or n_timeline or n_stats) else "NO_DATA"
                        cursor.execute(
                            """
                            INSERT INTO ops.player_backfill_progress (
                                fixture_id, status_code, lineup_rows, timeline_rows, stat_rows
                            )
                            VALUES (%s, %s, %s, %s, %s)
                            ON CONFLICT (fixture_id) DO UPDATE
                            SET status_code = EXCLUDED.status_code,
                                lineup_rows = EXCLUDED.lineup_rows,
                                timeline_rows = EXCLUDED.timeline_rows,
                                stat_rows = EXCLUDED.stat_rows,
                                processed_at = now()
                            """,
                            (fixture_id, status, n_lineup, n_timeline, n_stats),
                        )
                        lineup_rows += n_lineup
                        timeline_rows += n_timeline
                        stat_rows += n_stats
                        if n_lineup:
                            with_lineup += 1
                        if status == "NO_DATA":
                            no_data += 1
                    except Exception as exc:
                        cursor.execute("ROLLBACK TO SAVEPOINT fixture_detail")
                        errors += 1
                        cursor.execute(
                            """
                            INSERT INTO ops.player_backfill_progress (fixture_id, status_code, error_message)
                            VALUES (%s, 'ERROR', %s)
                            ON CONFLICT (fixture_id) DO UPDATE
                            SET status_code = 'ERROR',
                                error_message = EXCLUDED.error_message,
                                processed_at = now()
                            """,
                            (fixture_id, str(exc)[:400]),
                        )
                    else:
                        cursor.execute("RELEASE SAVEPOINT fixture_detail")
                    processed += 1
                connection.commit()
        finally:
            with connection.cursor() as cursor:
                self._finish_run(
                    cursor, run_id, "SUCCESS",
                    received=processed,
                    written=lineup_rows + timeline_rows + stat_rows,
                )
            connection.commit()

        return BackfillSummary(
            fixtures_processed=processed,
            fixtures_with_lineup=with_lineup,
            lineup_rows=lineup_rows,
            timeline_rows=timeline_rows,
            stat_rows=stat_rows,
            fixtures_no_data=no_data,
            fixtures_error=errors,
        )

    # ==================================================================
    # 3. Contexte evenement (meteo, affluence, ville/pays du stade)
    # ==================================================================
    def backfill_event_context(self, connection, limit: int | None = None) -> ContextSummary:
        with connection.cursor() as cursor:
            run_id = self._start_run(cursor, "EVENT_CONTEXT")
            connection.commit()
            cursor.execute(
                f"""
                SELECT f.fixture_id, f.thesportsdb_event_id
                FROM core.fixtures f
                LEFT JOIN ops.event_context_progress p ON p.fixture_id = f.fixture_id
                WHERE f.thesportsdb_event_id > 0
                  AND p.fixture_id IS NULL
                ORDER BY
                  (f.kickoff_utc >= now()) DESC,      -- matchs futurs d'abord (geo pour la meteo previsionnelle)
                  f.kickoff_utc DESC NULLS LAST       -- puis les plus recents
                {'LIMIT %(limit)s' if limit else ''}
                """,
                {"limit": limit},
            )
            fixtures = [(int(r[0]), int(r[1])) for r in cursor.fetchall()]

        processed = with_context = weather_n = attendance_n = no_data = errors = 0
        try:
            for fixture_id, tsdb_event_id in fixtures:
                with connection.cursor() as cursor:
                    touch_run(cursor, run_id)
                    cursor.execute("SAVEPOINT event_context")
                    try:
                        payload = self._client.get_json(
                            "lookupevent.php", {"id": str(tsdb_event_id)}
                        )
                        events = payload.get("events") or []
                        event = events[0] if events and isinstance(events[0], dict) else {}
                        context = extract_event_context(event)

                        # Lien stade : eventsseason ne porte pas idVenue, seule
                        # la fiche complete le donne -> on repare venue_id ici.
                        venue = extract_event_venue(event)
                        venue_id = self._ensure_venue(
                            cursor,
                            venue.get("thesportsdb_venue_id"),
                            venue.get("venue_name"),
                            context.get("venue_city"),
                            context.get("venue_country"),
                        )
                        if venue_id is not None:
                            cursor.execute(
                                "UPDATE core.fixtures SET venue_id = %s WHERE fixture_id = %s",
                                (venue_id, fixture_id),
                            )

                        if context:
                            assignments = ", ".join(f"{col} = %({col})s" for col in context)
                            cursor.execute(
                                f"UPDATE core.fixtures SET {assignments} WHERE fixture_id = %(fixture_id)s",
                                {**context, "fixture_id": fixture_id},
                            )
                            with_context += 1
                            weather_n += 1 if "weather_text" in context else 0
                            attendance_n += 1 if "attendance" in context else 0
                        status = "DONE" if (context or venue_id is not None) else "NO_DATA"
                        if status == "NO_DATA":
                            no_data += 1
                        cursor.execute(
                            """
                            INSERT INTO ops.event_context_progress (fixture_id, status_code, fields_filled)
                            VALUES (%s, %s, %s)
                            ON CONFLICT (fixture_id) DO UPDATE
                            SET status_code = EXCLUDED.status_code,
                                fields_filled = EXCLUDED.fields_filled,
                                processed_at = now()
                            """,
                            (fixture_id, status, len(context)),
                        )
                    except Exception as exc:
                        cursor.execute("ROLLBACK TO SAVEPOINT event_context")
                        errors += 1
                        cursor.execute(
                            """
                            INSERT INTO ops.event_context_progress (fixture_id, status_code, error_message)
                            VALUES (%s, 'ERROR', %s)
                            ON CONFLICT (fixture_id) DO UPDATE
                            SET status_code = 'ERROR',
                                error_message = EXCLUDED.error_message,
                                processed_at = now()
                            """,
                            (fixture_id, str(exc)[:400]),
                        )
                    else:
                        cursor.execute("RELEASE SAVEPOINT event_context")
                    processed += 1
                connection.commit()
        finally:
            with connection.cursor() as cursor:
                self._finish_run(cursor, run_id, "SUCCESS", received=processed, written=with_context)
            connection.commit()

        return ContextSummary(
            fixtures_processed=processed,
            fixtures_with_context=with_context,
            weather_filled=weather_n,
            attendance_filled=attendance_n,
            fixtures_no_data=no_data,
            fixtures_error=errors,
        )

    def _ensure_venue(
        self,
        cursor,
        tsdb_venue_id: int | None,
        venue_name: str | None,
        city: str | None,
        country: str | None,
    ) -> int | None:
        """venue_id interne depuis la fiche evenement — retrouve par ID
        TheSportsDB, sinon par nom, sinon cree le stade."""
        if tsdb_venue_id:
            cursor.execute(
                "SELECT venue_id FROM core.venues WHERE thesportsdb_venue_id = %s",
                (tsdb_venue_id,),
            )
            row = cursor.fetchone()
            if row:
                return int(row[0])
        if venue_name:
            cursor.execute(
                "SELECT venue_id FROM core.venues WHERE lower(venue_name) = lower(%s) LIMIT 1",
                (venue_name,),
            )
            row = cursor.fetchone()
            if row:
                return int(row[0])
        if not venue_name:
            return None
        cursor.execute(
            """
            INSERT INTO core.venues (
                thesportsdb_venue_id, venue_name, city_name, country_name, last_synced_at
            )
            VALUES (%s, %s, %s, %s, now())
            RETURNING venue_id
            """,
            (tsdb_venue_id, venue_name, city, country),
        )
        return int(cursor.fetchone()[0])

    def _load_pending_backfill(self, cursor, limit: int | None, international_only: bool):
        domestic_clause = ""
        if international_only:
            domestic_clause = "AND NOT (l.league_name = ANY(%(domestic)s))"
        cursor.execute(
            f"""
            SELECT f.fixture_id, f.thesportsdb_event_id, f.home_team_id, f.away_team_id
            FROM core.fixtures f
            JOIN core.leagues l ON l.league_id = f.league_id
            JOIN core.fixture_scores fs ON fs.fixture_id = f.fixture_id
            LEFT JOIN ops.player_backfill_progress p ON p.fixture_id = f.fixture_id
            WHERE f.thesportsdb_event_id > 0
              AND fs.home_score IS NOT NULL
              AND p.fixture_id IS NULL
              {domestic_clause}
            ORDER BY
              (l.league_name = ANY(%(domestic)s)) ASC,  -- internationaux d'abord
              f.kickoff_utc DESC NULLS LAST             -- recents d'abord
            {'LIMIT %(limit)s' if limit else ''}
            """,
            {"domestic": list(DOMESTIC_LEAGUE_NAMES), "limit": limit},
        )
        return [(int(r[0]), int(r[1]), int(r[2]), int(r[3])) for r in cursor.fetchall()]

    def _ingest_one_fixture(
        self, cursor, fixture_id: int, tsdb_event_id: int, home_team_id: int, away_team_id: int
    ) -> tuple[int, int, int]:
        event_ref = str(tsdb_event_id)

        lineup_payload = self._client.get_json("lookuplineup.php", {"id": event_ref})
        n_lineup = self._ingest_lineup(
            cursor, fixture_id, home_team_id, away_team_id, lineup_payload.get("lineup") or []
        )

        timeline_payload = self._client.get_json("lookuptimeline.php", {"id": event_ref})
        n_timeline = self._ingest_timeline(
            cursor, fixture_id, timeline_payload.get("timeline") or []
        )

        stats_payload = self._client.get_json("lookupeventstats.php", {"id": event_ref})
        n_stats = self._ingest_team_stats(
            cursor, fixture_id, stats_payload.get("eventstats") or []
        )
        return n_lineup, n_timeline, n_stats

    def _ingest_lineup(self, cursor, fixture_id: int, home_team_id: int, away_team_id: int, rows: list) -> int:
        written = 0
        for row in rows:
            if not isinstance(row, dict):
                continue
            player_id = self._resolve_player(cursor, row.get("idPlayer"), row.get("strPlayer"))
            if player_id is None:
                continue
            is_home = _to_bool_yesno(row.get("strHome"))
            cursor.execute(
                """
                INSERT INTO core.fixture_lineups (
                    fixture_id, player_id, team_id, is_home, is_starter,
                    position_code, shirt_number
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (fixture_id, player_id) DO UPDATE
                SET is_starter = EXCLUDED.is_starter,
                    position_code = EXCLUDED.position_code,
                    shirt_number = EXCLUDED.shirt_number
                """,
                (
                    fixture_id,
                    player_id,
                    home_team_id if is_home else away_team_id,
                    is_home,
                    not _to_bool_yesno(row.get("strSubstitute")),
                    _position_code(row.get("strPosition")),
                    _to_int(row.get("intSquadNumber")),
                ),
            )
            written += 1
        return written

    def _ingest_timeline(self, cursor, fixture_id: int, rows: list) -> int:
        written = 0
        for row in rows:
            if not isinstance(row, dict):
                continue
            tsdb_timeline_id = _to_int(row.get("idTimeline"))
            player_id = self._resolve_player(cursor, row.get("idPlayer"), row.get("strPlayer"))
            assist_id = self._resolve_player(cursor, row.get("idAssist"), row.get("strAssist"))
            cursor.execute(
                """
                INSERT INTO core.fixture_timeline (
                    thesportsdb_timeline_id, fixture_id, event_code, event_detail,
                    minute, is_home, player_id, assist_player_id
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (thesportsdb_timeline_id) DO UPDATE
                SET event_code = EXCLUDED.event_code,
                    event_detail = EXCLUDED.event_detail,
                    minute = EXCLUDED.minute,
                    player_id = EXCLUDED.player_id,
                    assist_player_id = EXCLUDED.assist_player_id
                """,
                (
                    tsdb_timeline_id,
                    fixture_id,
                    _timeline_code(row.get("strTimeline")),
                    str(row.get("strTimelineDetail") or "") or None,
                    _to_int(row.get("intTime")),
                    _to_bool_yesno(row.get("strHome")),
                    player_id,
                    assist_id,
                ),
            )
            written += 1
        return written

    def _ingest_team_stats(self, cursor, fixture_id: int, rows: list) -> int:
        written = 0
        for row in rows:
            if not isinstance(row, dict):
                continue
            stat_name = str(row.get("strStat") or "").strip()
            if not stat_name:
                continue
            cursor.execute(
                """
                INSERT INTO core.fixture_team_stats (fixture_id, stat_name, home_value, away_value)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (fixture_id, stat_name) DO UPDATE
                SET home_value = EXCLUDED.home_value,
                    away_value = EXCLUDED.away_value
                """,
                (fixture_id, stat_name, _to_float(row.get("intHome")), _to_float(row.get("intAway"))),
            )
            written += 1
        return written

    # ==================================================================
    # Resolution / upsert joueurs
    # ==================================================================
    def _resolve_player(self, cursor, tsdb_player_id: Any, player_name: Any) -> int | None:
        """player_id interne depuis un idPlayer TheSportsDB (creation minimale
        si inconnu — le roster enrichira plus tard)."""
        pid = _to_int(tsdb_player_id)
        if pid is None:
            return None
        cached = self._player_cache.get(pid)
        if cached is not None:
            return cached
        name = str(player_name or "").strip() or f"Player {pid}"
        cursor.execute(
            """
            INSERT INTO core.players (thesportsdb_player_id, player_name, last_synced_at)
            VALUES (%s, %s, now())
            ON CONFLICT (thesportsdb_player_id) WHERE thesportsdb_player_id IS NOT NULL
            DO UPDATE SET player_name = COALESCE(core.players.player_name, EXCLUDED.player_name)
            RETURNING player_id
            """,
            (pid, name),
        )
        player_id = int(cursor.fetchone()[0])
        self._player_cache[pid] = player_id
        return player_id

    def _upsert_player_full(self, cursor, player: dict[str, Any]) -> int | None:
        """Upsert roster complet : pont TheSportsDB + pont API-Football."""
        tsdb_id = _to_int(player.get("idPlayer"))
        name = str(player.get("strPlayer") or "").strip()
        if tsdb_id is None or not name:
            return None
        af_id = _to_int(player.get("idAPIfootball"))

        # 1. Si le joueur existe deja via API-Football, on complete le pont.
        if af_id is not None:
            cursor.execute(
                """
                UPDATE core.players
                SET thesportsdb_player_id = COALESCE(thesportsdb_player_id, %s),
                    birth_date = COALESCE(birth_date, %s),
                    nationality = COALESCE(nationality, %s),
                    position_code = COALESCE(position_code, %s),
                    last_synced_at = now()
                WHERE apifootball_player_id = %s
                RETURNING player_id
                """,
                (
                    tsdb_id,
                    _to_date(player.get("dateBorn")),
                    str(player.get("strNationality") or "") or None,
                    _position_code(player.get("strPosition")),
                    af_id,
                ),
            )
            row = cursor.fetchone()
            if row:
                player_id = int(row[0])
                self._player_cache[tsdb_id] = player_id
                return player_id

        # 2. Sinon upsert par pont TheSportsDB.
        cursor.execute(
            """
            INSERT INTO core.players (
                thesportsdb_player_id, apifootball_player_id, player_name,
                birth_date, nationality, position_code, photo_url, last_synced_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (thesportsdb_player_id) WHERE thesportsdb_player_id IS NOT NULL
            DO UPDATE SET
                player_name = EXCLUDED.player_name,
                apifootball_player_id = COALESCE(core.players.apifootball_player_id, EXCLUDED.apifootball_player_id),
                birth_date = COALESCE(EXCLUDED.birth_date, core.players.birth_date),
                nationality = COALESCE(EXCLUDED.nationality, core.players.nationality),
                position_code = COALESCE(EXCLUDED.position_code, core.players.position_code),
                last_synced_at = now()
            RETURNING player_id
            """,
            (
                tsdb_id,
                af_id,
                name,
                _to_date(player.get("dateBorn")),
                str(player.get("strNationality") or "") or None,
                _position_code(player.get("strPosition")),
                player.get("strThumb") or player.get("strCutout"),
            ),
        )
        player_id = int(cursor.fetchone()[0])
        self._player_cache[tsdb_id] = player_id
        return player_id

    # ==================================================================
    # Journalisation
    # ==================================================================
    def _start_run(self, cursor, scope: str) -> str:
        cursor.execute("SELECT provider_id FROM ops.providers WHERE provider_code = 'THESPORTSDB'")
        row = cursor.fetchone()
        if not row:
            raise RuntimeError("THESPORTSDB provider missing from ops.providers")
        meta = run_metadata({}, default_trigger="BACKFILL" if scope != "PLAYER_ROSTERS" else "MANUAL")
        cursor.execute(
            """
            INSERT INTO ops.ingestion_runs (
                provider_id, run_scope, status_code, started_at, heartbeat_at,
                request_params, request_fingerprint, application_name,
                trigger_source, host_name, process_id
            )
            VALUES (%s, %s, 'RUNNING', now(), now(), %s::jsonb, %s, %s, %s, %s, %s)
            RETURNING ingestion_run_id
            """,
            (
                int(row[0]),
                scope,
                meta["request_params_json"],
                meta["request_fingerprint"],
                meta["application_name"],
                meta["trigger_source"],
                meta["host_name"],
                meta["process_id"],
            ),
        )
        return str(cursor.fetchone()[0])

    def _finish_run(self, cursor, run_id: str, status: str, received: int, written: int) -> None:
        cursor.execute(
            """
            UPDATE ops.ingestion_runs
            SET status_code = %s, finished_at = now(), heartbeat_at = now(),
                records_received = %s, records_written = %s
            WHERE ingestion_run_id = %s
            """,
            (status, received, written, run_id),
        )


# ----------------------------------------------------------------------
# Helpers de parsing (tolerants aux donnees sales — journal E12/E13)
# ----------------------------------------------------------------------

def _timeline_code(value: Any) -> str:
    text = str(value or "").strip().lower()
    if "goal" in text:
        return "GOAL"
    if "card" in text:
        return "CARD"
    if "subst" in text or text == "sub":
        return "SUB"
    if "var" in text:
        return "VAR"
    return "OTHER"


def _to_bool_yesno(value: Any) -> bool:
    return str(value or "").strip().lower() in ("yes", "true", "1")


def _position_code(value: Any) -> str | None:
    text = str(value or "").strip().upper()
    if not text:
        return None
    mapping = {
        "GOALKEEPER": "G",
        "DEFENDER": "D",
        "MIDFIELDER": "M",
        "FORWARD": "F",
        "ATTACKER": "F",
    }
    return mapping.get(text, text[:1])


def _to_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(str(value).strip().replace("%", ""))
    except (TypeError, ValueError):
        return None


def _to_date(value: Any):
    if not value:
        return None
    import datetime as _dt

    try:
        return _dt.date.fromisoformat(str(value).strip())
    except ValueError:
        return None
