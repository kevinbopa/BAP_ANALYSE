"""Reglement des value bets — le track record REEL du systeme.

Chaque deal recommande (model.value_bets, statut ACTIVE) est regle des que
le score final de son match est en base : WON / LOST + profit en unites
(mise plate de 1 unite : WON -> cote-1, LOST -> -1).

C'est le "backtest vivant" : les cotes historiques n'existant pas, la seule
mesure honnete du ROI des deals est prospective. Elle s'accumule ici a
chaque match termine.
"""
from __future__ import annotations

from typing import Any


def regulation_score(
    final_home: int,
    final_away: int,
    goals_home_90: int | None,
    goals_away_90: int | None,
    goals_home_total: int | None,
    goals_away_total: int | None,
    has_extra_time_goal: bool,
) -> tuple[int, int]:
    """Score des 90 minutes reglementaires — la base de TOUT pari 1X2/O-U/BTTS.

    "Gagner" un pari se juge a 90 minutes ; "se qualifier" (prolongations,
    tirs au but) est un autre marche. TheSportsDB stocke le score FINAL :
    quand la timeline montre des buts au-dela de la 90e (prolongation) ET
    qu'elle est coherente avec le score final (aucun but manquant), on
    reconstruit le score reglementaire. Timeline incomplete -> score final
    (on ne devine jamais).

    Convention TheSportsDB verifiee (2026-07-08) : le temps additionnel
    reglementaire (90+X) est stocke a la minute 90, et la prolongation aux
    minutes 91-120 (120+X dumpe a 120). Le seuil "minute <= 90" cote appelant
    capture donc bien tout le temps reglementaire, additionnel inclus.
    """
    if not has_extra_time_goal:
        return final_home, final_away
    if goals_home_total is None or goals_away_total is None:
        return final_home, final_away
    if int(goals_home_total) != final_home or int(goals_away_total) != final_away:
        # Timeline incoherente avec le score officiel : pas fiable.
        return final_home, final_away
    return int(goals_home_90 or 0), int(goals_away_90 or 0)


def settle_result(
    selection_code: str, home_score: int, away_score: int, market_code: str | None = None
) -> str:
    total = home_score + away_score
    # O/U toutes lignes (demi-lignes 1.5/2.5/3.5 -> jamais de push).
    if selection_code in ("OVER", "UNDER"):
        line = {"OU15": 1.5, "OU35": 3.5}.get(market_code or "", 2.5)
        won = total > line if selection_code == "OVER" else total < line
        return "WON" if won else "LOST"
    if selection_code == "BTTS_YES":
        return "WON" if home_score > 0 and away_score > 0 else "LOST"
    if selection_code == "BTTS_NO":
        return "WON" if home_score == 0 or away_score == 0 else "LOST"
    winner = (
        "HOME" if home_score > away_score
        else "AWAY" if away_score > home_score
        else "DRAW"
    )
    # Double chance : l'issue est-elle dans la paire couverte ?
    dc_sets = {"DC_1X": {"HOME", "DRAW"}, "DC_12": {"HOME", "AWAY"}, "DC_X2": {"DRAW", "AWAY"}}
    if selection_code in dc_sets:
        return "WON" if winner in dc_sets[selection_code] else "LOST"
    # Draw-no-bet : mise remboursee (VOID) si match nul.
    if market_code == "DNB":
        if winner == "DRAW":
            return "VOID"
        return "WON" if selection_code == winner else "LOST"
    return "WON" if selection_code == winner else "LOST"


def settle_handicap(
    home_margin: int, line: float, is_home: bool, market_odd: float
) -> tuple[str, float]:
    """Reglement asiatique. `line` est du point de vue de la selection (home
    ou away). Une ligne en quart (x.25/x.75) = deux demi-mises aux lignes
    adjacentes. Retourne (result_code, profit) pour une mise plate d'1 unite.

      home -1, gagne de 2 -> WON plein ; de 1 -> push (VOID) ; nul/perd -> LOST
      home -0.75, gagne de 1 -> demi-gain ; home -1.25, gagne de 1 -> demi-perte
    """
    base = home_margin if is_home else -home_margin
    # Ligne en quart ? -> deux sous-lignes a +/-0.25 (demi-mise chacune).
    if abs(line * 2 - round(line * 2)) > 1e-6:
        subs = (line - 0.25, line + 0.25)
    else:
        subs = (line, line)
    total = 0.0
    for sub in subs:
        adjusted = base + sub
        if adjusted > 1e-9:
            total += (market_odd - 1.0)
        elif adjusted < -1e-9:
            total += -1.0
        # push (== 0) : +0
    profit = round(total / 2.0, 4)
    if profit > 1e-9:
        code = "WON"
    elif profit < -1e-9:
        code = "LOST"
    else:
        code = "VOID"
    return code, profit


def flat_profit(result_code: str, market_odd: float | None) -> float | None:
    if market_odd is None or market_odd <= 1.0:
        return None
    if result_code == "WON":
        return round(market_odd - 1.0, 4)
    if result_code == "LOST":
        return -1.0
    return 0.0


def settle_pending_bets(connection) -> dict[str, Any]:
    settled = 0
    won = 0
    profit_total = 0.0
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT vb.value_bet_id, vb.selection_code, vb.market_odd, vb.market_code,
                   vb.line,
                   fs.home_score, fs.away_score,
                   goals.h90, goals.a90, goals.h_total, goals.a_total,
                   COALESCE(goals.has_extra_time, false) AS has_extra_time
            FROM model.value_bets vb
            JOIN core.fixture_scores fs ON fs.fixture_id = vb.fixture_id
            LEFT JOIN LATERAL (
                SELECT
                    COUNT(*) FILTER (WHERE tl.is_home AND tl.minute <= 90) AS h90,
                    COUNT(*) FILTER (WHERE NOT tl.is_home AND tl.minute <= 90) AS a90,
                    COUNT(*) FILTER (WHERE tl.is_home) AS h_total,
                    COUNT(*) FILTER (WHERE NOT tl.is_home) AS a_total,
                    BOOL_OR(tl.minute > 90) AS has_extra_time
                FROM core.fixture_timeline tl
                WHERE tl.fixture_id = vb.fixture_id
                  AND tl.event_code = 'GOAL'
                  AND tl.is_home IS NOT NULL
                  AND tl.minute IS NOT NULL
            ) goals ON true
            WHERE vb.result_code IS NULL
              AND fs.home_score IS NOT NULL
              AND fs.away_score IS NOT NULL
            """
        )
        rows = cursor.fetchall()
        for (value_bet_id, selection_code, market_odd, market_code, line, hs, aws,
             h90, a90, h_total, a_total, has_extra_time) in rows:
            # Les paris se reglent sur les 90 minutes reglementaires —
            # la prolongation appartient au marche "qualification".
            hs_reg, as_reg = regulation_score(
                int(hs), int(aws),
                int(h90) if h90 is not None else None,
                int(a90) if a90 is not None else None,
                int(h_total) if h_total is not None else None,
                int(a_total) if a_total is not None else None,
                bool(has_extra_time),
            )
            if str(market_code) == "HANDICAP" and line is not None and market_odd is not None:
                # Reglement ASIATIQUE : push ligne entiere, demi-gain/perte
                # sur les quarts (.25/.75) — profit calcule directement.
                result, profit = settle_handicap(
                    hs_reg - as_reg, float(line),
                    str(selection_code) == "HOME", float(market_odd),
                )
            else:
                result = settle_result(str(selection_code), hs_reg, as_reg, str(market_code))
                profit = flat_profit(result, float(market_odd) if market_odd is not None else None)
            if profit is None:
                result = "VOID"
                profit = 0.0
            cursor.execute(
                """
                UPDATE model.value_bets
                SET result_code = %s, profit_units = %s, settled_at = now()
                WHERE value_bet_id = %s
                """,
                (result, profit, int(value_bet_id)),
            )
            settled += 1
            if result == "WON":
                won += 1
            profit_total += profit

        cursor.execute("SELECT * FROM reporting.v_deal_performance")
        perf_row = cursor.fetchone()
        perf_cols = [c.name for c in cursor.description]
    connection.commit()

    predictions_settled = settle_predictions(connection)
    parlays_settled = settle_parlays(connection)
    scorer_settled = settle_scorer_deals(connection)

    return {
        "newly_settled": settled,
        "newly_won": won,
        "newly_profit_units": round(profit_total, 2),
        "predictions_settled": predictions_settled,
        "parlays_settled": parlays_settled,
        "scorer_settled": scorer_settled,
        "all_time": dict(zip(perf_cols, [str(v) for v in perf_row])) if perf_row else {},
    }


def settle_scorer_deals(connection) -> dict[str, Any]:
    """Regle les deals buteur : le joueur a-t-il marque en temps
    reglementaire (<= 90 min) ? Une prolongation ne compte pas."""
    settled = 0
    won = 0
    profit_total = 0.0
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT sd.scorer_deal_id, sd.player_id, sd.fixture_id, sd.market_odd,
                   EXISTS (
                       SELECT 1 FROM core.fixture_timeline tl
                       WHERE tl.fixture_id = sd.fixture_id
                         AND tl.event_code = 'GOAL'
                         AND tl.player_id = sd.player_id
                         AND tl.minute <= 90
                   ) AS scored
            FROM model.scorer_deals sd
            JOIN core.fixture_scores fs ON fs.fixture_id = sd.fixture_id
            WHERE sd.result_code IS NULL
              AND fs.home_score IS NOT NULL AND fs.away_score IS NOT NULL
            """
        )
        rows = cursor.fetchall()
        for scorer_deal_id, player_id, fixture_id, market_odd, scored in rows:
            result = "WON" if scored else "LOST"
            profit = flat_profit(result, float(market_odd) if market_odd is not None else None)
            if profit is None:
                result, profit = "VOID", 0.0
            cursor.execute(
                "UPDATE model.scorer_deals SET result_code=%s, profit_units=%s, "
                "settled_at=now() WHERE scorer_deal_id=%s",
                (result, profit, int(scorer_deal_id)),
            )
            settled += 1
            if result == "WON":
                won += 1
            profit_total += profit
    connection.commit()
    return {
        "newly_settled": settled,
        "newly_won": won,
        "newly_profit_units": round(profit_total, 2),
    }


def settle_predictions(connection) -> dict[str, Any]:
    """Regle les PRONOSTICS 1X2 du forecaster (pas les deals).

    Pour chaque fixture termine, on prend le DERNIER pronostic et on verifie
    s'il est tombe juste sur le resultat des 90 minutes reglementaires. C'est
    la performance PURE du moteur de prediction, independante du value betting.
    """
    settled = 0
    correct = 0
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT lp.fixture_id, lp.prediction_id,
                   p.feature_snapshot_json::jsonb ->> 'pronostic' AS pronostic,
                   (p.feature_snapshot_json::jsonb ->> 'surete')::numeric AS surete,
                   fs.home_score, fs.away_score,
                   goals.h90, goals.a90, goals.h_total, goals.a_total,
                   COALESCE(goals.has_extra_time, false) AS has_extra_time
            FROM reporting.v_latest_predictions_1x2 lp
            JOIN model.predictions p ON p.prediction_id = lp.prediction_id
            JOIN core.fixture_scores fs ON fs.fixture_id = lp.fixture_id
            LEFT JOIN LATERAL (
                SELECT
                    COUNT(*) FILTER (WHERE tl.is_home AND tl.minute <= 90) AS h90,
                    COUNT(*) FILTER (WHERE NOT tl.is_home AND tl.minute <= 90) AS a90,
                    COUNT(*) FILTER (WHERE tl.is_home) AS h_total,
                    COUNT(*) FILTER (WHERE NOT tl.is_home) AS a_total,
                    BOOL_OR(tl.minute > 90) AS has_extra_time
                FROM core.fixture_timeline tl
                WHERE tl.fixture_id = lp.fixture_id
                  AND tl.event_code = 'GOAL'
                  AND tl.is_home IS NOT NULL AND tl.minute IS NOT NULL
            ) goals ON true
            WHERE fs.home_score IS NOT NULL AND fs.away_score IS NOT NULL
              AND p.feature_snapshot_json::jsonb ->> 'pronostic' IN ('HOME', 'DRAW', 'AWAY')
              AND NOT EXISTS (
                  SELECT 1 FROM model.prediction_outcomes po
                  WHERE po.fixture_id = lp.fixture_id
                    AND po.prediction_id = lp.prediction_id
              )
            """
        )
        rows = cursor.fetchall()
        for (fixture_id, prediction_id, pronostic, surete, hs, aws,
             h90, a90, h_total, a_total, has_extra_time) in rows:
            hs_reg, as_reg = regulation_score(
                int(hs), int(aws),
                int(h90) if h90 is not None else None,
                int(a90) if a90 is not None else None,
                int(h_total) if h_total is not None else None,
                int(a_total) if a_total is not None else None,
                bool(has_extra_time),
            )
            actual = (
                "HOME" if hs_reg > as_reg else "AWAY" if as_reg > hs_reg else "DRAW"
            )
            is_correct = (str(pronostic) == actual)
            cursor.execute(
                """
                INSERT INTO model.prediction_outcomes (
                    fixture_id, prediction_id, pronostic, actual_outcome,
                    is_correct, surety_score, home_reg, away_reg
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (fixture_id) DO UPDATE
                SET prediction_id = EXCLUDED.prediction_id,
                    pronostic = EXCLUDED.pronostic,
                    actual_outcome = EXCLUDED.actual_outcome,
                    is_correct = EXCLUDED.is_correct,
                    surety_score = EXCLUDED.surety_score,
                    home_reg = EXCLUDED.home_reg,
                    away_reg = EXCLUDED.away_reg,
                    settled_at = now()
                """,
                (int(fixture_id), int(prediction_id), str(pronostic), actual,
                 is_correct, surete, hs_reg, as_reg),
            )
            settled += 1
            if is_correct:
                correct += 1
    connection.commit()
    return {
        "newly_settled": settled,
        "newly_correct": correct,
        "accuracy_pct": round(100.0 * correct / settled, 1) if settled else None,
    }


def settle_parlays(connection) -> dict[str, Any]:
    """Regle les combines : un ticket gagne SEULEMENT si toutes ses jambes
    gagnent (produit des cotes). Chaque jambe se regle sur les 90 min
    reglementaires de son match. Un combine reste en attente tant qu'une
    jambe n'a pas de score final."""
    settled = 0
    won = 0
    profit_total = 0.0
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT parlay_id, combined_odd, stake_amount FROM model.parlay_tickets "
            "WHERE result_code IS NULL"
        )
        parlays = cursor.fetchall()
        for parlay_id, combined_odd, stake_amount in parlays:
            cursor.execute(
                """
                SELECT pl.leg_id, pl.market_code, pl.selection_code,
                       fs.home_score, fs.away_score,
                       goals.h90, goals.a90, goals.h_total, goals.a_total,
                       COALESCE(goals.has_extra_time, false) AS has_extra_time
                FROM model.parlay_legs pl
                LEFT JOIN core.fixture_scores fs ON fs.fixture_id = pl.fixture_id
                LEFT JOIN LATERAL (
                    SELECT
                        COUNT(*) FILTER (WHERE tl.is_home AND tl.minute <= 90) AS h90,
                        COUNT(*) FILTER (WHERE NOT tl.is_home AND tl.minute <= 90) AS a90,
                        COUNT(*) FILTER (WHERE tl.is_home) AS h_total,
                        COUNT(*) FILTER (WHERE NOT tl.is_home) AS a_total,
                        BOOL_OR(tl.minute > 90) AS has_extra_time
                    FROM core.fixture_timeline tl
                    WHERE tl.fixture_id = pl.fixture_id
                      AND tl.event_code = 'GOAL'
                      AND tl.is_home IS NOT NULL AND tl.minute IS NOT NULL
                ) goals ON true
                WHERE pl.parlay_id = %s
                """,
                (parlay_id,),
            )
            legs = cursor.fetchall()
            all_settled = True
            any_lost = False
            for (leg_id, market_code, selection_code, hs, aws,
                 h90, a90, h_total, a_total, has_extra_time) in legs:
                if hs is None or aws is None:
                    all_settled = False
                    continue
                hs_reg, as_reg = regulation_score(
                    int(hs), int(aws),
                    int(h90) if h90 is not None else None,
                    int(a90) if a90 is not None else None,
                    int(h_total) if h_total is not None else None,
                    int(a_total) if a_total is not None else None,
                    bool(has_extra_time),
                )
                leg_result = settle_result(str(selection_code), hs_reg, as_reg)
                cursor.execute(
                    "UPDATE model.parlay_legs SET leg_result = %s WHERE leg_id = %s",
                    (leg_result, int(leg_id)),
                )
                if leg_result == "LOST":
                    any_lost = True

            # Un combine perdu des qu'une jambe tombe (meme si d'autres en
            # attente) ; gagne seulement quand TOUTES sont reglees et gagnees.
            if any_lost:
                result = "LOST"
                profit = -float(stake_amount)
            elif all_settled:
                result = "WON"
                profit = round(float(stake_amount) * (float(combined_odd) - 1.0), 4)
            else:
                continue  # encore en attente

            cursor.execute(
                "UPDATE model.parlay_tickets SET result_code = %s, profit_units = %s, "
                "settled_at = now() WHERE parlay_id = %s",
                (result, profit, int(parlay_id)),
            )
            settled += 1
            if result == "WON":
                won += 1
            profit_total += profit
    connection.commit()
    return {
        "newly_settled": settled,
        "newly_won": won,
        "newly_profit_units": round(profit_total, 2),
    }
