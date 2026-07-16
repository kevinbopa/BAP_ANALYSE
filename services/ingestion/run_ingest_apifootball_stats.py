"""Backfill des stats de match (xG, tirs, corners, cartons) — API-Football.

    py services/ingestion/run_ingest_apifootball_stats.py
    py services/ingestion/run_ingest_apifootball_stats.py --leagues 39,140 --seasons 2024

Par ligue-saison : liste des matchs, puis stats par match (1 appel/match). Le
xG n'existe chez API-Football que pour les grandes ligues ~2022+. Budget borne
pour ne pas griller le quota jour (7500 en Pro).
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str((Path(__file__).resolve().parent / "src").resolve()))

from spe_ingestion.clients.apifootball import ApiFootballClient
from spe_ingestion.config import ApiFootballSettings
from spe_ingestion.db import DatabaseSettings, connect_db

# Grandes ligues (IDs API-Football standards) avec xG fiable.
DEFAULT_LEAGUES = [39, 140, 135, 78, 61, 2, 3]  # PL, Liga, SerieA, Bundes, L1, UCL, UEL
DEFAULT_SEASONS = [2023, 2024, 2025]
DEFAULT_BUDGET = 6500


def _arg(name, default):
    for a in sys.argv:
        if a.startswith(f"--{name}"):
            val = a.split("=", 1)[1] if "=" in a else sys.argv[sys.argv.index(a) + 1]
            return val
    return default


def _num(value):
    if value is None:
        return None
    try:
        return float(str(value).replace("%", "").strip())
    except (TypeError, ValueError):
        return None


def _team_bridge(cursor):
    cursor.execute("SELECT apifootball_team_id, team_id FROM core.teams WHERE apifootball_team_id IS NOT NULL")
    return {int(a): int(t) for a, t in cursor.fetchall()}


def _parse_dt(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def main() -> int:
    settings = ApiFootballSettings.from_env()
    if not settings.enabled:
        print(json.dumps({"status": "SKIPPED", "reason": "APIFOOTBALL_KEY absent"}))
        return 0
    leagues = [int(x) for x in str(_arg("leagues", ",".join(map(str, DEFAULT_LEAGUES)))).split(",") if x.strip()]
    seasons = [int(x) for x in str(_arg("seasons", ",".join(map(str, DEFAULT_SEASONS)))).split(",") if x.strip()]

    budget = int(os.getenv("APIFOOTBALL_STATS_BUDGET", str(DEFAULT_BUDGET)))
    client = ApiFootballClient(settings)
    connection = connect_db(DatabaseSettings.from_env(prefix="POSTGRES_INGEST"))
    summary = {"leagues": leagues, "seasons": seasons, "fixtures_seen": 0,
               "stats_rows": 0, "with_xg": 0, "skipped_done": 0, "requests": 0,
               "budget": budget, "budget_exhausted": False}
    try:
        with connection.cursor() as cursor:
            bridge = _team_bridge(cursor)
            # Incrementiel : on saute les matchs deja ingeres (economise l'appel
            # stats cher) -> chaque run avance le backfill sans re-payer.
            cursor.execute("SELECT DISTINCT apifootball_fixture_id FROM core.team_match_stats")
            done = {int(r[0]) for r in cursor.fetchall()}
            for league in leagues:
                for season in seasons:
                    if client.requests_made + 1 > budget:
                        summary["budget_exhausted"] = True
                        break
                    payload = client.get_league_fixtures(league, season)
                    for item in payload.get("response") or []:
                        fx = item.get("fixture") or {}
                        status = ((fx.get("status") or {}).get("short") or "")
                        if status != "FT":
                            continue
                        fid = fx.get("id")
                        if fid is not None and int(fid) in done:
                            summary["skipped_done"] += 1
                            continue
                        if client.requests_made + 1 > budget:
                            summary["budget_exhausted"] = True
                            break
                        teams = item.get("teams") or {}
                        goals = item.get("goals") or {}
                        home_id = ((teams.get("home") or {}).get("id"))
                        away_id = ((teams.get("away") or {}).get("id"))
                        gh, ga = goals.get("home"), goals.get("away")
                        kickoff = _parse_dt(fx.get("date"))
                        league_name = ((item.get("league") or {}).get("name"))
                        summary["fixtures_seen"] += 1

                        stats = client.get_fixture_statistics(int(fid))
                        by_team = {}
                        for block in stats.get("response") or []:
                            tid = ((block.get("team") or {}).get("id"))
                            by_team[tid] = {s.get("type"): s.get("value") for s in block.get("statistics") or []}
                        if not by_team:
                            continue
                        for af_team_id, is_home in ((home_id, True), (away_id, False)):
                            st = by_team.get(af_team_id)
                            if st is None:
                                continue
                            opp = by_team.get(away_id if is_home else home_id) or {}
                            xg_for = _num(st.get("expected_goals"))
                            xg_against = _num(opp.get("expected_goals"))
                            gf = gh if is_home else ga
                            gag = ga if is_home else gh
                            cursor.execute(
                                """
                                INSERT INTO core.team_match_stats (
                                    apifootball_fixture_id, apifootball_team_id, team_id,
                                    kickoff_utc, league_name, is_home, goals_for, goals_against,
                                    xg_for, xg_against, shots_total, shots_on, corners,
                                    yellow_cards, red_cards, possession
                                )
                                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                                ON CONFLICT (apifootball_fixture_id, apifootball_team_id) DO UPDATE
                                SET xg_for = EXCLUDED.xg_for, xg_against = EXCLUDED.xg_against,
                                    team_id = EXCLUDED.team_id
                                """,
                                (
                                    int(fid), int(af_team_id) if af_team_id else None,
                                    bridge.get(int(af_team_id)) if af_team_id else None,
                                    kickoff, league_name, is_home,
                                    int(gf) if gf is not None else None,
                                    int(gag) if gag is not None else None,
                                    xg_for, xg_against,
                                    int(_num(st.get("Total Shots")) or 0) if st.get("Total Shots") is not None else None,
                                    int(_num(st.get("Shots on Goal")) or 0) if st.get("Shots on Goal") is not None else None,
                                    int(_num(st.get("Corner Kicks")) or 0) if st.get("Corner Kicks") is not None else None,
                                    int(_num(st.get("Yellow Cards")) or 0) if st.get("Yellow Cards") is not None else None,
                                    int(_num(st.get("Red Cards")) or 0) if st.get("Red Cards") is not None else None,
                                    _num(st.get("Ball Possession")),
                                ),
                            )
                            summary["stats_rows"] += 1
                            if xg_for is not None:
                                summary["with_xg"] += 1
                    connection.commit()
                    if summary["budget_exhausted"]:
                        break
                if summary["budget_exhausted"]:
                    break
        summary["requests"] = client.requests_made
    finally:
        connection.close()
        client.close()
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
