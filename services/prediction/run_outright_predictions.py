"""Previsions long terme ("faits divers") + deals outright.

    py services/prediction/run_outright_predictions.py

Genere, pour chaque competition eligible :
  - LEAGUE_WINNER     : P(titre) par simulation Monte Carlo de la saison
                        restante (probas de chaque match par le moteur).
  - TOURNAMENT_WINNER : P(vainqueur) pour la Coupe du monde (matchs connus
                        simules, tours futurs par Elo).
  - TOP_SCORER        : P(meilleur buteur) par projection Poisson du rythme
                        reel (core.fixture_timeline).
  - BALLON_DOR_INDEX  : indice de performance 12 mois (descriptif, sans deal).

Puis confronte les probabilites aux cotes outright en base (si presentes)
et emet les deals long terme (model.outright_deals) — supersede-on-write.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sys

# Console Windows cp1252 vs noms de joueurs européens (ń, ø, ć...).
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str((Path(__file__).resolve().parent / "src").resolve()))

from spe_prediction.backtest import _TeamState, _snapshot, MIN_PRIOR_MATCHES
from spe_prediction.correlations import competition_family
from spe_prediction.db import DatabaseSettings, connect_db
from spe_prediction.domain import FixtureFeatures
from spe_prediction.engine import MatchAnalysisEngine
from spe_prediction.outrights import (
    RemainingMatch,
    ScorerState,
    outright_deal_candidates,
    performance_index,
    simulate_knockout,
    simulate_league_title,
    top_scorer_probabilities,
)
from spe_prediction.rating import (
    BASE_K, HOME_BONUS_ELO, _margin_multiplier, competition_weight, is_neutral_venue,
)

MIN_REMAINING_FOR_TITLE = 5   # championnats : sous 5 matchs le titre est joue
MIN_REMAINING_KNOCKOUT = 2    # tournoi a elimination : des les quarts
MAX_SIMULATED_MATCHES = 400
LEAGUE_SIMULATIONS = 3000
KNOCKOUT_SIMULATIONS = 10000
SCORER_SIMULATIONS = 5000


def _replay_team_states(cursor) -> dict[int, _TeamState]:
    """Rejoue tout l'historique (Elo + forme) — meme mecanique que le backtest."""
    cursor.execute(
        """
        SELECT f.kickoff_utc, f.home_team_id, f.away_team_id,
               fs.home_score, fs.away_score, l.league_name
        FROM core.fixtures f
        JOIN core.fixture_scores fs ON fs.fixture_id = f.fixture_id
        JOIN core.leagues l ON l.league_id = f.league_id
        WHERE fs.home_score IS NOT NULL AND f.kickoff_utc IS NOT NULL
        ORDER BY f.kickoff_utc ASC, f.fixture_id ASC
        """
    )
    states: dict[int, _TeamState] = {}
    for kickoff, home_id, away_id, hs, aws, league in cursor.fetchall():
        kickoff = kickoff if kickoff.tzinfo else kickoff.replace(tzinfo=timezone.utc)
        home_id, away_id, hs, aws = int(home_id), int(away_id), int(hs), int(aws)
        home_state = states.setdefault(home_id, _TeamState())
        away_state = states.setdefault(away_id, _TeamState())
        bonus = 0.0 if is_neutral_venue(str(league)) else HOME_BONUS_ELO
        expected = 1.0 / (1.0 + math.pow(10.0, -((home_state.rating - away_state.rating + bonus) / 400.0)))
        actual = 1.0 if hs > aws else 0.5 if hs == aws else 0.0
        k = BASE_K * competition_weight(str(league)) * _margin_multiplier(abs(hs - aws))
        delta = k * (actual - expected)
        home_state.rating += delta
        away_state.rating -= delta
        home_pts = 3.0 if hs > aws else 1.0 if hs == aws else 0.0
        away_pts = 3.0 if aws > hs else 1.0 if hs == aws else 0.0
        home_state.history.append((kickoff, str(league), float(hs), float(aws), home_pts))
        away_state.history.append((kickoff, str(league), float(aws), float(hs), away_pts))
    return states


def _project_matches(
    engine, states, rows, league_name: str, now: datetime,
) -> list[RemainingMatch]:
    """Probabilites 1X2 du moteur pour chaque match restant."""
    neutral = is_neutral_venue(league_name)
    projected: list[RemainingMatch] = []
    for home_id, home_name, away_id, away_name, kickoff in rows:
        kickoff = kickoff if kickoff and kickoff.tzinfo else (kickoff.replace(tzinfo=timezone.utc) if kickoff else now)
        home_state = states.setdefault(int(home_id), _TeamState())
        away_state = states.setdefault(int(away_id), _TeamState())
        fixture = FixtureFeatures(
            fixture_id=0,
            home_team=_snapshot(int(home_id), str(home_name), home_state, kickoff),
            away_team=_snapshot(int(away_id), str(away_name), away_state, kickoff),
            home_advantage=0.04 if neutral else 0.16,
        )
        result = engine.analyze_fixture(fixture, None)
        projected.append(
            RemainingMatch(
                home_team_id=int(home_id),
                away_team_id=int(away_id),
                p_home=result.probabilities.home,
                p_draw=result.probabilities.draw,
                p_away=result.probabilities.away,
            )
        )
    return projected


def _upsert_market(cursor, market_code, league_id, season_name, label, deadline) -> int:
    cursor.execute(
        """
        INSERT INTO core.outright_markets (market_code, league_id, season_name, market_label, deadline_utc)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (market_code, league_id, season_name) DO UPDATE
        SET market_label = EXCLUDED.market_label,
            deadline_utc = EXCLUDED.deadline_utc,
            status_code = 'OPEN'
        RETURNING outright_market_id
        """,
        (market_code, league_id, season_name, label, deadline),
    )
    return int(cursor.fetchone()[0])


def _upsert_selection(cursor, market_id, subject_type, label, team_id=None, player_id=None) -> int:
    cursor.execute(
        """
        INSERT INTO core.outright_selections (
            outright_market_id, subject_type, team_id, player_id, subject_label
        )
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (outright_market_id, subject_label) DO UPDATE
        SET team_id = EXCLUDED.team_id, player_id = EXCLUDED.player_id
        RETURNING outright_selection_id
        """,
        (market_id, subject_type, team_id, player_id, label),
    )
    return int(cursor.fetchone()[0])


def _store_predictions(cursor, market_id, probabilities_by_selection, method_code, details=None):
    for selection_id, probability in probabilities_by_selection.items():
        cursor.execute(
            """
            INSERT INTO model.outright_predictions (
                outright_market_id, outright_selection_id, probability, method_code, details_json
            )
            VALUES (%s, %s, %s, %s, %s::jsonb)
            """,
            (market_id, selection_id, round(float(probability), 6), method_code,
             json.dumps(details or {}, ensure_ascii=True)),
        )


def _emit_deals(cursor, market_id, probabilities_by_selection) -> int:
    """Deals outright : edge vs la meilleure cote de chaque candidat."""
    cursor.execute(
        """
        SELECT DISTINCT ON (oo.outright_selection_id)
            oo.outright_selection_id, oo.decimal_odd, oo.bookmaker_id
        FROM core.outright_odds oo
        JOIN core.outright_selections s ON s.outright_selection_id = oo.outright_selection_id
        WHERE s.outright_market_id = %s
        ORDER BY oo.outright_selection_id, oo.captured_at DESC, oo.decimal_odd DESC
        """,
        (market_id,),
    )
    odds = {}
    books = {}
    for selection_id, odd, bookmaker_id in cursor.fetchall():
        odds[int(selection_id)] = float(odd)
        books[int(selection_id)] = bookmaker_id
    if not odds:
        return 0
    candidates = outright_deal_candidates(probabilities_by_selection, odds)
    # Supersede-on-write : ce run devient LA position long terme du marche.
    cursor.execute(
        """
        UPDATE model.outright_deals SET status_code = 'CANCELLED', result_code = 'VOID',
               profit_units = 0, settled_at = now()
        WHERE outright_market_id = %s AND result_code IS NULL
        """,
        (market_id,),
    )
    for candidate in candidates[:3]:
        cursor.execute(
            """
            INSERT INTO model.outright_deals (
                outright_market_id, outright_selection_id, bookmaker_id,
                model_probability, implied_probability, edge_probability, market_odd
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                market_id, candidate["selection_id"], books.get(candidate["selection_id"]),
                candidate["model_probability"], candidate["implied_probability"],
                candidate["edge_probability"], candidate["market_odd"],
            ),
        )
    return len(candidates[:3])


def main() -> int:
    now = datetime.now(timezone.utc)
    connection = connect_db(DatabaseSettings.from_env())
    engine = MatchAnalysisEngine()  # analytique calibre, sans marche
    report = {"league_winner": [], "tournament_winner": [], "top_scorer": [], "deals": 0}
    try:
        with connection.cursor() as cursor:
            states = _replay_team_states(cursor)

            # --- competitions avec matchs restants ---------------------------
            cursor.execute(
                """
                SELECT l.league_id, l.league_name, f.season_id, s.season_name,
                       COUNT(*) AS remaining, MAX(f.kickoff_utc) AS deadline
                FROM core.fixtures f
                JOIN core.leagues l ON l.league_id = f.league_id
                LEFT JOIN core.seasons s ON s.season_id = f.season_id
                WHERE f.kickoff_utc >= now()
                GROUP BY 1, 2, 3, 4
                HAVING COUNT(*) >= %s
                """,
                (MIN_REMAINING_KNOCKOUT,),
            )
            targets = cursor.fetchall()

            for league_id, league_name, season_id, season_name, remaining, deadline in targets:
                league_id = int(league_id)
                league_name = str(league_name)
                season_name = str(season_name or "")
                family = competition_family(league_name)
                if family not in ("DOMESTIC", "WORLD_CUP"):
                    continue  # V1 : titres de championnats + Coupe du monde
                minimum = (
                    MIN_REMAINING_FOR_TITLE if family == "DOMESTIC" else MIN_REMAINING_KNOCKOUT
                )
                if int(remaining) < minimum:
                    continue

                cursor.execute(
                    """
                    SELECT f.home_team_id, ht.team_name, f.away_team_id, at.team_name, f.kickoff_utc
                    FROM core.fixtures f
                    JOIN core.teams ht ON ht.team_id = f.home_team_id
                    JOIN core.teams at ON at.team_id = f.away_team_id
                    WHERE f.league_id = %s AND f.season_id = %s AND f.kickoff_utc >= now()
                    ORDER BY f.kickoff_utc
                    LIMIT %s
                    """,
                    (league_id, season_id, MAX_SIMULATED_MATCHES),
                )
                remaining_rows = cursor.fetchall()
                if len(remaining_rows) < minimum:
                    continue
                projected = _project_matches(engine, states, remaining_rows, league_name, now)

                team_names: dict[int, str] = {}
                for home_id, home_name, away_id, away_name, _ in remaining_rows:
                    team_names[int(home_id)] = str(home_name)
                    team_names[int(away_id)] = str(away_name)

                if family == "DOMESTIC":
                    # Standings courants de la meme saison.
                    cursor.execute(
                        """
                        SELECT f.home_team_id, f.away_team_id, fs.home_score, fs.away_score
                        FROM core.fixtures f
                        JOIN core.fixture_scores fs ON fs.fixture_id = f.fixture_id
                        WHERE f.league_id = %s AND f.season_id = %s
                          AND fs.home_score IS NOT NULL
                        """,
                        (league_id, season_id),
                    )
                    points: dict[int, float] = {}
                    for home_id, away_id, hs, aws in cursor.fetchall():
                        hs, aws = int(hs), int(aws)
                        points.setdefault(int(home_id), 0.0)
                        points.setdefault(int(away_id), 0.0)
                        if hs > aws:
                            points[int(home_id)] += 3
                        elif hs < aws:
                            points[int(away_id)] += 3
                        else:
                            points[int(home_id)] += 1
                            points[int(away_id)] += 1
                    titles = simulate_league_title(
                        projected, points, simulations=LEAGUE_SIMULATIONS,
                    )
                    market_code, method = "LEAGUE_WINNER", "SEASON_SIMULATION"
                    label = f"Champion - {league_name} {season_name}".strip()
                else:
                    # Coupe du monde : premier tour = les matchs connus,
                    # tours suivants apparies par Elo.
                    def _elo_win_probability(team_a: int, team_b: int) -> float:
                        rating_a = states.setdefault(team_a, _TeamState()).rating
                        rating_b = states.setdefault(team_b, _TeamState()).rating
                        return 1.0 / (1.0 + math.pow(10.0, -(rating_a - rating_b) / 400.0))

                    titles = simulate_knockout(
                        [projected], simulations=KNOCKOUT_SIMULATIONS,
                        win_probability_fn=_elo_win_probability,
                    )
                    market_code, method = "TOURNAMENT_WINNER", "KNOCKOUT_SIMULATION"
                    label = f"Vainqueur - {league_name} {season_name}".strip()

                if not titles:
                    continue
                market_id = _upsert_market(
                    cursor, market_code, league_id, season_name, label, deadline,
                )
                probabilities_by_selection: dict[int, float] = {}
                for team_id, probability in sorted(titles.items(), key=lambda kv: kv[1], reverse=True):
                    if probability < 0.005:
                        continue
                    selection_id = _upsert_selection(
                        cursor, market_id, "TEAM",
                        team_names.get(team_id, f"team:{team_id}"), team_id=team_id,
                    )
                    probabilities_by_selection[selection_id] = probability
                _store_predictions(
                    cursor, market_id, probabilities_by_selection, method,
                    {"remaining_matches": len(projected), "simulations": LEAGUE_SIMULATIONS},
                )
                report["deals"] += _emit_deals(cursor, market_id, probabilities_by_selection)
                key = "league_winner" if market_code == "LEAGUE_WINNER" else "tournament_winner"
                top = max(titles.items(), key=lambda kv: kv[1])
                report[key].append(
                    f"{league_name} {season_name}: favori {team_names.get(top[0])} ({top[1]:.0%})"
                )

                # --- meilleur buteur (saisons EN COURS seulement) ------------
                if family == "DOMESTIC":
                    cursor.execute(
                        """
                        SELECT tl.player_id, p.player_name,
                               CASE WHEN tl.is_home THEN f.home_team_id ELSE f.away_team_id END AS team_id,
                               COUNT(*) AS goals
                        FROM core.fixture_timeline tl
                        JOIN core.fixtures f ON f.fixture_id = tl.fixture_id
                        JOIN core.players p ON p.player_id = tl.player_id
                        WHERE f.league_id = %s AND f.season_id = %s
                          AND tl.event_code = 'GOAL' AND tl.player_id IS NOT NULL
                        GROUP BY 1, 2, 3
                        ORDER BY goals DESC
                        LIMIT 25
                        """,
                        (league_id, season_id),
                    )
                    scorer_rows = cursor.fetchall()
                    if len(scorer_rows) >= 5:
                        cursor.execute(
                            """
                            SELECT team_id, COUNT(*) FROM (
                                SELECT f.home_team_id AS team_id FROM core.fixtures f
                                JOIN core.fixture_scores fs ON fs.fixture_id = f.fixture_id
                                WHERE f.league_id = %s AND f.season_id = %s AND fs.home_score IS NOT NULL
                                UNION ALL
                                SELECT f.away_team_id FROM core.fixtures f
                                JOIN core.fixture_scores fs ON fs.fixture_id = f.fixture_id
                                WHERE f.league_id = %s AND f.season_id = %s AND fs.home_score IS NOT NULL
                            ) played GROUP BY team_id
                            """,
                            (league_id, season_id, league_id, season_id),
                        )
                        played_by_team = {int(t): int(c) for t, c in cursor.fetchall()}
                        remaining_by_team: dict[int, int] = {}
                        for match in projected:
                            remaining_by_team[match.home_team_id] = remaining_by_team.get(match.home_team_id, 0) + 1
                            remaining_by_team[match.away_team_id] = remaining_by_team.get(match.away_team_id, 0) + 1
                        scorers = [
                            ScorerState(
                                player_id=int(pid), player_name=str(pname),
                                team_id=int(tid), goals=int(goals),
                                matches_played=played_by_team.get(int(tid), 1),
                            )
                            for pid, pname, tid, goals in scorer_rows
                        ]
                        scorer_probs = top_scorer_probabilities(
                            scorers, remaining_by_team, simulations=SCORER_SIMULATIONS,
                        )
                        if scorer_probs:
                            scorer_market_id = _upsert_market(
                                cursor, "TOP_SCORER", league_id, season_name,
                                f"Meilleur buteur - {league_name} {season_name}".strip(), deadline,
                            )
                            names = {s.player_id: s.player_name for s in scorers}
                            by_selection = {}
                            for pid, prob in scorer_probs.items():
                                if prob < 0.01:
                                    continue
                                sid = _upsert_selection(
                                    cursor, scorer_market_id, "PLAYER",
                                    names.get(pid, f"player:{pid}"), player_id=pid,
                                )
                                by_selection[sid] = prob
                            _store_predictions(
                                cursor, scorer_market_id, by_selection, "SCORER_PROJECTION",
                                {"simulations": SCORER_SIMULATIONS},
                            )
                            best_pid = max(scorer_probs, key=scorer_probs.get)
                            report["top_scorer"].append(
                                f"{league_name}: {names.get(best_pid)} ({scorer_probs[best_pid]:.0%})"
                            )

            # --- indice Ballon d'Or (12 mois glissants, toutes competitions) --
            cursor.execute(
                """
                SELECT tl.player_id, p.player_name, l.league_name, tl.event_code, COUNT(*)
                FROM core.fixture_timeline tl
                JOIN core.fixtures f ON f.fixture_id = tl.fixture_id
                JOIN core.leagues l ON l.league_id = f.league_id
                JOIN core.players p ON p.player_id = tl.player_id
                WHERE tl.event_code = 'GOAL' AND tl.player_id IS NOT NULL
                  AND f.kickoff_utc >= now() - interval '12 months'
                  -- Ecarte les joueurs placeholder des feuilles de match sales.
                  AND p.player_name !~ '^Player [0-9]+$'
                  AND length(p.player_name) > 2
                GROUP BY 1, 2, 3, 4
                """
            )
            goal_rows = cursor.fetchall()
            cursor.execute(
                """
                SELECT tl.assist_player_id, p.player_name, l.league_name, COUNT(*)
                FROM core.fixture_timeline tl
                JOIN core.fixtures f ON f.fixture_id = tl.fixture_id
                JOIN core.leagues l ON l.league_id = f.league_id
                JOIN core.players p ON p.player_id = tl.assist_player_id
                WHERE tl.event_code = 'GOAL' AND tl.assist_player_id IS NOT NULL
                  AND f.kickoff_utc >= now() - interval '12 months'
                GROUP BY 1, 2, 3
                """
            )
            assist_rows = cursor.fetchall()
            per_player: dict[int, dict] = {}
            for pid, name, league, _code, count in goal_rows:
                entry = per_player.setdefault(int(pid), {"name": str(name), "goals": 0.0, "assists": 0.0, "weights": []})
                weight = competition_weight(str(league))
                entry["goals"] += int(count) * weight
                entry["weights"].append(weight)
            for pid, name, league, count in assist_rows:
                entry = per_player.setdefault(int(pid), {"name": str(name), "goals": 0.0, "assists": 0.0, "weights": []})
                weight = competition_weight(str(league))
                entry["assists"] += int(count) * weight
                entry["weights"].append(weight)
            index_rows = [
                (pid, e["name"], e["goals"], e["assists"],
                 sum(e["weights"]) / len(e["weights"]) if e["weights"] else 1.0)
                for pid, e in per_player.items()
            ]
            ranking = performance_index(index_rows)[:25]
            if ranking:
                bdo_market_id = _upsert_market(
                    cursor, "BALLON_DOR_INDEX", None, str(now.year),
                    f"Indice Ballon d'Or {now.year} (performance 12 mois)", None,
                )
                by_selection = {}
                for entry in ranking:
                    sid = _upsert_selection(
                        cursor, bdo_market_id, "PLAYER",
                        str(entry["player_name"]), player_id=int(entry["player_id"]),
                    )
                    by_selection[sid] = float(entry["index"])
                _store_predictions(
                    cursor, bdo_market_id, by_selection, "PERFORMANCE_INDEX",
                    {"note": "indice descriptif, pas une probabilite - aucun deal"},
                )
                report["ballon_dor_top3"] = [e["player_name"] for e in ranking[:3]]

        connection.commit()
    finally:
        connection.close()
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
