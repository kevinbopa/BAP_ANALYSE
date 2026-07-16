"""Validation legere du Back.

Ce script ne cherche PAS de nouveaux deals et ne relance PAS les modeles.
Il recupere seulement les resultats utiles aux paris deja pris puis lance le
reglement. Usage:

    py services/prediction/run_validate_back.py
    py services/prediction/run_validate_back.py --sport football
    py services/prediction/run_validate_back.py --sport golf
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str((ROOT / "services" / "prediction" / "src").resolve()))
sys.path.insert(0, str((ROOT / "services" / "ingestion" / "src").resolve()))

from spe_ingestion.clients.datagolf import DataGolfClient, DataGolfError
from spe_ingestion.clients.thesportsdb import TheSportsDBClient
from spe_ingestion.config import DataGolfSettings, TheSportsDBSettings
from spe_ingestion.db import DatabaseSettings as IngestDatabaseSettings
from spe_ingestion.db import connect_db as connect_ingest_db
from spe_prediction.db import DatabaseSettings as PredictionDatabaseSettings
from spe_prediction.db import connect_db as connect_prediction_db
from spe_prediction.golf_settlement import flat_profit, parse_position, settle_matchup, settle_outright
from spe_prediction.settlement import settle_pending_bets


def _to_int(value: Any) -> int | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() == "none":
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


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


def _open_football_events(cursor) -> list[dict[str, Any]]:
    """Fixtures strictement necessaires au Back: tickets ouverts + parlays."""
    cursor.execute(
        """
        SELECT DISTINCT f.fixture_id, f.thesportsdb_event_id,
               f.home_team_id, f.away_team_id, f.status_code
        FROM core.fixtures f
        WHERE f.kickoff_utc <= now()
          AND f.thesportsdb_event_id IS NOT NULL
          AND (
              EXISTS (
                  SELECT 1
                  FROM model.user_bet_positions p
                  WHERE p.fixture_id = f.fixture_id
                    AND p.bet_kind IN ('DEAL', 'PRONOSTIC')
              )
              OR EXISTS (
                  SELECT 1
                  FROM model.parlay_legs pl
                  JOIN model.parlay_tickets pt ON pt.parlay_id = pl.parlay_id
                  WHERE pl.fixture_id = f.fixture_id
                    AND pt.result_code IS NULL
              )
          )
          AND NOT EXISTS (
              SELECT 1
              FROM core.fixture_scores fs
              WHERE fs.fixture_id = f.fixture_id
                AND fs.home_score IS NOT NULL
                AND fs.away_score IS NOT NULL
          )
        ORDER BY f.fixture_id
        LIMIT 80
        """
    )
    return [
        {
            "fixture_id": int(fid),
            "event_id": int(event_id),
            "home_team_id": int(home_id),
            "away_team_id": int(away_id),
            "status_code": status,
        }
        for fid, event_id, home_id, away_id, status in cursor.fetchall()
    ]


def _upsert_football_event(cursor, fixture: dict[str, Any], event: dict[str, Any]) -> bool:
    home_score = _to_int(event.get("intHomeScore"))
    away_score = _to_int(event.get("intAwayScore"))
    status = event.get("strStatus")
    cursor.execute(
        """
        UPDATE core.fixtures
        SET status_code = COALESCE(%s, status_code),
            status_text = COALESCE(%s, status_text),
            raw_last_snapshot_at = now(),
            last_synced_at = now()
        WHERE fixture_id = %s
        """,
        (status, status, fixture["fixture_id"]),
    )
    if home_score is None or away_score is None:
        return False
    cursor.execute(
        """
        INSERT INTO core.fixture_scores (
            fixture_id, home_score, away_score, winner_code, score_status
        )
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (fixture_id) DO UPDATE
        SET home_score = EXCLUDED.home_score,
            away_score = EXCLUDED.away_score,
            winner_code = EXCLUDED.winner_code,
            score_status = EXCLUDED.score_status,
            updated_at = now()
        """,
        (
            fixture["fixture_id"],
            home_score,
            away_score,
            _winner_code(home_score, away_score),
            status,
        ),
    )
    return True


def validate_football_results() -> dict[str, Any]:
    report = {"candidates": 0, "events_found": 0, "scores_written": 0, "errors": []}
    settings = TheSportsDBSettings.from_env()
    read_connection = connect_prediction_db(PredictionDatabaseSettings.from_env())
    write_connection = connect_ingest_db(IngestDatabaseSettings.from_env(prefix="POSTGRES_INGEST"))
    client = TheSportsDBClient(settings)
    try:
        with read_connection.cursor() as cursor:
            fixtures = _open_football_events(cursor)

        with write_connection.cursor() as cursor:
            report["candidates"] = len(fixtures)
            for fixture in fixtures:
                try:
                    payload = client.get_json("lookupevent.php", {"id": str(fixture["event_id"])})
                except Exception as exc:  # API indisponible ou event synthetique.
                    report["errors"].append(f"fixture {fixture['fixture_id']}: {exc}")
                    continue
                events = payload.get("events") or []
                if not events:
                    continue
                report["events_found"] += 1
                if _upsert_football_event(cursor, fixture, events[0]):
                    report["scores_written"] += 1
        write_connection.commit()
    finally:
        read_connection.close()
        write_connection.close()
        client.close()
    return report


def _open_golf_tournaments(cursor) -> list[dict[str, Any]]:
    cursor.execute(
        """
        SELECT DISTINCT gt.golf_tournament_id, gt.tour_code, gt.dg_event_id
        FROM core.golf_tournaments gt
        WHERE gt.tour_code IS NOT NULL
          AND gt.completed_at IS NULL
          AND (
              EXISTS (
                  SELECT 1
                  FROM model.user_bet_positions p
                  JOIN model.golf_deals gd ON gd.golf_deal_id = p.golf_deal_id
                  WHERE p.bet_kind = 'GOLF'
                    AND gd.golf_tournament_id = gt.golf_tournament_id
                    AND gd.result_code IS NULL
              )
              OR EXISTS (
                  SELECT 1
                  FROM model.user_bet_positions p
                  JOIN model.golf_matchup_deals gmd
                    ON gmd.golf_matchup_deal_id = p.golf_matchup_deal_id
                  WHERE p.bet_kind = 'GOLF'
                    AND gmd.golf_tournament_id = gt.golf_tournament_id
                    AND gmd.result_code IS NULL
              )
          )
        ORDER BY gt.golf_tournament_id
        """
    )
    return [
        {"tournament_id": int(tid), "tour": str(tour), "dg_event_id": int(dg) if dg is not None else None}
        for tid, tour, dg in cursor.fetchall()
    ]


def _event_id_from_in_play(payload: dict[str, Any]) -> int | None:
    info = payload.get("info") or {}
    for key in ("event_id", "dg_event_id", "tournament_id"):
        value = _to_int(info.get(key))
        if value is not None:
            return value
    return None


def _ingest_golf_in_play(cursor, client: DataGolfClient, tournament: dict[str, Any]) -> int:
    payload = client.get_in_play(tournament["tour"])
    payload_event_id = _event_id_from_in_play(payload)
    if payload_event_id is not None and tournament.get("dg_event_id") not in (None, payload_event_id):
        return 0
    rnd = (payload.get("info") or {}).get("current_round")
    written = 0
    for row in payload.get("data") or []:
        dg_id = _to_int(row.get("dg_id"))
        if dg_id is None:
            continue
        rank, made_cut = parse_position(row.get("current_pos"))
        cursor.execute(
            """
            INSERT INTO core.golf_results (
                golf_tournament_id, golf_player_id, dg_id, position_text,
                position_rank, made_cut, current_round, updated_at
            )
            VALUES (
                %s,
                (SELECT golf_player_id FROM core.golf_players WHERE dg_id = %s),
                %s, %s, %s, %s, %s, now()
            )
            ON CONFLICT (golf_tournament_id, dg_id) DO UPDATE
            SET golf_player_id = COALESCE(EXCLUDED.golf_player_id, core.golf_results.golf_player_id),
                position_text = EXCLUDED.position_text,
                position_rank = EXCLUDED.position_rank,
                made_cut = EXCLUDED.made_cut,
                current_round = EXCLUDED.current_round,
                updated_at = now()
            """,
            (
                tournament["tournament_id"], dg_id, dg_id,
                str(row.get("current_pos") or ""), rank, made_cut, rnd,
            ),
        )
        written += 1
    return written


def validate_golf_results() -> dict[str, Any]:
    report = {"tournaments": 0, "tours_checked": 0, "results_written": 0, "errors": []}
    settings = DataGolfSettings.from_env()
    if not settings.enabled:
        report["errors"].append("DATAGOLF_API_KEY manquant")
        return report
    read_connection = connect_prediction_db(PredictionDatabaseSettings.from_env())
    write_connection = connect_ingest_db(IngestDatabaseSettings.from_env(prefix="POSTGRES_INGEST"))
    client = DataGolfClient(settings)
    try:
        with read_connection.cursor() as cursor:
            tournaments = _open_golf_tournaments(cursor)

        with write_connection.cursor() as cursor:
            report["tournaments"] = len(tournaments)
            seen_tours: set[str] = set()
            for tournament in tournaments:
                try:
                    if tournament["tour"] not in seen_tours:
                        seen_tours.add(tournament["tour"])
                        report["tours_checked"] += 1
                    report["results_written"] += _ingest_golf_in_play(cursor, client, tournament)
                except DataGolfError as exc:
                    report["errors"].append(f"{tournament['tour']}: {exc}")
                except Exception as exc:
                    report["errors"].append(f"{tournament['tour']}: {exc}")
        write_connection.commit()
    finally:
        read_connection.close()
        write_connection.close()
        client.close()
    return report


def settle_after_validation() -> dict[str, Any]:
    connection = connect_prediction_db(PredictionDatabaseSettings.from_env())
    try:
        football = settle_pending_bets(connection)
    finally:
        connection.close()

    golf = settle_golf_pending()
    return {"football": football, "golf": golf}


def settle_golf_pending() -> dict[str, int]:
    report = {"tournaments_settled": 0, "outright_settled": 0, "matchup_settled": 0}
    connection = connect_prediction_db(PredictionDatabaseSettings.from_env())
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT gt.golf_tournament_id
                FROM core.golf_tournaments gt
                WHERE gt.completed_at IS NULL
                  AND gt.date_end IS NOT NULL AND gt.date_end < current_date
                  AND EXISTS (SELECT 1 FROM core.golf_results r
                              WHERE r.golf_tournament_id = gt.golf_tournament_id)
                """
            )
            tids = [int(r[0]) for r in cursor.fetchall()]
            for tid in tids:
                cursor.execute(
                    "SELECT golf_player_id, position_rank, made_cut FROM core.golf_results "
                    "WHERE golf_tournament_id = %s AND golf_player_id IS NOT NULL",
                    (tid,),
                )
                by_player = {int(pid): (rank, bool(mc)) for pid, rank, mc in cursor.fetchall()}

                cursor.execute(
                    "SELECT golf_deal_id, golf_player_id, market_code, market_odd "
                    "FROM model.golf_deals WHERE golf_tournament_id = %s AND result_code IS NULL",
                    (tid,),
                )
                for did, pid, market, odd in cursor.fetchall():
                    rank, made_cut = by_player.get(int(pid), (None, False)) if pid else (None, False)
                    result = settle_outright(str(market), rank, made_cut)
                    profit = flat_profit(result, float(odd) if odd is not None else None)
                    cursor.execute(
                        "UPDATE model.golf_deals SET result_code = %s, profit_units = %s, "
                        "settled_at = now() WHERE golf_deal_id = %s",
                        (result, profit, did),
                    )
                    report["outright_settled"] += 1

                cursor.execute(
                    "SELECT golf_matchup_deal_id, pick_golf_player_id, opponent_golf_player_id, "
                    "market_odd FROM model.golf_matchup_deals "
                    "WHERE golf_tournament_id = %s AND result_code IS NULL",
                    (tid,),
                )
                for did, pick, opp, odd in cursor.fetchall():
                    pick_rank = by_player.get(int(pick), (None, False))[0] if pick else None
                    opp_rank = by_player.get(int(opp), (None, False))[0] if opp else None
                    result = settle_matchup(pick_rank, opp_rank)
                    profit = flat_profit(result, float(odd) if odd is not None else None)
                    cursor.execute(
                        "UPDATE model.golf_matchup_deals SET result_code = %s, profit_units = %s, "
                        "settled_at = now() WHERE golf_matchup_deal_id = %s",
                        (result, profit, did),
                    )
                    report["matchup_settled"] += 1

                cursor.execute(
                    "UPDATE core.golf_tournaments SET completed_at = now() WHERE golf_tournament_id = %s",
                    (tid,),
                )
                report["tournaments_settled"] += 1
        connection.commit()
    finally:
        connection.close()
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Valide uniquement les paris ouverts du Back.")
    parser.add_argument("--sport", choices=("all", "football", "golf"), default="all")
    args = parser.parse_args()

    report: dict[str, Any] = {"sport": args.sport}
    if args.sport in ("all", "football"):
        report["football_refresh"] = validate_football_results()
    if args.sport in ("all", "golf"):
        report["golf_refresh"] = validate_golf_results()
    report["settlement"] = settle_after_validation()
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
