"""Prediction Golf V2 — modele DataGolf.

    py services/prediction/run_golf_predictions.py

Le modele n'est plus un consensus de cotes mais le modele DataGolf lui-meme
(strokes-gained + course-fit), ingere dans core.golf_odds / core.golf_matchup_odds
sous le book synthetique DATAGOLF (fair-odd sans vig). Ce runner (role spe_app_rw) :
  * copie DATAGOLF  -> model.golf_predictions (win/top5/10/20/make_cut)
  * copie DATAGOLF  -> model.golf_matchup_predictions (proba de chaque duel)
  * detecte les deals outrights : un book reel paie plus que la proba modele
  * detecte les deals matchups : book devige (2/3-way) vs proba modele

Repli : si DATAGOLF est absent (feed indispo), on ne casse rien — 0 prediction.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import date
import json
from pathlib import Path
import sys

sys.path.insert(0, str((Path(__file__).resolve().parent / "src").resolve()))

from spe_prediction.config import get_env
from spe_prediction.db import DatabaseSettings, connect_db
from spe_prediction.golf_engine import build_market_predictions, clamp_probability
from spe_prediction.golf_markets import golf_spec, is_longshot_reject

MODEL_BOOK = "DATAGOLF"
# On ne compare jamais le modele a un exchange (prix placeholder) ni a lui-meme.
EXCLUDE_BOOKS_SQL = (
    "b.bookmaker_code <> 'DATAGOLF' "
    "AND b.bookmaker_code !~ '_EX_' "
    "AND b.bookmaker_code NOT IN ('MATCHBOOK', 'SMARKETS', 'BETDAQ')"
)

# FRAICHEUR : une cote qui a plus de N heures depuis sa derniere capture est
# consideree comme RETIREE par le book. Sans ce filtre, on genere des deals
# actifs sur des cotes gelees (bug observe 2026-07-24 : deal Conners vs
# Keefer proposait cote bet365 datant du 20 juillet alors que bet365 avait
# retire le matchup depuis).
# - Matchup : expire vite (jour du tournoi ou entre les tours)
# - Outright : plus stable (peut tenir toute la semaine du tournoi)
_MATCHUP_ODDS_MAX_AGE_HOURS = 36
_OUTRIGHT_ODDS_MAX_AGE_HOURS = 96
FRESH_MATCHUP_SQL = f"mo.captured_at >= now() - interval '{_MATCHUP_ODDS_MAX_AGE_HOURS} hours'"
FRESH_OUTRIGHT_SQL = f"go.captured_at >= now() - interval '{_OUTRIGHT_ODDS_MAX_AGE_HOURS} hours'"

# ROUND_MATCHUP : matchup sur UN round precis (R1/R2/R3/R4). Aucune colonne
# round_number en base -> impossible de savoir a quel tour ca correspond.
# Regle de surete : une fois le tournoi commence (date_start passe), les
# ROUND_MATCHUP sont trop risques a proposer sans indication de tour.
ROUND_MATCHUP_GUARD_SQL = (
    "(mo.market_code <> 'ROUND_MATCHUP' "
    "OR COALESCE(gt.date_start, gt.commence_time::date) > now()::date)"
)

# Seuils/garde-fous : isoles PAR MARCHE dans spe_prediction.golf_markets
# (registre GOLF_MARKET_SPECS) -> chaque type de pari a son propre systeme.


def _target_books() -> tuple[str, ...]:
    for name in ("SPE_GOLF_TARGET_BOOKMAKERS", "SPE_TARGET_BOOKMAKER", "THEODDS_API_BOOKMAKERS"):
        raw = get_env(name, "")
        vals = tuple(v.strip().upper() for v in raw.split(",") if v.strip())
        if vals:
            return vals
    return ()


def _valid_date_arg(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        raise argparse.ArgumentTypeError(f"Date invalide: {value}")


def _scope_sql(alias: str, args: argparse.Namespace, params: list) -> str:
    clauses: list[str] = []
    if args.golf_tournament_id:
        clauses.append(f"{alias}.golf_tournament_id = %s")
        params.append(args.golf_tournament_id)
    if args.tour:
        clauses.append(f"{alias}.tour_code = ANY(%s)")
        params.append([tour.strip().lower() for tour in args.tour if tour.strip()])
    date_expr = f"COALESCE({alias}.date_start, {alias}.commence_time::date)"
    if args.date_from:
        clauses.append(f"{date_expr} >= %s::date")
        params.append(args.date_from.isoformat())
    if args.date_to:
        clauses.append(f"{date_expr} <= %s::date")
        params.append(args.date_to.isoformat())
    return (" AND " + " AND ".join(clauses)) if clauses else ""


# --- Outrights --------------------------------------------------------------
def _model_outrights(cursor, args: argparse.Namespace) -> list[tuple]:
    params: list = [MODEL_BOOK]
    scope = _scope_sql("gt", args, params)
    cursor.execute(
        f"""
        SELECT DISTINCT ON (go.golf_tournament_id, go.market_code, go.selection_name)
            go.golf_tournament_id, go.golf_player_id, go.market_code,
            go.selection_name, go.decimal_odd
        FROM core.golf_odds go
        JOIN core.bookmakers b ON b.bookmaker_id = go.bookmaker_id
        JOIN core.golf_tournaments gt ON gt.golf_tournament_id = go.golf_tournament_id
        JOIN core.golf_pretournament_preds pp
          ON pp.golf_tournament_id = go.golf_tournament_id
         AND pp.golf_player_id = go.golf_player_id
         AND pp.market_code = go.market_code
        WHERE gt.completed_at IS NULL AND b.bookmaker_code = %s
          AND {FRESH_OUTRIGHT_SQL} {scope}
        ORDER BY go.golf_tournament_id, go.market_code, go.selection_name, go.captured_at DESC
        """,
        params,
    )
    return cursor.fetchall()


def _best_book_outrights(cursor, target_books: tuple[str, ...], args: argparse.Namespace) -> dict[tuple, tuple]:
    """Meilleure cote reelle par (tournoi, marche, selection). Si des books
    cibles sont configures on s'y limite (ex: bet365)."""
    where_target = ""
    params: list = []
    if target_books:
        where_target = "AND b.bookmaker_code = ANY(%s)"
        params.append(list(target_books))
    scope = _scope_sql("gt", args, params)
    cursor.execute(
        f"""
        WITH latest AS (
            SELECT DISTINCT ON (go.golf_tournament_id, go.market_code, go.selection_name, go.bookmaker_id)
                go.golf_tournament_id, go.golf_player_id, go.market_code, go.selection_name,
                go.bookmaker_id, go.decimal_odd
            FROM core.golf_odds go
            JOIN core.bookmakers b ON b.bookmaker_id = go.bookmaker_id
            JOIN core.golf_tournaments gt ON gt.golf_tournament_id = go.golf_tournament_id
            JOIN core.golf_pretournament_preds pp
              ON pp.golf_tournament_id = go.golf_tournament_id
             AND pp.golf_player_id = go.golf_player_id
             AND pp.market_code = go.market_code
            WHERE gt.completed_at IS NULL AND {EXCLUDE_BOOKS_SQL}
              AND {FRESH_OUTRIGHT_SQL} {where_target} {scope}
            ORDER BY go.golf_tournament_id, go.market_code, go.selection_name,
                     go.bookmaker_id, go.captured_at DESC
        )
        SELECT DISTINCT ON (golf_tournament_id, market_code, selection_name)
            golf_tournament_id, market_code, selection_name, bookmaker_id, decimal_odd
        FROM latest
        ORDER BY golf_tournament_id, market_code, selection_name, decimal_odd DESC
        """,
        params,
    )
    best: dict[tuple, tuple] = {}
    for tid, market, selection, book_id, odd in cursor.fetchall():
        best[(tid, market, selection)] = (int(book_id), float(odd))
    return best


def _engine_outrights(cursor, args: argparse.Namespace) -> list[tuple]:
    """Source primaire V3 : probas pre-tournoi (2 modeles) -> moteur
    (blend + normalisation par marche) sur TOUT le field.
    Retourne des rows unifiees (tid, player_id, market, selection, prob)."""
    params: list = []
    scope = _scope_sql("gt", args, params)
    cursor.execute(
        f"""
        SELECT p.golf_tournament_id, p.golf_player_id, p.dg_id, p.player_name,
               p.market_code, p.prob_baseline, p.prob_fit
        FROM core.golf_pretournament_preds p
        JOIN core.golf_tournaments gt ON gt.golf_tournament_id = p.golf_tournament_id
        WHERE gt.completed_at IS NULL {scope}
        """,
        params,
    )
    grouped: dict[tuple, list[dict]] = defaultdict(list)
    for tid, pid, dg_id, name, market, p_base, p_fit in cursor.fetchall():
        grouped[(int(tid), str(market))].append({
            "dg_id": int(dg_id), "player_id": int(pid) if pid is not None else None,
            "selection_name": str(name or ""),
            "prob_baseline": float(p_base) if p_base is not None else None,
            "prob_fit": float(p_fit) if p_fit is not None else None,
        })
    rows: list[tuple] = []
    for (tid, market), players in grouped.items():
        for pred in build_market_predictions(players, market):
            rows.append((tid, pred.player_id, market, pred.selection_name, pred.probability))
    return rows


def _book_fallback_rows(model_rows) -> list[tuple]:
    """Repli : rows du pseudo-book DATAGOLF (prob = 1/fair), meme forme."""
    rows: list[tuple] = []
    for tid, player_id, market, selection, dg_odd in model_rows:
        dg_odd = float(dg_odd)
        if dg_odd <= 1.0:
            continue
        probability = clamp_probability(1.0 / dg_odd)
        if probability is None:
            continue
        rows.append((tid, player_id, market, selection, probability))
    return rows


def _write_outright_predictions(cursor, prob_rows, method: str) -> int:
    count = 0
    for tid, player_id, market, selection, prob in prob_rows:
        stored_probability = clamp_probability(prob)
        if stored_probability is None:
            continue
        cursor.execute(
            """
            INSERT INTO model.golf_predictions (
                golf_tournament_id, golf_player_id, market_code, selection_name,
                model_probability, model_fair_odd, method_code, feature_snapshot_json
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            """,
            (
                tid, player_id, market, selection, round(stored_probability, 6),
                round(1.0 / stored_probability, 4), method,
                json.dumps({"source": "datagolf", "method": method}, ensure_ascii=True),
            ),
        )
        count += 1
    return count


def _outright_deals(cursor, prob_rows, best_book) -> int:
    # Deals preserves (position reelle) : ne pas recreer le meme deal a cote.
    cursor.execute(
        """
        SELECT gd.golf_tournament_id, gd.market_code, gd.selection_name
        FROM model.golf_deals gd
        WHERE gd.status_code='ACTIVE' AND gd.result_code IS NULL
          AND EXISTS (SELECT 1 FROM model.user_bet_positions p
                      WHERE p.golf_deal_id = gd.golf_deal_id)
        """
    )
    preserved = {(int(t), str(m), str(s)) for t, m, s in cursor.fetchall()}
    # Chaque marche a SON spec isole (seuils, longshot, plafond) -> un marche
    # bruite (vainqueur) ne contamine pas un marche fiable (top-20).
    grouped: dict[tuple, list[dict]] = defaultdict(list)
    for tid, player_id, market, selection, prob in prob_rows:
        if (int(tid), str(market), str(selection)) in preserved:
            continue
        spec = golf_spec(market)
        if spec is None or spec.is_matchup:
            continue
        if prob < spec.min_prob:
            continue
        book = best_book.get((tid, market, selection))
        if not book:
            continue
        book_id, book_odd = book
        implied = 1.0 / book_odd
        edge = prob - implied
        if edge < spec.min_edge or edge > spec.max_edge:
            continue
        if is_longshot_reject(spec, implied, edge):
            continue
        grouped[(tid, market)].append({
            "tid": tid, "player_id": player_id, "market": market, "selection": selection,
            "book_id": book_id, "prob": prob, "implied": implied, "edge": edge, "odd": book_odd,
        })

    written = 0
    for (tid, market), candidates in grouped.items():
        cap = golf_spec(market).max_per_tournament
        for deal in sorted(candidates, key=lambda d: d["edge"], reverse=True)[:cap]:
            cursor.execute(
                """
                INSERT INTO model.golf_deals (
                    golf_tournament_id, golf_player_id, bookmaker_id, market_code,
                    selection_name, model_probability, implied_probability,
                    edge_probability, market_odd
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (deal["tid"], deal["player_id"], deal["book_id"], deal["market"], deal["selection"],
                 round(deal["prob"], 6), round(deal["implied"], 6), round(deal["edge"], 6),
                 round(deal["odd"], 4)),
            )
            written += 1
    return written


# --- Matchups ---------------------------------------------------------------
def _model_matchups(cursor, args: argparse.Namespace) -> dict[tuple, dict]:
    """Proba modele par (tournoi, marche, p1, p2) depuis le book DATAGOLF."""
    params: list = [MODEL_BOOK]
    scope = _scope_sql("gt", args, params)
    cursor.execute(
        f"""
        SELECT DISTINCT ON (mo.golf_tournament_id, mo.market_code,
                            mo.p1_golf_player_id, mo.p2_golf_player_id)
            mo.golf_tournament_id, mo.market_code,
            mo.p1_golf_player_id, mo.p2_golf_player_id, mo.p3_golf_player_id,
            mo.p1_odd, mo.p2_odd, mo.p3_odd, mo.tie_odd
        FROM core.golf_matchup_odds mo
        JOIN core.bookmakers b ON b.bookmaker_id = mo.bookmaker_id
        JOIN core.golf_tournaments gt ON gt.golf_tournament_id = mo.golf_tournament_id
        JOIN core.golf_pretournament_preds pp1
          ON pp1.golf_tournament_id = mo.golf_tournament_id
         AND pp1.golf_player_id = mo.p1_golf_player_id
        JOIN core.golf_pretournament_preds pp2
          ON pp2.golf_tournament_id = mo.golf_tournament_id
         AND pp2.golf_player_id = mo.p2_golf_player_id
        WHERE gt.completed_at IS NULL AND b.bookmaker_code = %s
          AND {FRESH_MATCHUP_SQL} AND {ROUND_MATCHUP_GUARD_SQL} {scope}
        ORDER BY mo.golf_tournament_id, mo.market_code,
                 mo.p1_golf_player_id, mo.p2_golf_player_id, mo.captured_at DESC
        """,
        params,
    )

    def prob(odd):
        return 1.0 / float(odd) if odd and float(odd) > 1.0 else None

    model: dict[tuple, dict] = {}
    for tid, market, p1, p2, p3, p1o, p2o, p3o, tieo in cursor.fetchall():
        model[(tid, market, p1, p2)] = {
            "p3": p3, "p1": prob(p1o), "p2": prob(p2o), "p3p": prob(p3o), "tie": prob(tieo),
        }
    return model


def _write_matchup_predictions(cursor, model) -> int:
    count = 0
    for (tid, market, p1, p2), m in model.items():
        if m["p1"] is None and m["p2"] is None:
            continue
        cursor.execute(
            """
            INSERT INTO model.golf_matchup_predictions (
                golf_tournament_id, market_code, p1_golf_player_id, p2_golf_player_id,
                p3_golf_player_id, p1_probability, p2_probability, p3_probability, tie_probability
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (tid, market, p1, p2, m["p3"],
             _round(m["p1"]), _round(m["p2"]), _round(m["p3p"]), _round(m["tie"])),
        )
        count += 1
    return count


def _matchup_deals(cursor, model, target_books: tuple[str, ...], args: argparse.Namespace) -> int:
    where_target = ""
    params: list = []
    if target_books:
        where_target = "AND b.bookmaker_code = ANY(%s)"
        params.append(list(target_books))
    scope = _scope_sql("gt", args, params)
    cursor.execute(
        f"""
        SELECT DISTINCT ON (mo.golf_tournament_id, mo.market_code, mo.bookmaker_id,
                            mo.p1_golf_player_id, mo.p2_golf_player_id)
            mo.golf_tournament_id, mo.market_code, mo.bookmaker_id,
            mo.p1_golf_player_id, mo.p2_golf_player_id, mo.p3_golf_player_id,
            mo.p1_odd, mo.p2_odd, mo.p3_odd, mo.tie_odd
        FROM core.golf_matchup_odds mo
        JOIN core.bookmakers b ON b.bookmaker_id = mo.bookmaker_id
        JOIN core.golf_tournaments gt ON gt.golf_tournament_id = mo.golf_tournament_id
        JOIN core.golf_pretournament_preds pp1
          ON pp1.golf_tournament_id = mo.golf_tournament_id
         AND pp1.golf_player_id = mo.p1_golf_player_id
        JOIN core.golf_pretournament_preds pp2
          ON pp2.golf_tournament_id = mo.golf_tournament_id
         AND pp2.golf_player_id = mo.p2_golf_player_id
        WHERE gt.completed_at IS NULL AND {EXCLUDE_BOOKS_SQL}
          AND {FRESH_MATCHUP_SQL} AND {ROUND_MATCHUP_GUARD_SQL} {where_target} {scope}
        ORDER BY mo.golf_tournament_id, mo.market_code, mo.bookmaker_id,
                 mo.p1_golf_player_id, mo.p2_golf_player_id, mo.captured_at DESC
        """,
        params,
    )

    # Meilleure value par (tournoi, marche, cote-du-duel) toutes books confondues.
    best: dict[tuple, dict] = {}
    for row in cursor.fetchall():
        tid, market, book_id, p1, p2, p3, p1o, p2o, p3o, tieo = row
        spec = golf_spec(market)
        if spec is None or not spec.is_matchup:
            continue
        m = model.get((tid, market, p1, p2))
        if not m:
            continue
        sides = [(p1, p1, p2, _f(p1o), m["p1"]), (p2, p2, p1, _f(p2o), m["p2"])]
        if p3 is not None:
            sides.append((p3, p3, p1, _f(p3o), m["p3p"]))
        # Overround du book pour deviger (2 ou 3-way + tie).
        book_imps = [1.0 / o for (_pk, _a, _b, o, _mp) in sides if o]
        if tieo and _f(tieo):
            book_imps.append(1.0 / _f(tieo))
        overround = sum(book_imps)
        if overround <= 0:
            continue
        for pick, pick_id, opp_id, odd, model_p in sides:
            if odd is None or model_p is None or odd <= 1.0:
                continue
            if model_p < spec.min_prob:
                continue
            fair_book = (1.0 / odd) / overround
            edge = model_p - fair_book
            if edge < spec.min_edge or edge > spec.max_edge:
                continue
            key = (tid, market, pick_id, opp_id)
            if key not in best or edge > best[key]["edge"]:
                best[key] = {
                    "tid": tid, "market": market, "book_id": int(book_id),
                    "pick": pick_id, "opp": opp_id, "prob": model_p,
                    "implied": fair_book, "edge": edge, "odd": odd,
                }

    # Matchups preserves (position reelle) : ne pas recreer le meme deal.
    cursor.execute(
        """
        SELECT gmd.golf_tournament_id, gmd.market_code,
               gmd.pick_golf_player_id, gmd.opponent_golf_player_id
        FROM model.golf_matchup_deals gmd
        WHERE gmd.status_code='ACTIVE' AND gmd.result_code IS NULL
          AND EXISTS (SELECT 1 FROM model.user_bet_positions p
                      WHERE p.golf_matchup_deal_id = gmd.golf_matchup_deal_id)
        """
    )
    preserved_m = {(int(t), str(m), int(pk), int(op)) for t, m, pk, op in cursor.fetchall()}
    grouped: dict[tuple, list[dict]] = defaultdict(list)
    for deal in best.values():
        if (int(deal["tid"]), str(deal["market"]), int(deal["pick"]), int(deal["opp"])) in preserved_m:
            continue
        grouped[(deal["tid"], deal["market"])].append(deal)
    written = 0
    for (tid, market), candidates in grouped.items():
        cap = golf_spec(market).max_per_tournament
        for deal in sorted(candidates, key=lambda d: d["edge"], reverse=True)[:cap]:
            cursor.execute(
                """
                INSERT INTO model.golf_matchup_deals (
                    golf_tournament_id, market_code, bookmaker_id,
                    pick_golf_player_id, opponent_golf_player_id,
                    model_probability, implied_probability, edge_probability, market_odd
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (deal["tid"], deal["market"], deal["book_id"], deal["pick"], deal["opp"],
                 round(deal["prob"], 6), round(deal["implied"], 6), round(deal["edge"], 6),
                 round(deal["odd"], 4)),
            )
            written += 1
    return written


def _round(value):
    return round(value, 6) if value is not None else None


def _f(value):
    return float(value) if value is not None else None


def main() -> int:
    parser = argparse.ArgumentParser(description="Predictions/deals golf scoper par championnat.")
    parser.add_argument("--golf-tournament-id", type=int, default=None)
    parser.add_argument("--tour", action="append", help="Tour DataGolf a calculer (repete possible).")
    parser.add_argument("--date-from", type=_valid_date_arg, default=None)
    parser.add_argument("--date-to", type=_valid_date_arg, default=None)
    args = parser.parse_args()
    target_books = _target_books()
    connection = connect_db(DatabaseSettings.from_env())
    report = {
        "model_book": MODEL_BOOK,
        "target_books": list(target_books) if target_books else ["ALL"],
        "scope": {
            "golf_tournament_id": args.golf_tournament_id,
            "tour": args.tour or [],
            "date_from": args.date_from.isoformat() if args.date_from else None,
            "date_to": args.date_to.isoformat() if args.date_to else None,
        },
        "predictions": 0, "deals": 0,
        "matchup_predictions": 0, "matchup_deals": 0,
    }
    try:
        with connection.cursor() as cursor:
            # Annule les deals actifs precedents (on re-detecte a chaque run)
            # SAUF ceux qui portent une POSITION REELLE (argent engage) : ces
            # deals doivent vivre jusqu'au reglement avec le VRAI resultat —
            # jamais VOID par simple re-run.
            cancel_params: list = []
            cancel_scope = _scope_sql("gt", args, cancel_params)
            cursor.execute(
                f"""
                UPDATE model.golf_deals gd
                SET status_code='CANCELLED', result_code='VOID',
                    profit_units=0, settled_at=now()
                FROM core.golf_tournaments gt
                WHERE gt.golf_tournament_id = gd.golf_tournament_id
                  AND gd.status_code='ACTIVE' AND gd.result_code IS NULL {cancel_scope}
                  AND NOT EXISTS (SELECT 1 FROM model.user_bet_positions p
                                  WHERE p.golf_deal_id = gd.golf_deal_id)
                """,
                cancel_params,
            )
            cancel_matchup_params: list = []
            cancel_matchup_scope = _scope_sql("gt", args, cancel_matchup_params)
            cursor.execute(
                f"""
                UPDATE model.golf_matchup_deals gmd
                SET status_code='CANCELLED', result_code='VOID',
                    profit_units=0, settled_at=now()
                FROM core.golf_tournaments gt
                WHERE gt.golf_tournament_id = gmd.golf_tournament_id
                  AND gmd.status_code='ACTIVE' AND gmd.result_code IS NULL {cancel_matchup_scope}
                  AND NOT EXISTS (SELECT 1 FROM model.user_bet_positions p
                                  WHERE p.golf_matchup_deal_id = gmd.golf_matchup_deal_id)
                """,
                cancel_matchup_params,
            )

            # Moteur V3 (pretournament, blend+normalisation) prioritaire ;
            # repli pseudo-book DATAGOLF pour les (tournoi, marche) absents.
            engine_rows = _engine_outrights(cursor, args)
            engine_keys = {(tid, market) for tid, _p, market, _s, _pr in engine_rows}
            fallback_rows = [
                row for row in _book_fallback_rows(_model_outrights(cursor, args))
                if (row[0], row[2]) not in engine_keys
            ]
            report["predictions"] = (
                _write_outright_predictions(cursor, engine_rows, "GOLF_ENGINE_V3")
                + _write_outright_predictions(cursor, fallback_rows, "GOLF_DATAGOLF_V2")
            )
            report["engine_rows"] = len(engine_rows)
            report["fallback_rows"] = len(fallback_rows)
            best_book = _best_book_outrights(cursor, target_books, args)
            report["deals"] = _outright_deals(cursor, engine_rows + fallback_rows, best_book)

            matchup_model = _model_matchups(cursor, args)
            report["matchup_predictions"] = _write_matchup_predictions(cursor, matchup_model)
            report["matchup_deals"] = _matchup_deals(cursor, matchup_model, target_books, args)
        connection.commit()
    finally:
        connection.close()
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
