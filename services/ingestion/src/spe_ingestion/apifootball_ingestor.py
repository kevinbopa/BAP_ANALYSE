"""API-Football player-layer ingestor.

Ingests, raw-first, for every team in ``core.teams`` that carries an
``apifootball_team_id`` (bridged automatically from TheSportsDB's
``idAPIfootball`` field):

  1. the current squad         → core.players + core.team_squad_members
  2. active injuries           → core.player_injuries
  3. (optional, budget-bound)  per-match player stats of recent fixtures
                               → core.player_match_stats

The request budget is explicit: the ingestor stops BEFORE burning the
daily free-tier quota rather than failing mid-run.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from typing import Any

from spe_ingestion.clients.apifootball import ApiFootballClient
from spe_ingestion.config import ApiFootballSettings

# /injuries?season=Y renvoie TOUT le log de blessures de la saison (historique),
# pas seulement les blesses du moment. On ne marque "actif" (= indisponible
# maintenant) que les entrees dont le match est dans cette fenetre autour
# d'aujourd'hui ; le reste est conserve comme historique (is_active=false).
RECENT_INJURY_DAYS = 14


@dataclass(frozen=True)
class PlayerIngestionSummary:
    teams_processed: int = 0
    players_upserted: int = 0
    squad_memberships: int = 0
    injuries_recorded: int = 0
    match_stats_rows: int = 0
    requests_used: int = 0
    budget_exhausted: bool = False


class ApiFootballIngestor:
    def __init__(self, client: ApiFootballClient, settings: ApiFootballSettings) -> None:
        self._client = client
        self._settings = settings

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------
    def ingest_player_layer(self, connection, with_match_stats: bool = False) -> PlayerIngestionSummary:
        summary = PlayerIngestionSummary()
        with connection.cursor() as cursor:
            provider_id = self._fetch_provider_id(cursor)
            team_rows = self._load_bridged_teams(cursor)

            teams_processed = 0
            players_upserted = 0
            memberships = 0
            injuries = 0
            stats_rows = 0
            exhausted = False

            for team_id, apifootball_team_id in team_rows:
                # Chaque equipe coute 2 requetes (squad + injuries); on verifie
                # le budget AVANT, pour terminer proprement.
                if self._client.requests_made + 2 > self._settings.daily_request_budget:
                    exhausted = True
                    break

                squad_payload = self._client.get_squad(apifootball_team_id)
                self._store_raw(cursor, provider_id, "SQUAD", apifootball_team_id, squad_payload)
                new_players, new_memberships = self._ingest_squad(cursor, team_id, squad_payload)
                players_upserted += new_players
                memberships += new_memberships

                # Le plan Free d'API-Football n'expose que les saisons 2022-2024 :
                # les blessures de la saison courante sont hors plan. On saute
                # proprement plutot que d'echouer — les effectifs restent la
                # valeur principale de la couche gratuite.
                try:
                    injuries_payload = self._client.get_injuries(
                        apifootball_team_id, self._settings.season_year
                    )
                    self._store_raw(cursor, provider_id, "INJURIES", apifootball_team_id, injuries_payload)
                    injuries += self._ingest_injuries(cursor, team_id, injuries_payload)
                except RuntimeError as exc:
                    if "plan" not in str(exc).lower():
                        raise

                if with_match_stats:
                    if self._client.requests_made + 1 > self._settings.daily_request_budget:
                        exhausted = True
                        break
                    stats_rows += self._ingest_recent_match_stats(
                        cursor, provider_id, team_id, apifootball_team_id
                    )

                teams_processed += 1

            summary = PlayerIngestionSummary(
                teams_processed=teams_processed,
                players_upserted=players_upserted,
                squad_memberships=memberships,
                injuries_recorded=injuries,
                match_stats_rows=stats_rows,
                requests_used=self._client.requests_made,
                budget_exhausted=exhausted,
            )
        connection.commit()
        return summary

    # ------------------------------------------------------------------
    # Squad
    # ------------------------------------------------------------------
    def _ingest_squad(self, cursor, team_id: int, payload: dict[str, Any]) -> tuple[int, int]:
        players_upserted = 0
        memberships = 0
        for response_item in payload.get("response") or []:
            for player in (response_item.get("players") or []):
                player_id = self._upsert_player(cursor, player)
                if player_id is None:
                    continue
                players_upserted += 1
                cursor.execute(
                    """
                    INSERT INTO core.team_squad_members (
                        team_id, player_id, season_year, competition_name,
                        shirt_number, position_code
                    )
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (team_id, player_id, season_year, competition_name)
                    DO UPDATE SET
                        shirt_number = EXCLUDED.shirt_number,
                        position_code = EXCLUDED.position_code
                    """,
                    (
                        team_id,
                        player_id,
                        self._settings.season_year,
                        "DEFAULT",
                        _to_int(player.get("number")),
                        _position_code(player.get("position")),
                    ),
                )
                memberships += 1
        return players_upserted, memberships

    def _upsert_player(self, cursor, player: dict[str, Any]) -> int | None:
        apifootball_player_id = _to_int(player.get("id"))
        name = str(player.get("name") or "").strip()
        if apifootball_player_id is None or not name:
            return None
        cursor.execute(
            """
            INSERT INTO core.players (
                apifootball_player_id, player_name, position_code, photo_url, last_synced_at
            )
            VALUES (%s, %s, %s, %s, now())
            ON CONFLICT (apifootball_player_id) DO UPDATE
            SET player_name = EXCLUDED.player_name,
                position_code = EXCLUDED.position_code,
                photo_url = EXCLUDED.photo_url,
                last_synced_at = now()
            RETURNING player_id
            """,
            (
                apifootball_player_id,
                name,
                _position_code(player.get("position")),
                player.get("photo"),
            ),
        )
        return int(cursor.fetchone()[0])

    # ------------------------------------------------------------------
    # Injuries
    # ------------------------------------------------------------------
    def _ingest_injuries(self, cursor, team_id: int, payload: dict[str, Any]) -> int:
        # Idempotent : on purge le log de blessures de cette equipe pour la
        # saison, puis on reinsere l'etat courant de l'API. Evite l'accumulation
        # de doublons a chaque sync (le premium re-ingere tout le log a chaque run).
        cursor.execute(
            "DELETE FROM core.player_injuries WHERE team_id = %s AND season_year = %s",
            (team_id, self._settings.season_year),
        )
        cutoff = datetime.now(timezone.utc) - timedelta(days=RECENT_INJURY_DAYS)
        recorded = 0
        for item in payload.get("response") or []:
            player = item.get("player") or {}
            apifootball_player_id = _to_int(player.get("id"))
            if apifootball_player_id is None:
                continue
            cursor.execute(
                "SELECT player_id FROM core.players WHERE apifootball_player_id = %s",
                (apifootball_player_id,),
            )
            row = cursor.fetchone()
            if not row:
                continue
            db_player_id = int(row[0])
            fixture = item.get("fixture") or {}
            reported_at = _parse_dt(fixture.get("date"))
            # Actif = blessure signalee dans la fenetre recente (blesse "maintenant").
            is_active = reported_at is not None and reported_at >= cutoff
            cursor.execute(
                """
                INSERT INTO core.player_injuries (
                    player_id, team_id, injury_type, injury_reason,
                    reported_at, fixture_apifootball_id, season_year, is_active
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    db_player_id,
                    team_id,
                    str(player.get("type") or "") or None,
                    str(player.get("reason") or "") or None,
                    reported_at,
                    _to_int(fixture.get("id")),
                    self._settings.season_year,
                    is_active,
                ),
            )
            recorded += 1
        return recorded

    # ------------------------------------------------------------------
    # Per-match player stats (budget-bound)
    # ------------------------------------------------------------------
    def _ingest_recent_match_stats(
        self, cursor, provider_id: int, team_id: int, apifootball_team_id: int
    ) -> int:
        try:
            fixtures_payload = self._client.get_team_fixtures(
                apifootball_team_id, self._settings.season_year, last=5
            )
        except RuntimeError as exc:
            if "plan" in str(exc).lower():
                return 0
            raise
        self._store_raw(cursor, provider_id, "TEAM_FIXTURES", apifootball_team_id, fixtures_payload)
        rows_written = 0
        for item in fixtures_payload.get("response") or []:
            if self._client.requests_made + 1 > self._settings.daily_request_budget:
                break
            fixture_info = (item.get("fixture") or {})
            fixture_af_id = _to_int(fixture_info.get("id"))
            if fixture_af_id is None:
                continue
            players_payload = self._client.get_fixture_players(fixture_af_id)
            self._store_raw(cursor, provider_id, "FIXTURE_PLAYERS", fixture_af_id, players_payload)
            rows_written += self._ingest_fixture_players(
                cursor, team_id, fixture_af_id, _parse_dt(fixture_info.get("date")), players_payload
            )
        return rows_written

    def _ingest_fixture_players(
        self, cursor, team_id: int, fixture_af_id: int, kickoff, payload: dict[str, Any]
    ) -> int:
        written = 0
        for team_block in payload.get("response") or []:
            for player_entry in team_block.get("players") or []:
                player = player_entry.get("player") or {}
                stats_list = player_entry.get("statistics") or []
                if not stats_list:
                    continue
                stats = stats_list[0]
                games = stats.get("games") or {}
                apifootball_player_id = _to_int(player.get("id"))
                if apifootball_player_id is None:
                    continue
                cursor.execute(
                    "SELECT player_id FROM core.players WHERE apifootball_player_id = %s",
                    (apifootball_player_id,),
                )
                row = cursor.fetchone()
                if not row:
                    continue
                db_player_id = int(row[0])
                goals = stats.get("goals") or {}
                shots = stats.get("shots") or {}
                passes = stats.get("passes") or {}
                tackles = stats.get("tackles") or {}
                duels = stats.get("duels") or {}
                dribbles = stats.get("dribbles") or {}
                fouls = stats.get("fouls") or {}
                cards = stats.get("cards") or {}
                cursor.execute(
                    """
                    INSERT INTO core.player_match_stats (
                        apifootball_fixture_id, player_id, team_id, kickoff_utc,
                        minutes_played, rating, position_code, is_starter, is_captain,
                        goals, assists, shots_total, shots_on_target,
                        passes_total, passes_accuracy, tackles_total, interceptions,
                        duels_total, duels_won, dribbles_success,
                        fouls_committed, yellow_cards, red_cards
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (player_id, apifootball_fixture_id) DO UPDATE
                    SET minutes_played = EXCLUDED.minutes_played,
                        rating = EXCLUDED.rating,
                        kickoff_utc = EXCLUDED.kickoff_utc
                    """,
                    (
                        fixture_af_id,
                        db_player_id,
                        team_id,
                        kickoff,
                        _to_int(games.get("minutes")),
                        _to_float(games.get("rating")),
                        _position_code(games.get("position")),
                        bool(games.get("substitute")) is False,
                        bool(games.get("captain")),
                        _to_int(goals.get("total")),
                        _to_int(goals.get("assists")),
                        _to_int(shots.get("total")),
                        _to_int(shots.get("on")),
                        _to_int(passes.get("total")),
                        _to_float(passes.get("accuracy")),
                        _to_int(tackles.get("total")),
                        _to_int(tackles.get("interceptions")),
                        _to_int(duels.get("total")),
                        _to_int(duels.get("won")),
                        _to_int(dribbles.get("success")),
                        _to_int(fouls.get("committed")),
                        _to_int(cards.get("yellow")),
                        _to_int(cards.get("red")),
                    ),
                )
                written += 1
        return written

    # ------------------------------------------------------------------
    # Plumbing
    # ------------------------------------------------------------------
    def _fetch_provider_id(self, cursor) -> int:
        cursor.execute("SELECT provider_id FROM ops.providers WHERE provider_code = 'APIFOOTBALL'")
        row = cursor.fetchone()
        if not row:
            raise RuntimeError("APIFOOTBALL provider missing — apply migration 0008 first")
        return int(row[0])

    def _load_bridged_teams(self, cursor) -> list[tuple[int, int]]:
        cursor.execute(
            """
            SELECT team_id, apifootball_team_id
            FROM core.teams
            WHERE apifootball_team_id IS NOT NULL
            ORDER BY last_synced_at DESC NULLS LAST, team_id
            """
        )
        return [(int(r[0]), int(r[1])) for r in cursor.fetchall()]

    def _store_raw(self, cursor, provider_id: int, object_type: str, object_id: Any, payload: dict[str, Any]) -> None:
        payload_text = json.dumps(payload, sort_keys=True, ensure_ascii=True)
        checksum = sha256(payload_text.encode("utf-8")).hexdigest()
        cursor.execute(
            """
            INSERT INTO raw.provider_payloads (
                provider_id, provider_object_type, provider_object_id,
                natural_key, payload, payload_checksum
            )
            VALUES (%s, %s, %s, %s, %s::jsonb, %s)
            """,
            (
                provider_id,
                object_type,
                str(object_id),
                f"apifootball:{object_type.lower()}:{object_id}",
                payload_text,
                checksum,
            ),
        )


def _position_code(value: Any) -> str | None:
    text = str(value or "").strip().upper()
    if not text:
        return None
    mapping = {
        "GOALKEEPER": "G", "G": "G",
        "DEFENDER": "D", "D": "D",
        "MIDFIELDER": "M", "M": "M",
        "ATTACKER": "F", "F": "F", "FORWARD": "F",
    }
    return mapping.get(text, text[:1])


def _to_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_dt(value: Any):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None
