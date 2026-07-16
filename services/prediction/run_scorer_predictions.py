"""Predictions buteur + deals (anytime goalscorer).

    py services/prediction/run_scorer_predictions.py

Pour chaque match a venir cote au buteur : P(chaque joueur marque) via part
de buts x xG de l'equipe (module scorer.py), confrontee aux cotes reelles.
Emet les value bets buteur (model.scorer_deals), supersede-on-write.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sys

sys.path.insert(0, str((Path(__file__).resolve().parent / "src").resolve()))

from spe_prediction.db import DatabaseSettings, connect_db
from spe_prediction.scorer import (
    PlayerGoalRecord,
    scorer_deal_candidates,
    team_scorer_probabilities,
)

GOAL_WINDOW = "18 months"
HALF_LIFE_DAYS = 365.0  # meme demi-vie que le reste du moteur


def _team_availability(cursor, team_id: int) -> dict[int, float]:
    """Presence recente par joueur : fraction des N derniers matchs de
    l'equipe (avec compo connue) ou le joueur figure. Proxy blessure/banc.
    Neutre (dict vide) si trop peu de compos -> le modele ne penalise pas."""
    cursor.execute(
        """
        WITH recent_fixtures AS (
            SELECT DISTINCT f.fixture_id, f.kickoff_utc
            FROM core.fixtures f
            JOIN core.fixture_lineups fl ON fl.fixture_id = f.fixture_id
            WHERE (f.home_team_id = %s OR f.away_team_id = %s)
              AND f.kickoff_utc < now()
            ORDER BY f.kickoff_utc DESC
            LIMIT 6
        )
        SELECT fl.player_id,
               COUNT(DISTINCT fl.fixture_id) AS apps,
               (SELECT COUNT(*) FROM recent_fixtures) AS total
        FROM core.fixture_lineups fl
        JOIN recent_fixtures rf ON rf.fixture_id = fl.fixture_id
        WHERE fl.player_id IS NOT NULL
        GROUP BY fl.player_id
        """,
        (team_id, team_id),
    )
    rows = cursor.fetchall()
    availability: dict[int, float] = {}
    for pid, apps, total in rows:
        if total and int(total) >= 3:  # assez de compos pour un signal fiable
            availability[int(pid)] = min(1.0, int(apps) / int(total))
    return availability


def _team_goal_data(cursor, team_id: int) -> tuple[list[PlayerGoalRecord], int, float]:
    """Par joueur : buts bruts, buts ponderes recence, forme (90j vs base).
    Renvoie aussi le total d'equipe brut et pondere."""
    cursor.execute(
        f"""
        SELECT tl.player_id, p.player_name, p.position_code,
               COUNT(*) AS goals,
               COUNT(DISTINCT tl.fixture_id) AS matches,
               SUM(POWER(0.5, GREATEST(0, EXTRACT(EPOCH FROM (now() - f.kickoff_utc)) / 86400.0) / {HALF_LIFE_DAYS})) AS wgoals,
               COUNT(*) FILTER (WHERE f.kickoff_utc >= now() - interval '90 days') AS goals_90d
        FROM core.fixture_timeline tl
        JOIN core.fixtures f ON f.fixture_id = tl.fixture_id
        JOIN core.players p ON p.player_id = tl.player_id
        WHERE tl.event_code = 'GOAL' AND tl.player_id IS NOT NULL
          AND ( (tl.is_home AND f.home_team_id = %s)
             OR (NOT tl.is_home AND f.away_team_id = %s) )
          AND f.kickoff_utc >= now() - interval '{GOAL_WINDOW}'
        GROUP BY 1, 2, 3
        """,
        (team_id, team_id),
    )
    # IMPORTANT : consommer CE resultat avant toute autre requete sur le
    # meme curseur (_team_availability l'ecraserait sinon).
    goal_rows = cursor.fetchall()
    availability = _team_availability(cursor, team_id)
    players = []
    for pid, name, position_code, goals, matches, wgoals, goals_90d in goal_rows:
        goals = int(goals)
        # La FORME est deja capturee par la part PONDEREE RECENCE (les buts
        # recents pesent plus). On n'ajoute PAS de multiplicateur de forme
        # par-dessus : ce serait un double-comptage qui sur-gonfle les
        # buteurs chauds (form_factor reste neutre a 1.0). La disponibilite,
        # elle, est orthogonale (le joueur joue-t-il ?).
        players.append(PlayerGoalRecord(
            player_id=int(pid), player_name=str(name),
            goals=goals, matches=int(matches),
            weighted_goals=float(wgoals or 0.0),
            form_factor=1.0,
            availability=availability.get(int(pid), 1.0),
            position_code=str(position_code) if position_code else None,
        ))
    team_total = sum(p.goals for p in players)
    weighted_team_total = sum(p.weighted_goals for p in players)
    return players, team_total, weighted_team_total


def main() -> int:
    now = datetime.now(timezone.utc)
    connection = connect_db(DatabaseSettings.from_env())
    report = {"fixtures": 0, "predictions": 0, "deals": 0}
    try:
        with connection.cursor() as cursor:
            # Fixtures a venir ayant des cotes buteur.
            cursor.execute(
                """
                SELECT DISTINCT f.fixture_id, f.home_team_id, f.away_team_id,
                       p.expected_home_goals, p.expected_away_goals
                FROM core.fixture_odds_scorer os
                JOIN core.fixtures f ON f.fixture_id = os.fixture_id
                LEFT JOIN reporting.v_latest_predictions_1x2 lp ON lp.fixture_id = f.fixture_id
                LEFT JOIN model.predictions p ON p.prediction_id = lp.prediction_id
                WHERE f.kickoff_utc >= now()
                """
            )
            fixtures = cursor.fetchall()
            for fixture_id, home_id, away_id, xg_home, xg_away in fixtures:
                fixture_id = int(fixture_id)
                # Sans prediction 1X2 (donc sans xG), on saute : le modele
                # buteur a besoin du xG attendu de chaque equipe.
                if xg_home is None or xg_away is None:
                    continue
                report["fixtures"] += 1

                probs: dict[int, float] = {}
                team_of: dict[int, int] = {}
                for team_id, team_xg in ((int(home_id), float(xg_home)),
                                         (int(away_id), float(xg_away))):
                    players, team_total, weighted_team_total = _team_goal_data(cursor, team_id)
                    team_probs = team_scorer_probabilities(
                        team_xg, players, team_total,
                        weighted_team_goals=weighted_team_total or None,
                    )
                    for pid, p in team_probs.items():
                        probs[pid] = p
                        team_of[pid] = team_id

                # Ne garder que les joueurs effectivement cotes (lies).
                cursor.execute(
                    """
                    SELECT DISTINCT ON (player_id) player_id, anytime_odd
                    FROM core.fixture_odds_scorer
                    WHERE fixture_id = %s AND player_id IS NOT NULL
                    ORDER BY player_id, captured_at DESC, anytime_odd DESC
                    """,
                    (fixture_id,),
                )
                market_odds = {int(pid): float(odd) for pid, odd in cursor.fetchall()}

                # Persister les predictions (joueurs cotes ET modelises).
                for pid, p in probs.items():
                    if pid not in market_odds:
                        continue
                    cursor.execute(
                        """
                        INSERT INTO model.scorer_predictions (
                            fixture_id, player_id, team_id, probability, method_code
                        )
                        VALUES (%s, %s, %s, %s, 'GOAL_SHARE_POISSON')
                        """,
                        (fixture_id, pid, team_of.get(pid), round(p, 6)),
                    )
                    report["predictions"] += 1

                # Deals : supersede-on-write puis emission.
                cursor.execute(
                    """
                    UPDATE model.scorer_deals SET status_code='CANCELLED',
                        result_code='VOID', profit_units=0, settled_at=now()
                    WHERE fixture_id=%s AND result_code IS NULL
                    """,
                    (fixture_id,),
                )
                candidates = scorer_deal_candidates(probs, market_odds)
                # Meilleur book par joueur pour la cote affichee.
                for cand in candidates[:8]:
                    cursor.execute(
                        """
                        SELECT bookmaker_id FROM core.fixture_odds_scorer
                        WHERE fixture_id=%s AND player_id=%s
                        ORDER BY captured_at DESC, anytime_odd DESC LIMIT 1
                        """,
                        (fixture_id, cand["player_id"]),
                    )
                    brow = cursor.fetchone()
                    cursor.execute(
                        """
                        INSERT INTO model.scorer_deals (
                            fixture_id, player_id, bookmaker_id, model_probability,
                            implied_probability, edge_probability, market_odd
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        """,
                        (fixture_id, cand["player_id"], brow[0] if brow else None,
                         cand["model_probability"], cand["implied_probability"],
                         cand["edge_probability"], cand["market_odd"]),
                    )
                    report["deals"] += 1
        connection.commit()
    finally:
        connection.close()
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
