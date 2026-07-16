"""Registre : UN systeme isole par type de pari golf.

Chaque marche golf a des caracteristiques tres differentes (le vainqueur est
un marche de longshots bruites ; le top-20 est court et fiable ; le make-cut
est ~50/50 efficace ; les matchups sont 2/3-way). Les mettre sous un seul jeu
de seuils degrade la precision. Ce registre isole chaque marche avec ses
propres garde-fous, comme market_models.py pour le foot.

Ajouter/tuner un marche = une ligne ici, sans toucher au moteur.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GolfMarketSpec:
    code: str
    display: str
    min_edge: float          # edge minimal pour un deal
    max_edge: float          # au-dela = trop beau => donnee suspecte, rejet
    min_prob: float          # proba modele minimale (evite le bruit longshot)
    longshot_implied: float  # si implied < ce seuil ...
    longshot_edge: float     # ... ET edge > ce seuil => news probable, rejet
    max_per_tournament: int  # plafond de deals par tournoi (diversification)
    is_matchup: bool = False


# Outrights (proba = 1/fair DataGolf ; le vainqueur est le plus bruite).
GOLF_MARKET_SPECS: dict[str, GolfMarketSpec] = {
    "TOURNAMENT_WINNER": GolfMarketSpec(
        "TOURNAMENT_WINNER", "Vainqueur du tournoi",
        min_edge=0.030, max_edge=0.15, min_prob=0.008,
        longshot_implied=0.02, longshot_edge=0.05, max_per_tournament=5),
    "TOP_5": GolfMarketSpec(
        "TOP_5", "Top 5",
        min_edge=0.028, max_edge=0.18, min_prob=0.020,
        longshot_implied=0.03, longshot_edge=0.07, max_per_tournament=6),
    "TOP_10": GolfMarketSpec(
        "TOP_10", "Top 10",
        min_edge=0.025, max_edge=0.18, min_prob=0.030,
        longshot_implied=0.04, longshot_edge=0.08, max_per_tournament=6),
    "TOP_20": GolfMarketSpec(
        "TOP_20", "Top 20",
        min_edge=0.022, max_edge=0.16, min_prob=0.050,
        longshot_implied=0.06, longshot_edge=0.10, max_per_tournament=8),
    "MAKE_CUT": GolfMarketSpec(
        "MAKE_CUT", "Passe le cut",
        min_edge=0.035, max_edge=0.12, min_prob=0.100,
        longshot_implied=0.0, longshot_edge=1.0, max_per_tournament=6),
    # Matchups (dejaa devigues cote moteur ; pas de failsafe longshot).
    "TOURNAMENT_MATCHUP": GolfMarketSpec(
        "TOURNAMENT_MATCHUP", "Duel tournoi",
        min_edge=0.030, max_edge=0.20, min_prob=0.20,
        longshot_implied=0.0, longshot_edge=1.0, max_per_tournament=8, is_matchup=True),
    "ROUND_MATCHUP": GolfMarketSpec(
        "ROUND_MATCHUP", "Duel du tour",
        min_edge=0.035, max_edge=0.22, min_prob=0.20,
        longshot_implied=0.0, longshot_edge=1.0, max_per_tournament=6, is_matchup=True),
    "THREE_BALL": GolfMarketSpec(
        "THREE_BALL", "3-balls",
        min_edge=0.040, max_edge=0.25, min_prob=0.15,
        longshot_implied=0.0, longshot_edge=1.0, max_per_tournament=6, is_matchup=True),
}


def golf_spec(market_code: str) -> GolfMarketSpec | None:
    return GOLF_MARKET_SPECS.get(str(market_code))


def is_longshot_reject(spec: GolfMarketSpec, implied: float, edge: float) -> bool:
    """Elite bien price par le modele mais donne tres gros par un book =
    souvent une news (blessure/forfait). Desactive si longshot_edge >= 1."""
    return implied < spec.longshot_implied and edge > spec.longshot_edge
