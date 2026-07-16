"""Ingestion Golf V1 depuis The Odds API.

    py services/ingestion/run_ingest_golf_odds.py

La V1 ingere les marches golf disponibles chez les bookmakers configures.
Si aucun bookmaker n'est filtre dans .env, on garde tous les books pour
construire un consensus robuste.
"""
from __future__ import annotations

from datetime import UTC, datetime
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str((Path(__file__).resolve().parent / "src").resolve()))

from spe_ingestion.clients.theoddsapi import TheOddsApiClient
from spe_ingestion.config import TheOddsApiSettings
from spe_ingestion.db import DatabaseSettings, connect_db
from spe_ingestion.theoddsapi_odds_ingestor import parse_iso_datetime


def _csv_env(name: str, default: str = "") -> tuple[str, ...]:
    raw = os.getenv(name, default)
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _golf_bookmakers() -> str:
    for name in ("THEODDS_API_GOLF_BOOKMAKERS", "THEODDS_API_BOOKMAKERS"):
        value = os.getenv(name)
        if value and value.strip():
            return value.strip()
    return ""


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return value.strip().lower() in {"1", "true", "yes", "oui"}


def _market_code(raw_key: str) -> str | None:
    key = raw_key.lower().replace("-", "_")
    if key == "outrights" or "winner" in key:
        return "TOURNAMENT_WINNER"
    if "top_3" in key or "top3" in key:
        return "TOP_3"
    if "top_5" in key or "top5" in key:
        return "TOP_5"
    if "top_10" in key or "top10" in key:
        return "TOP_10"
    if "top_20" in key or "top20" in key:
        return "TOP_20"
    if "round" in key and "winner" in key:
        return "ROUND_WINNER"
    if "make_cut" in key or "cut" in key:
        return "MAKE_CUT"
    if "position" in key or "finish" in key:
        return "POSITION_FINISH"
    return None


def _event_name(event: dict, sport_key: str) -> str:
    for field in ("title", "home_team", "sport_title"):
        value = str(event.get(field) or "").strip()
        if value:
            return value
    return sport_key.replace("_", " ").title()


def _discover_golf_sports(client: TheOddsApiClient) -> tuple[str, ...]:
    configured = _csv_env("THEODDS_API_GOLF_SPORT_KEYS")
    if configured:
        return configured
    sports = client.get_sports()
    keys = []
    for sport in sports:
        key = str(sport.get("key") or "")
        group = str(sport.get("group") or "").lower()
        title = str(sport.get("title") or "").lower()
        if sport.get("has_outrights") and (group == "golf" or key.startswith("golf_") or "golf" in title):
            keys.append(key)
    return tuple(sorted(set(keys)))


def main() -> int:
    settings = TheOddsApiSettings.from_env()
    client = TheOddsApiClient(settings)
    connection = connect_db(DatabaseSettings.from_env(prefix="POSTGRES_INGEST"))
    markets = ",".join(_csv_env("THEODDS_API_GOLF_MARKETS", "outrights"))
    bookmakers = _golf_bookmakers()
    fetch_participants = _bool_env("THEODDS_API_GOLF_FETCH_PARTICIPANTS", True)
    summary = {
        "sports": 0,
        "events": 0,
        "participants_loaded": 0,
        "players": 0,
        "odds_written": 0,
        "bookmakers": bookmakers or "all",
        "markets": markets,
        "skipped_markets": [],
    }
    try:
        sport_keys = _discover_golf_sports(client)
        with connection.cursor() as cursor:
            for sport_key in sport_keys:
                if fetch_participants:
                    try:
                        participants = client.get_participants(sport_key)
                    except Exception:
                        participants = []
                    for participant in participants:
                        name = str(
                            participant.get("full_name")
                            or participant.get("name")
                            or participant.get("display_name")
                            or ""
                        ).strip()
                        if not name:
                            continue
                        participant_id = str(participant.get("id") or "").strip() or None
                        cursor.execute(
                            """
                            INSERT INTO core.golf_players (
                                player_name, provider_participant_id, country_code, raw_player_json
                            )
                            VALUES (%s, %s, %s, %s::jsonb)
                            ON CONFLICT (player_name, provider_participant_id) DO UPDATE
                            SET country_code = COALESCE(EXCLUDED.country_code, core.golf_players.country_code),
                                raw_player_json = EXCLUDED.raw_player_json,
                                updated_at = now()
                            """,
                            (
                                name,
                                participant_id,
                                str(participant.get("country") or participant.get("country_code") or "").strip() or None,
                                json.dumps(participant, ensure_ascii=True),
                            ),
                        )
                        summary["participants_loaded"] += 1
                events = client.get_outrights(sport_key, markets=markets, bookmakers=bookmakers)
                summary["sports"] += 1
                for event in events:
                    event_id = str(event.get("id") or "").strip()
                    if not event_id:
                        event_id = f"{sport_key}:{_event_name(event, sport_key)}:{event.get('commence_time') or ''}"
                    tournament_name = _event_name(event, sport_key)
                    commence_time = parse_iso_datetime(event.get("commence_time"))
                    venue = event.get("venue") if isinstance(event.get("venue"), dict) else {}
                    cursor.execute(
                        """
                        INSERT INTO core.golf_tournaments (
                            provider_event_id, sport_key, tournament_name, sport_title,
                            commence_time, venue_name, venue_city, venue_country,
                            venue_lat, venue_lon, raw_event_json
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                        ON CONFLICT (provider_event_id) DO UPDATE
                        SET sport_key = EXCLUDED.sport_key,
                            tournament_name = EXCLUDED.tournament_name,
                            sport_title = EXCLUDED.sport_title,
                            commence_time = EXCLUDED.commence_time,
                            venue_name = COALESCE(EXCLUDED.venue_name, core.golf_tournaments.venue_name),
                            venue_city = COALESCE(EXCLUDED.venue_city, core.golf_tournaments.venue_city),
                            venue_country = COALESCE(EXCLUDED.venue_country, core.golf_tournaments.venue_country),
                            venue_lat = COALESCE(EXCLUDED.venue_lat, core.golf_tournaments.venue_lat),
                            venue_lon = COALESCE(EXCLUDED.venue_lon, core.golf_tournaments.venue_lon),
                            raw_event_json = EXCLUDED.raw_event_json,
                            updated_at = now()
                        RETURNING golf_tournament_id
                        """,
                        (
                            event_id,
                            sport_key,
                            tournament_name,
                            str(event.get("sport_title") or ""),
                            commence_time,
                            str(venue.get("name") or event.get("venue_name") or "").strip() or None,
                            str(venue.get("city") or event.get("venue_city") or "").strip() or None,
                            str(venue.get("country") or event.get("venue_country") or "").strip() or None,
                            venue.get("lat") or venue.get("latitude"),
                            venue.get("lon") or venue.get("longitude"),
                            json.dumps(event, ensure_ascii=True),
                        ),
                    )
                    tournament_id = int(cursor.fetchone()[0])
                    summary["events"] += 1

                    for bookmaker in event.get("bookmakers") or []:
                        bookmaker_key = str(bookmaker.get("key") or "").strip()
                        if not bookmaker_key:
                            continue
                        cursor.execute(
                            """
                            INSERT INTO core.bookmakers (bookmaker_code, bookmaker_name, is_active)
                            VALUES (%s, %s, true)
                            ON CONFLICT (bookmaker_code) DO UPDATE
                            SET bookmaker_name = EXCLUDED.bookmaker_name,
                                is_active = true,
                                updated_at = now()
                            RETURNING bookmaker_id
                            """,
                            (bookmaker_key.upper(), str(bookmaker.get("title") or bookmaker_key)),
                        )
                        bookmaker_id = int(cursor.fetchone()[0])
                        captured_at = parse_iso_datetime(bookmaker.get("last_update")) or datetime.now(UTC)
                        for market in bookmaker.get("markets") or []:
                            raw_market_key = str(market.get("key") or "")
                            market_code = _market_code(raw_market_key)
                            if market_code is None:
                                if raw_market_key not in summary["skipped_markets"]:
                                    summary["skipped_markets"].append(raw_market_key)
                                continue
                            for outcome in market.get("outcomes") or []:
                                selection_name = str(outcome.get("name") or "").strip()
                                price = outcome.get("price")
                                if not selection_name or price is None or float(price) <= 1.0:
                                    continue
                                cursor.execute(
                                    """
                                    INSERT INTO core.golf_players (player_name, raw_player_json)
                                    VALUES (%s, %s::jsonb)
                                    ON CONFLICT (player_name, provider_participant_id) DO UPDATE
                                    SET raw_player_json = EXCLUDED.raw_player_json,
                                        updated_at = now()
                                    RETURNING golf_player_id
                                    """,
                                    (selection_name, json.dumps(outcome, ensure_ascii=True)),
                                )
                                player_id = int(cursor.fetchone()[0])
                                summary["players"] += 1
                                cursor.execute(
                                    """
                                    INSERT INTO core.golf_odds (
                                        golf_tournament_id, golf_player_id, bookmaker_id,
                                        market_code, selection_name, captured_at, decimal_odd,
                                        source_reference, raw_market_key, raw_outcome_json
                                    )
                                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                                    ON CONFLICT (
                                        golf_tournament_id, bookmaker_id, market_code,
                                        selection_name, captured_at
                                    ) DO UPDATE
                                    SET decimal_odd = EXCLUDED.decimal_odd,
                                        golf_player_id = EXCLUDED.golf_player_id,
                                        raw_outcome_json = EXCLUDED.raw_outcome_json
                                    """,
                                    (
                                        tournament_id,
                                        player_id,
                                        bookmaker_id,
                                        market_code,
                                        selection_name,
                                        captured_at,
                                        float(price),
                                        f"{sport_key}|{bookmaker_key}|{raw_market_key}",
                                        raw_market_key,
                                        json.dumps(outcome, ensure_ascii=True),
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
