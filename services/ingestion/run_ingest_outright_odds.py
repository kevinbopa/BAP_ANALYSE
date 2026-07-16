"""Ingestion des cotes outright (vainqueur de competition).

    py services/ingestion/run_ingest_outright_odds.py

V1 : le vainqueur de la Coupe du monde (soccer_fifa_world_cup_winner chez
The Odds API, ~2 credits par sync). Cree le marche TOURNAMENT_WINNER et ses
selections si besoin (matching des noms d'equipes par la logique partagee),
puis capture la cote de chaque book par candidat.
"""
from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
import sys

sys.path.insert(0, str((Path(__file__).resolve().parent / "src").resolve()))

from spe_ingestion.clients.theoddsapi import TheOddsApiClient
from spe_ingestion.config import TheOddsApiSettings
from spe_ingestion.db import DatabaseSettings, connect_db
from spe_ingestion.odds_matching import compare_team_names
from spe_ingestion.theoddsapi_odds_ingestor import parse_iso_datetime

OUTRIGHT_SPORTS = {
    # sport_key The Odds API -> (league_name en base, market_code)
    "soccer_fifa_world_cup_winner": ("FIFA World Cup", "TOURNAMENT_WINNER"),
}


def _ensure_market(cursor, league_name: str, market_code: str) -> tuple[int, int] | None:
    cursor.execute(
        "SELECT league_id, current_season_name FROM core.leagues WHERE league_name = %s",
        (league_name,),
    )
    row = cursor.fetchone()
    if not row:
        return None
    league_id = int(row[0])
    season_name = str(row[1] or "")
    cursor.execute(
        """
        INSERT INTO core.outright_markets (market_code, league_id, season_name, market_label)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (market_code, league_id, season_name) DO UPDATE
        SET status_code = 'OPEN'
        RETURNING outright_market_id
        """,
        (market_code, league_id, season_name, f"Vainqueur - {league_name} {season_name}".strip()),
    )
    return int(cursor.fetchone()[0]), league_id


def _match_team(cursor, league_id: int, outcome_name: str) -> tuple[int, str] | None:
    cursor.execute(
        """
        SELECT DISTINCT t.team_id, t.team_name
        FROM core.teams t
        JOIN core.fixtures f
          ON t.team_id IN (f.home_team_id, f.away_team_id)
        WHERE f.league_id = %s
        """,
        (league_id,),
    )
    best = None
    best_score = 0
    for team_id, team_name in cursor.fetchall():
        score = compare_team_names(outcome_name, str(team_name))
        if score > best_score:
            best_score = score
            best = (int(team_id), str(team_name))
    return best if best_score > 0 else None


def main() -> int:
    settings = TheOddsApiSettings.from_env()
    client = TheOddsApiClient(settings)
    connection = connect_db(DatabaseSettings.from_env(prefix="POSTGRES_INGEST"))
    summary = {"sports": 0, "selections": 0, "odds_written": 0, "unmatched": []}
    try:
        with connection.cursor() as cursor:
            for sport_key, (league_name, market_code) in OUTRIGHT_SPORTS.items():
                market = _ensure_market(cursor, league_name, market_code)
                if market is None:
                    continue
                market_id, league_id = market
                events = client.get_outrights(sport_key)
                summary["sports"] += 1
                for event in events:
                    for bookmaker in event.get("bookmakers") or []:
                        bookmaker_key = str(bookmaker.get("key") or "").strip()
                        if not bookmaker_key:
                            continue
                        cursor.execute(
                            """
                            INSERT INTO core.bookmakers (bookmaker_code, bookmaker_name, is_active)
                            VALUES (%s, %s, true)
                            ON CONFLICT (bookmaker_code) DO UPDATE SET is_active = true
                            RETURNING bookmaker_id
                            """,
                            (bookmaker_key.upper(), str(bookmaker.get("title") or bookmaker_key)),
                        )
                        bookmaker_id = int(cursor.fetchone()[0])
                        captured_at = (
                            parse_iso_datetime(bookmaker.get("last_update")) or datetime.now(UTC)
                        )
                        for market_payload in bookmaker.get("markets") or []:
                            if str(market_payload.get("key") or "") != "outrights":
                                continue
                            for outcome in market_payload.get("outcomes") or []:
                                name = str(outcome.get("name") or "").strip()
                                price = outcome.get("price")
                                if not name or price is None or float(price) <= 1.0:
                                    continue
                                matched = _match_team(cursor, league_id, name)
                                if matched is None:
                                    if name not in summary["unmatched"]:
                                        summary["unmatched"].append(name)
                                    continue
                                team_id, team_name = matched
                                cursor.execute(
                                    """
                                    INSERT INTO core.outright_selections (
                                        outright_market_id, subject_type, team_id, subject_label
                                    )
                                    VALUES (%s, 'TEAM', %s, %s)
                                    ON CONFLICT (outright_market_id, subject_label) DO UPDATE
                                    SET team_id = EXCLUDED.team_id
                                    RETURNING outright_selection_id
                                    """,
                                    (market_id, team_id, team_name),
                                )
                                selection_id = int(cursor.fetchone()[0])
                                summary["selections"] += 1
                                cursor.execute(
                                    """
                                    INSERT INTO core.outright_odds (
                                        outright_selection_id, bookmaker_id, captured_at,
                                        decimal_odd, source_reference
                                    )
                                    VALUES (%s, %s, %s, %s, %s)
                                    ON CONFLICT (outright_selection_id, bookmaker_id, captured_at)
                                    DO UPDATE SET decimal_odd = EXCLUDED.decimal_odd
                                    """,
                                    (
                                        selection_id, bookmaker_id, captured_at,
                                        float(price), f"{sport_key}|{bookmaker_key}",
                                    ),
                                )
                                summary["odds_written"] += 1
        connection.commit()
    finally:
        connection.close()
        client.close()
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
