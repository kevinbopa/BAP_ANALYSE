"""Regles de reglement golf (logique pure, testable).

Position finale DataGolf (in-play `current_pos`) : "1", "T5", "CUT", "WD",
"DQ", "MC". On en derive un rang numerique + le fait d'avoir passe le cut,
puis on regle chaque type de pari.
"""
from __future__ import annotations


def parse_position(text: object) -> tuple[int | None, bool]:
    """(rang, a_passe_le_cut). "T5"->(5,True) ; "CUT"/"WD"/"DQ"->(None,False)."""
    raw = str(text or "").strip().upper()
    if raw.startswith("T"):
        raw = raw[1:]
    if raw.isdigit():
        return int(raw), True
    return None, False


def settle_outright(market_code: str, rank: int | None, made_cut: bool) -> str:
    """WON/LOST pour un pari outright, selon la position finale."""
    if market_code == "MAKE_CUT":
        return "WON" if made_cut else "LOST"
    if rank is None:
        return "LOST"
    if market_code == "TOURNAMENT_WINNER":
        return "WON" if rank == 1 else "LOST"
    threshold = {"TOP_3": 3, "TOP_5": 5, "TOP_10": 10, "TOP_20": 20}.get(market_code)
    if threshold is not None:
        return "WON" if rank <= threshold else "LOST"
    # Marche inconnu : on ne regle pas (VOID plutot que faux resultat).
    return "VOID"


def settle_matchup(pick_rank: int | None, opp_rank: int | None) -> str:
    """WON si le pick finit MIEUX (rang plus petit) que l'adversaire. Un joueur
    non classe (cut/WD) est traite comme pire que tout classe. Egalite -> PUSH."""
    a = pick_rank if pick_rank is not None else 10_000
    b = opp_rank if opp_rank is not None else 10_000
    if a == b:
        return "PUSH"
    return "WON" if a < b else "LOST"


def flat_profit(result_code: str, market_odd: float | None) -> float | None:
    """Mise plate 1 unite : WON -> cote-1, LOST -> -1, PUSH/VOID -> 0."""
    if market_odd is None or market_odd <= 1.0:
        return None
    if result_code == "WON":
        return round(float(market_odd) - 1.0, 4)
    if result_code == "LOST":
        return -1.0
    return 0.0
