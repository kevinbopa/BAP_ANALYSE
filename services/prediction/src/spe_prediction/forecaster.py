"""Pure prediction layer: turn outcome probabilities into a single
predicted verdict ("who is going to win, and how sure are we?"), without
any value-bet logic.

This is intentionally separate from ``decision.py`` so the two questions
(prediction vs. value-bet recommendation) stay independently testable
and independently displayable in the UI.
"""
from __future__ import annotations

from spe_prediction.domain import OutcomeProbabilities, PredictionVerdict


SELECTION_LABELS: dict[str, str] = {
    "HOME": "Victoire domicile",
    "DRAW": "Match nul",
    "AWAY": "Victoire exterieur",
}

# Seuils du "nul competitif". Mesure sur 32 895 matchs walk-forward
# (2026-07-05) : p_draw est bien calibre (25-30% predit -> 27.7% realise)
# mais ne depasse structurellement presque jamais max(home, away) — forcer
# le verdict au nul DEGRADE l'accuracy (50.05% -> 49.99% des la regle la
# plus timide). L'argmax reste donc le pronostic ; quand le nul talonne,
# on le montre comme scenario au lieu de le taire.
DRAW_COMPETITIVE_FLOOR = 0.28
DRAW_COMPETITIVE_GAP = 0.08


def draw_is_competitive(home: float, draw: float, away: float) -> bool:
    """Le nul talonne-t-il le pronostic sans le dominer ?"""
    top = max(home, away)
    # 1e-9 : tolerance d'arithmetique flottante sur la frontiere du gap.
    return (
        draw < top
        and draw >= DRAW_COMPETITIVE_FLOOR
        and (top - draw) <= DRAW_COMPETITIVE_GAP + 1e-9
    )


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def compute_surety(top_probability: float, margin: float, confidence_score: float, knowledge_signal: float = 0.0) -> float:
    """Compute the surety score for a predicted outcome.

    Parameters
    ----------
    top_probability:
        Probability of the most likely outcome (0..1).
    margin:
        Gap between top probability and runner-up (0..1).
    confidence_score:
        How epistemically grounded the model estimate is (0..1).
    knowledge_signal:
        Optional 0..1 signal of how much data backs the prediction. Lets the
        engine reduce surety when the prediction is mostly a market echo.
    """
    base = 0.18
    surety = (
        base
        + 0.28 * _clamp01(top_probability)
        + 0.32 * _clamp01(margin)
        + 0.12 * _clamp01(confidence_score)
        + 0.10 * _clamp01(knowledge_signal)
    )
    return _clamp01(surety)


def build_verdict(
    probabilities: OutcomeProbabilities,
    confidence_score: float,
    knowledge_signal: float = 0.0,
) -> PredictionVerdict:
    """Pick the most likely outcome and score how sure we are of it."""
    ordered = sorted(probabilities.as_dict().items(), key=lambda kv: kv[1], reverse=True)
    top_code, top_probability = ordered[0]
    runner_up_probability = ordered[1][1] if len(ordered) > 1 else 0.0
    margin = max(0.0, top_probability - runner_up_probability)
    surety = compute_surety(top_probability, margin, confidence_score, knowledge_signal)
    label = SELECTION_LABELS.get(top_code, top_code)
    draw_competitive = top_code != "DRAW" and draw_is_competitive(
        probabilities.home, probabilities.draw, probabilities.away
    )
    rationale = (
        f"Pronostic {label}: probabilite {top_probability:.1%}, "
        f"ecart au choix suivant {margin:.1%}, "
        f"confiance modele {confidence_score:.0%}"
    )
    if draw_competitive:
        rationale += (
            f" | Nul competitif ({probabilities.draw:.1%}, "
            f"a {max(0.0, top_probability - probabilities.draw):.1%} du pronostic)"
        )
    return PredictionVerdict(
        selection_code=top_code,
        label=label,
        probability=top_probability,
        runner_up_probability=runner_up_probability,
        margin=margin,
        surety_score=surety,
        rationale=rationale,
        draw_competitive=draw_competitive,
    )
