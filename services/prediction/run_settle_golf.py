"""Reglement des deals golf (outrights + matchups) sur les positions finales.

    py services/prediction/run_settle_golf.py

Un tournoi est regle quand sa date de fin est passee ET que des resultats
(core.golf_results, via DataGolf in-play) sont en base. On solde alors chaque
deal (WON/LOST/VOID/PUSH + profit unite) et on marque le tournoi termine.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys

sys.path.insert(0, str((Path(__file__).resolve().parent / "src").resolve()))

from spe_prediction.db import DatabaseSettings, connect_db
from spe_prediction.golf_settlement import flat_profit, settle_matchup, settle_outright


def main() -> int:
    connection = connect_db(DatabaseSettings.from_env())
    report = {"tournaments_settled": 0, "outright_settled": 0, "matchup_settled": 0}
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT gt.golf_tournament_id
                FROM core.golf_tournaments gt
                WHERE gt.date_end IS NOT NULL AND gt.date_end < current_date
                  AND EXISTS (SELECT 1 FROM core.golf_results r
                              WHERE r.golf_tournament_id = gt.golf_tournament_id)
                  AND (
                      gt.completed_at IS NULL
                      -- Tournoi deja complete mais avec des deals encore non
                      -- regles (ex: deals preserves par une position reelle,
                      -- crees/gardes apres le premier reglement) : on repasse.
                      OR EXISTS (SELECT 1 FROM model.golf_deals gd
                                 WHERE gd.golf_tournament_id = gt.golf_tournament_id
                                   AND gd.result_code IS NULL)
                      OR EXISTS (SELECT 1 FROM model.golf_matchup_deals gmd
                                 WHERE gmd.golf_tournament_id = gt.golf_tournament_id
                                   AND gmd.result_code IS NULL)
                  )
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

                # Outrights.
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

                # Matchups.
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
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
