"""Moteur de prediction golf V3 (pur, testable) — architecture facon foot.

Source primaire : les probas pre-tournoi DataGolf des DEUX modeles
(baseline = skill seul ; fit = skill + historique de parcours). Le moteur :

  1. BLEND les deux modeles (comme le blend multi-signaux du foot) — le fit
     est le meilleur mais peut sur-apprendre le course-fit sur peu de rondes ;
     le baseline le regularise.
  2. NORMALISE par marche : le vainqueur somme a 1, un top-N somme a N
     (exactement N joueurs finissent top N, dead-heat mis a part). Les probas
     brutes derivees de cotes/percent ne somment jamais juste.
  3. Emet des predictions homogenes (proba + fair odd) pour TOUT le field,
     pas seulement les joueurs cotes.

MAKE_CUT n'est pas normalise (le nombre de qualifies varie selon l'event).
"""
from __future__ import annotations

from dataclasses import dataclass

# Poids du modele course-fit dans le blend. 0.7 = on privilegie le fit
# (le meilleur modele DataGolf) tout en le regularisant par le baseline.
DEFAULT_FIT_WEIGHT = 0.70

# Cible de normalisation par marche (somme des probas sur le field).
_MARKET_TARGETS = {
    "TOURNAMENT_WINNER": 1.0,
    "TOP_5": 5.0,
    "TOP_10": 10.0,
    "TOP_20": 20.0,
}


@dataclass(frozen=True)
class GolfEnginePrediction:
    dg_id: int
    player_id: int | None
    selection_name: str
    market_code: str
    probability: float
    fair_odd: float | None


def blend_probability(
    prob_baseline: float | None,
    prob_fit: float | None,
    fit_weight: float = DEFAULT_FIT_WEIGHT,
) -> float | None:
    """Blend des deux modeles ; degrade sur celui qui existe."""
    if prob_fit is None and prob_baseline is None:
        return None
    if prob_fit is None:
        return float(prob_baseline)
    if prob_baseline is None:
        return float(prob_fit)
    w = min(1.0, max(0.0, fit_weight))
    return w * float(prob_fit) + (1.0 - w) * float(prob_baseline)


def normalize_market(
    probs: dict[int, float], market_code: str
) -> dict[int, float]:
    """Renormalise les probas d'un marche vers sa cible (1 pour le vainqueur,
    N pour un top-N). MAKE_CUT et marches inconnus : inchanges."""
    target = _MARKET_TARGETS.get(market_code)
    if target is None or not probs:
        return dict(probs)
    total = sum(p for p in probs.values() if p and p > 0)
    if total <= 0:
        return dict(probs)
    scale = target / total
    # Un top-N renormalise peut depasser 1 pour un ultra-favori -> clamp.
    return {k: min(0.999, max(0.0, p * scale)) for k, p in probs.items()}


def build_market_predictions(
    rows: list[dict],
    market_code: str,
    fit_weight: float = DEFAULT_FIT_WEIGHT,
) -> list[GolfEnginePrediction]:
    """rows: [{dg_id, player_id, selection_name, prob_baseline, prob_fit}].
    Blend -> normalisation -> predictions triees par proba decroissante."""
    blended: dict[int, float] = {}
    meta: dict[int, dict] = {}
    for row in rows:
        dg_id = int(row["dg_id"])
        p = blend_probability(row.get("prob_baseline"), row.get("prob_fit"), fit_weight)
        if p is None or p < 0:
            continue
        blended[dg_id] = p
        meta[dg_id] = row
    normalized = normalize_market(blended, market_code)
    predictions = [
        GolfEnginePrediction(
            dg_id=dg_id,
            player_id=meta[dg_id].get("player_id"),
            selection_name=str(meta[dg_id].get("selection_name") or ""),
            market_code=market_code,
            probability=round(p, 6),
            fair_odd=round(1.0 / p, 4) if p > 1e-9 else None,
        )
        for dg_id, p in normalized.items()
    ]
    predictions.sort(key=lambda x: -x.probability)
    return predictions
