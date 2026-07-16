"""Marches derives (over/under 2.5, BTTS) tires de la matrice Dixon-Coles.

La distribution complete des scores exacts (deja faconnee par les absences,
le contexte, le draw bias et les correlations SCORING_PATTERN) contient
toute l'information necessaire :

    P(over 2.5)  = somme des P(h, a) ou h + a >= 3
    P(BTTS oui)  = somme des P(h, a) ou h >= 1 et a >= 1

Le value betting reste LA methode : ces probabilites sont confrontees aux
cotes totals/btts de The Odds API avec exactement les memes gardes que le
1X2 (plancher d'edge, plafond dur, credibilite, failsafe longshot, Kelly
fractionnaire). Un marche a deux issues ne peut mathematiquement pas offrir
deux edges positifs face a un book avec marge : pas besoin de garde
anti-double-conviction ici.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import json
import math
from pathlib import Path
from statistics import median
from typing import Sequence

from spe_prediction.decision import (
    EDGE_HARD_CEILING,
    LONGSHOT_EDGE_CEILING,
    LONGSHOT_IMPLIED_THRESHOLD,
    _edge_credibility,
    _expected_value,
    _fair_odd,
    _fractional_kelly,
    _price_outlier_score,
    _ranking_score,
)
from spe_prediction.domain import DecisionConfig, ExactScorePrediction, SelectionDecision

# Selections (cote A, cote B) portees par chaque marche derive.
MARKET_SIDES: dict[str, tuple[str, str]] = {
    "OU25": ("OVER", "UNDER"),
    "OU15": ("OVER", "UNDER"),
    "OU35": ("OVER", "UNDER"),
    "BTTS": ("BTTS_YES", "BTTS_NO"),
    "DNB": ("HOME", "AWAY"),
}

OU25_GOAL_LINE = 2.5

# Calibration binaire par marche, ajustee sur l'historique reel (walk-forward,
# zero fuite) par run_derived_calibration_fit.py. alpha > 1 accentue les
# convictions, alpha < 1 les adoucit. Identite si le fichier est absent.
DERIVED_CALIBRATION_PATH = (
    Path(__file__).resolve().parents[2] / "models" / "calibration_derived.json"
)
# Borne alignee sur la grille du fitter (run_derived_calibration_fit.py, 0.20)
# sinon un alpha < 0.5 (marches derives peu discriminants) serait rejete et on
# retomberait sur l'identite 1.0 (aucune calibration).
_ALPHA_MIN, _ALPHA_MAX = 0.2, 2.5


def binary_sharpen(probability: float, alpha: float) -> float:
    """Calibration a un parametre d'une probabilite binaire.

    p' = p^a / (p^a + (1-p)^a) — symetrique (les deux cotes restent
    complementaires), fixe 0, 0.5 et 1.
    """
    p = min(1.0 - 1e-9, max(1e-9, probability))
    if alpha == 1.0:
        return p
    num = math.pow(p, alpha)
    den = num + math.pow(1.0 - p, alpha)
    return num / den


@lru_cache(maxsize=1)
def load_derived_calibration(path: str | None = None) -> dict[str, float]:
    """Alphas de calibration {'OU25': a, 'BTTS': a}, avec garde-fous."""
    target = Path(path) if path else DERIVED_CALIBRATION_PATH
    alphas = {"OU25": 1.0, "BTTS": 1.0}
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return alphas
    for market in alphas:
        raw = payload.get(market, {})
        candidate = raw.get("alpha") if isinstance(raw, dict) else None
        if isinstance(candidate, (int, float)) and _ALPHA_MIN <= candidate <= _ALPHA_MAX:
            alphas[market] = float(candidate)
    return alphas


@dataclass(frozen=True)
class TwoWayOffer:
    """Cotes d'un book sur un marche a deux issues (over/under ou oui/non)."""

    market_code: str
    bookmaker: str
    first_odd: float
    second_odd: float
    bookmaker_id: int | None = None
    source_count: int = 1


def derived_market_probabilities(
    distribution: Sequence[ExactScorePrediction],
) -> dict[str, float]:
    """Probabilites over/under 2.5 et BTTS depuis la distribution des scores.

    La distribution est deja normalisee et alignee sur le 1X2 final du
    moteur, donc les sommes partielles heritent de toute la chaine
    d'ajustements (calibration comprise).
    """
    over25 = 0.0
    btts_yes = 0.0
    total = 0.0
    for row in distribution:
        total += row.probability
        if row.home_goals + row.away_goals >= 3:
            over25 += row.probability
        if row.home_goals >= 1 and row.away_goals >= 1:
            btts_yes += row.probability
    if total <= 0:
        return {"OVER": 0.5, "UNDER": 0.5, "BTTS_YES": 0.5, "BTTS_NO": 0.5}
    over25 = min(1.0, max(0.0, over25 / total))
    btts_yes = min(1.0, max(0.0, btts_yes / total))
    return {
        "OVER": over25,
        "UNDER": 1.0 - over25,
        "BTTS_YES": btts_yes,
        "BTTS_NO": 1.0 - btts_yes,
    }


def calibrated_derived_probabilities(
    distribution: Sequence[ExactScorePrediction],
) -> dict[str, float]:
    """Probabilites derivees CALIBREES — la version que la prod consomme.

    Les sommes brutes de la matrice Poisson/Dixon-Coles sous-dispersent les
    totals (la variance reelle des buts depasse Poisson) : l'alpha par marche,
    ajuste sur l'historique walk-forward, corrige ce biais systematique.
    """
    raw = derived_market_probabilities(distribution)
    alphas = load_derived_calibration()
    over = binary_sharpen(raw["OVER"], alphas["OU25"])
    btts = binary_sharpen(raw["BTTS_YES"], alphas["BTTS"])
    return {
        "OVER": over,
        "UNDER": 1.0 - over,
        "BTTS_YES": btts,
        "BTTS_NO": 1.0 - btts,
    }


def handicap_win_probability(
    distribution: Sequence[ExactScorePrediction], line: float, is_home: bool
) -> float:
    """P(le cote `is_home` couvre le handicap `line`) depuis la distribution de
    marge (matrice Dixon-Coles). Le push (ligne entiere) compte pour moitie ->
    home_p + away_p = 1 (comparaison 2-way propre). `line` du point de vue du cote."""
    win = push = total = 0.0
    for row in distribution:
        total += row.probability
        margin = row.home_goals - row.away_goals
        base = margin if is_home else -margin
        adjusted = base + line
        if adjusted > 1e-9:
            win += row.probability
        elif abs(adjusted) <= 1e-9:
            push += row.probability
    if total <= 0:
        return 0.5
    return min(1.0, max(0.0, (win + 0.5 * push) / total))


def consensus_two_way(offers: Sequence[TwoWayOffer]) -> TwoWayOffer | None:
    """Cote mediane multi-books, meme esprit que le consensus 1X2."""
    if not offers:
        return None
    return TwoWayOffer(
        market_code=offers[0].market_code,
        bookmaker="CONSENSUS",
        first_odd=float(median(offer.first_odd for offer in offers)),
        second_odd=float(median(offer.second_odd for offer in offers)),
        source_count=len(offers),
    )


def _implied_two_way(first_odd: float, second_odd: float) -> tuple[float, float]:
    """Probabilites implicites normalisees (marge du book retiree)."""
    raw_first = 1.0 / first_odd if first_odd > 1.0 else 0.0
    raw_second = 1.0 / second_odd if second_odd > 1.0 else 0.0
    total = raw_first + raw_second
    if total <= 0:
        return 0.5, 0.5
    return raw_first / total, raw_second / total


def build_two_way_decisions(
    market_code: str,
    probabilities: dict[str, float],
    offer: TwoWayOffer,
    confidence_score: float,
    config: DecisionConfig,
    consensus: TwoWayOffer | None = None,
) -> tuple[SelectionDecision, ...]:
    """Decisions sur un marche a deux issues, avec les gardes du 1X2."""
    side_a, side_b = MARKET_SIDES[market_code]
    implied_a, implied_b = _implied_two_way(offer.first_odd, offer.second_odd)
    odds_map = {side_a: offer.first_odd, side_b: offer.second_odd}
    implied_map = {side_a: implied_a, side_b: implied_b}
    consensus_map = (
        {side_a: consensus.first_odd, side_b: consensus.second_odd}
        if consensus is not None
        else {}
    )

    decisions: list[SelectionDecision] = []
    for selection_code in (side_a, side_b):
        probability = probabilities[selection_code]
        market_odd = odds_map[selection_code]
        implied_probability = implied_map[selection_code]
        fair_odd = _fair_odd(probability)
        edge_probability = probability - implied_probability
        expected_value = _expected_value(probability, market_odd)
        price_outlier = _price_outlier_score(market_odd, consensus_map.get(selection_code))
        kelly_fraction = _fractional_kelly(probability, market_odd, config)
        credibility = _edge_credibility(edge_probability, confidence_score)
        longshot_failsafe = (
            implied_probability < LONGSHOT_IMPLIED_THRESHOLD
            and edge_probability > LONGSHOT_EDGE_CEILING
        )
        recommended = (
            confidence_score >= config.confidence_floor
            and edge_probability >= config.min_edge_probability
            and edge_probability < EDGE_HARD_CEILING
            and expected_value >= config.min_expected_value
            and market_odd >= fair_odd
            and credibility >= 0.20
            and not longshot_failsafe
        )
        rationale = (
            f"{selection_code}: proba modele {probability:.3f}, "
            f"marche {implied_probability:.3f}, edge {edge_probability:.3f}, "
            f"EV {expected_value:.3f}, prix relatif {price_outlier:+.3f}, "
            f"credibilite {credibility:.2f}"
        )
        decisions.append(
            SelectionDecision(
                selection_code=selection_code,
                model_probability=probability,
                fair_odd=fair_odd,
                market_odd=market_odd,
                implied_probability=implied_probability,
                edge_probability=edge_probability,
                expected_value=expected_value,
                fractional_kelly_fraction=kelly_fraction,
                confidence_score=confidence_score,
                ranking_score=_ranking_score(
                    probability,
                    edge_probability,
                    expected_value,
                    confidence_score,
                    price_outlier,
                    credibility,
                ),
                recommended=recommended,
                rationale=rationale,
                bookmaker=offer.bookmaker,
                bookmaker_id=offer.bookmaker_id,
                consensus_odd=consensus_map.get(selection_code),
                price_outlier_score=price_outlier,
                market_code=market_code,
            )
        )

    decisions.sort(
        key=lambda item: (
            item.recommended,
            item.ranking_score,
            item.expected_value or -999.0,
        ),
        reverse=True,
    )
    return tuple(decisions)


def build_selection_decision(
    market_code: str,
    selection_code: str,
    probability: float,
    market_odd: float,
    implied_probability: float,
    confidence_score: float,
    config: DecisionConfig,
    bookmaker: str,
    bookmaker_id: int | None = None,
    consensus_odd: float | None = None,
    line: float | None = None,
) -> SelectionDecision:
    """Une decision pour UNE issue (marches n-way : double chance, handicap).
    Memes gardes que build_two_way_decisions, mais la proba implicite devigee
    est fournie par l'appelant (le devig d'un marche a >2 issues lui est propre)."""
    fair_odd = _fair_odd(probability)
    edge_probability = probability - implied_probability
    expected_value = _expected_value(probability, market_odd)
    price_outlier = _price_outlier_score(market_odd, consensus_odd)
    kelly_fraction = _fractional_kelly(probability, market_odd, config)
    credibility = _edge_credibility(edge_probability, confidence_score)
    longshot_failsafe = (
        implied_probability < LONGSHOT_IMPLIED_THRESHOLD
        and edge_probability > LONGSHOT_EDGE_CEILING
    )
    recommended = (
        confidence_score >= config.confidence_floor
        and edge_probability >= config.min_edge_probability
        and edge_probability < EDGE_HARD_CEILING
        and expected_value >= config.min_expected_value
        and market_odd >= fair_odd
        and credibility >= 0.20
        and not longshot_failsafe
    )
    return SelectionDecision(
        selection_code=selection_code,
        model_probability=probability,
        fair_odd=fair_odd,
        market_odd=market_odd,
        implied_probability=implied_probability,
        edge_probability=edge_probability,
        expected_value=expected_value,
        fractional_kelly_fraction=kelly_fraction,
        confidence_score=confidence_score,
        ranking_score=_ranking_score(
            probability, edge_probability, expected_value,
            confidence_score, price_outlier, credibility,
        ),
        recommended=recommended,
        rationale=(
            f"{selection_code}: proba modele {probability:.3f}, "
            f"marche {implied_probability:.3f}, edge {edge_probability:.3f}, "
            f"EV {expected_value:.3f}, credibilite {credibility:.2f}"
        ),
        bookmaker=bookmaker,
        bookmaker_id=bookmaker_id,
        consensus_odd=consensus_odd,
        price_outlier_score=price_outlier,
        market_code=market_code,
        line=line,
    )
