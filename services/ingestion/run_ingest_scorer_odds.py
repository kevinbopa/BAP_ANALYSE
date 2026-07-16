"""Ingestion des cotes buteur (player_goal_scorer_anytime).

    py services/ingestion/run_ingest_scorer_odds.py

Pour chaque match a venir couvert par The Odds API : recupere les cotes
"buteur a tout moment", relie chaque nom de joueur a core.players (restreint
aux joueurs des deux equipes -> pas de collision de noms), et stocke la cote.
Cout quota : 1 x regions credits par match (endpoint par evenement).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import sys

sys.path.insert(0, str((Path(__file__).resolve().parent / "src").resolve()))

from spe_ingestion.clients.theoddsapi import TheOddsApiClient
from spe_ingestion.config import TheOddsApiSettings
from spe_ingestion.db import DatabaseSettings, connect_db
from spe_ingestion.odds_matching import canonical_label, compare_team_names
from spe_ingestion.theoddsapi_odds_ingestor import (
    SPORT_KEY_BY_LEAGUE_NAME,
    EXTRA_SPORT_KEYS_BY_LEAGUE_NAME,
    parse_iso_datetime,
)

SCORER_MARKET = "player_goal_scorer_anytime"
LOOKAHEAD_HOURS = 72


def _last_name(canonical: str) -> str:
    parts = canonical.split()
    return parts[-1] if parts else canonical


def _match_player(raw_name: str, candidates: list[tuple[int, str]]) -> int | None:
    """Relie un nom de cote a un player_id parmi les joueurs des 2 equipes.

    1) match plein canonicalise ; 2) nom de famille + initiale/prenom.
    """
    target = canonical_label(raw_name)
    target_last = _last_name(target)
    target_tokens = target.split()
    # 1. match plein
    for player_id, name in candidates:
        if canonical_label(name) == target:
            return player_id
    # 2. nom de famille + coherence prenom/initiale
    best = None
    for player_id, name in candidates:
        cname = canonical_label(name)
        if _last_name(cname) != target_last or not target_last:
            continue
        ctokens = cname.split()
        # initiales/prenoms compatibles (l'un prefixe l'autre)
        if len(target_tokens) >= 1 and len(ctokens) >= 1:
            a, b = target_tokens[0], ctokens[0]
            if a == b or a.startswith(b[0]) or b.startswith(a[0]):
                best = player_id
    return best


def _team_scorers(cursor, team_id: int) -> list[tuple[int, str]]:
    """Joueurs ayant marque pour cette equipe sur 18 mois (candidats de match)."""
    cursor.execute(
        """
        SELECT DISTINCT tl.player_id, p.player_name
        FROM core.fixture_timeline tl
        JOIN core.fixtures f ON f.fixture_id = tl.fixture_id
        JOIN core.players p ON p.player_id = tl.player_id
        WHERE tl.event_code = 'GOAL' AND tl.player_id IS NOT NULL
          AND ( (tl.is_home AND f.home_team_id = %s)
             OR (NOT tl.is_home AND f.away_team_id = %s) )
          AND f.kickoff_utc >= now() - interval '18 months'
        """,
        (team_id, team_id),
    )
    return [(int(pid), str(name)) for pid, name in cursor.fetchall()]


def _resolve_sport_keys(cursor) -> dict[str, list[dict]]:
    """Fixtures a venir (<= LOOKAHEAD) groupees par sport key The Odds API."""
    commence_to = datetime.now(UTC) + timedelta(hours=LOOKAHEAD_HOURS)
    cursor.execute(
        """
        SELECT f.fixture_id, l.league_name, ht.team_name, at.team_name,
               f.home_team_id, f.away_team_id, f.kickoff_utc
        FROM core.fixtures f
        JOIN core.leagues l ON l.league_id = f.league_id
        JOIN core.teams ht ON ht.team_id = f.home_team_id
        JOIN core.teams at ON at.team_id = f.away_team_id
        WHERE f.kickoff_utc >= now() AND f.kickoff_utc <= %s
        """,
        (commence_to,),
    )
    grouped: dict[str, list[dict]] = {}
    for fid, league, home, away, hid, aid, kickoff in cursor.fetchall():
        keys = []
        direct = SPORT_KEY_BY_LEAGUE_NAME.get(str(league))
        if direct:
            keys.append(direct)
        keys.extend(EXTRA_SPORT_KEYS_BY_LEAGUE_NAME.get(str(league), ()))
        for key in keys:
            grouped.setdefault(key, []).append({
                "fixture_id": int(fid), "home": str(home), "away": str(away),
                "home_id": int(hid), "away_id": int(aid), "kickoff": kickoff,
            })
    return grouped


def main() -> int:
    settings = TheOddsApiSettings.from_env()
    client = TheOddsApiClient(settings)
    connection = connect_db(DatabaseSettings.from_env(prefix="POSTGRES_INGEST"))
    summary = {"events": 0, "fixtures_matched": 0, "odds_written": 0,
               "players_linked": 0, "players_unmatched": 0}
    try:
        with connection.cursor() as cursor:
            grouped = _resolve_sport_keys(cursor)
            for sport_key, candidates in grouped.items():
                try:
                    events = client.get_odds(sport_key)
                except Exception:
                    continue
                for event in events:
                    summary["events"] += 1
                    ev_home = str(event.get("home_team") or "")
                    ev_away = str(event.get("away_team") or "")
                    event_id = str(event.get("id") or "").strip()
                    if not event_id:
                        continue
                    # Relier l'evenement a un fixture (par noms d'equipes).
                    matched = None
                    for c in candidates:
                        if (compare_team_names(ev_home, c["home"]) > 0
                                and compare_team_names(ev_away, c["away"]) > 0):
                            matched = c
                            break
                    if matched is None:
                        continue
                    summary["fixtures_matched"] += 1

                    try:
                        payload = client.get_event_odds(sport_key, event_id, markets=SCORER_MARKET)
                    except Exception:
                        continue
                    roster = (_team_scorers(cursor, matched["home_id"])
                              + _team_scorers(cursor, matched["away_id"]))
                    for bookmaker in payload.get("bookmakers") or []:
                        bkey = str(bookmaker.get("key") or "").strip()
                        if not bkey:
                            continue
                        cursor.execute(
                            "INSERT INTO core.bookmakers (bookmaker_code, bookmaker_name, is_active) "
                            "VALUES (%s,%s,true) ON CONFLICT (bookmaker_code) DO UPDATE "
                            "SET is_active=true RETURNING bookmaker_id",
                            (bkey.upper(), str(bookmaker.get("title") or bkey)),
                        )
                        bookmaker_id = int(cursor.fetchone()[0])
                        captured_at = parse_iso_datetime(bookmaker.get("last_update")) or datetime.now(UTC)
                        for market in bookmaker.get("markets") or []:
                            if str(market.get("key") or "") != SCORER_MARKET:
                                continue
                            for outcome in market.get("outcomes") or []:
                                if str(outcome.get("name") or "").strip().lower() != "yes":
                                    continue
                                raw = str(outcome.get("description") or "").strip()
                                price = outcome.get("price")
                                if not raw or price is None or float(price) <= 1.0:
                                    continue
                                player_id = _match_player(raw, roster)
                                if player_id is None:
                                    summary["players_unmatched"] += 1
                                else:
                                    summary["players_linked"] += 1
                                cursor.execute(
                                    """
                                    INSERT INTO core.fixture_odds_scorer (
                                        fixture_id, player_id, player_name_raw,
                                        bookmaker_id, captured_at, anytime_odd
                                    )
                                    VALUES (%s, %s, %s, %s, %s, %s)
                                    ON CONFLICT (fixture_id, player_name_raw, bookmaker_id, captured_at)
                                    DO UPDATE SET anytime_odd = EXCLUDED.anytime_odd,
                                                  player_id = EXCLUDED.player_id
                                    """,
                                    (matched["fixture_id"], player_id, raw,
                                     bookmaker_id, captured_at, float(price)),
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
