from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import math
from statistics import median, pstdev


@dataclass(frozen=True)
class GolfMarketOdd:
    tournament_id: int
    player_id: int | None
    bookmaker_id: int
    market_code: str
    selection_name: str
    decimal_odd: float


@dataclass(frozen=True)
class GolfPrediction:
    tournament_id: int
    player_id: int | None
    market_code: str
    selection_name: str
    probability: float
    fair_odd: float
    source_count: int
    bookmaker_count: int
    market_depth: float
    dispersion: float
    confidence: float


def devig_bookmaker_market(odds: list[GolfMarketOdd]) -> dict[tuple[int | None, str], float]:
    """Remove bookmaker margin inside one tournament/market/bookmaker slice."""
    raw = []
    for odd in odds:
        if odd.decimal_odd > 1.0:
            raw.append((odd, 1.0 / odd.decimal_odd))
    total = sum(prob for _odd, prob in raw)
    if total <= 0:
        return {}
    return {
        (odd.player_id, odd.selection_name): max(0.0, min(1.0, probability / total))
        for odd, probability in raw
    }


def consensus_predictions(
    odds: list[GolfMarketOdd],
    *,
    shrink_weight: float = 0.08,
) -> list[GolfPrediction]:
    """Consensus model V1.1 for golf outrights/top markets.

    Until we ingest a trusted player-stat feed, the safest predictive signal is
    the de-vigged market consensus across bookmakers. Golf outrights are noisy,
    so we shrink probabilities slightly toward the field baseline and expose a
    confidence score based on bookmaker depth and disagreement.
    """
    by_slice: dict[tuple[int, str, int], list[GolfMarketOdd]] = defaultdict(list)
    for odd in odds:
        by_slice[(odd.tournament_id, odd.market_code, odd.bookmaker_id)].append(odd)

    probabilities: dict[tuple[int, str, int | None, str], list[float]] = defaultdict(list)
    bookmakers: dict[tuple[int, str, int | None, str], set[int]] = defaultdict(set)
    for (tournament_id, market_code, bookmaker_id), slice_odds in by_slice.items():
        for (player_id, selection_name), probability in devig_bookmaker_market(slice_odds).items():
            key = (tournament_id, market_code, player_id, selection_name)
            probabilities[key].append(probability)
            bookmakers[key].add(bookmaker_id)

    predictions: list[GolfPrediction] = []
    for (tournament_id, market_code, player_id, selection_name), values in probabilities.items():
        if not values:
            continue
        field_size = sum(1 for key in probabilities if key[0] == tournament_id and key[1] == market_code)
        baseline = 1.0 / max(1, field_size)
        raw_probability = float(median(values))
        probability = (1.0 - shrink_weight) * raw_probability + shrink_weight * baseline
        if probability <= 0:
            continue
        bookmaker_count = len(bookmakers[(tournament_id, market_code, player_id, selection_name)])
        market_depth = min(1.0, math.log1p(bookmaker_count) / math.log1p(8))
        dispersion = float(pstdev(values)) if len(values) > 1 else 0.0
        disagreement_penalty = min(0.60, dispersion / 0.08)
        confidence = max(0.05, min(1.0, 0.20 + 0.80 * market_depth - disagreement_penalty))
        predictions.append(
            GolfPrediction(
                tournament_id=tournament_id,
                player_id=player_id,
                market_code=market_code,
                selection_name=selection_name,
                probability=probability,
                fair_odd=1.0 / probability,
                source_count=len(values),
                bookmaker_count=bookmaker_count,
                market_depth=market_depth,
                dispersion=dispersion,
                confidence=confidence,
            )
        )
    return sorted(
        predictions,
        key=lambda p: (p.tournament_id, p.market_code, -p.probability, p.selection_name),
    )


def golf_deal_candidates(
    predictions: list[GolfPrediction],
    latest_odds: list[GolfMarketOdd],
    *,
    min_edge: float = 0.035,
    min_probability: float = 0.01,
    min_confidence: float = 0.32,
    risk_adjustments: dict[int, float] | None = None,
    max_candidates_per_market: int = 5,
) -> list[dict]:
    """Return positive expected-value candidates for golf markets."""
    by_key = {
        (p.tournament_id, p.market_code, p.player_id, p.selection_name): p
        for p in predictions
    }
    best_odds: dict[tuple[int, str, int | None, str], GolfMarketOdd] = {}
    for odd in latest_odds:
        key = (odd.tournament_id, odd.market_code, odd.player_id, odd.selection_name)
        current = best_odds.get(key)
        if current is None or odd.decimal_odd > current.decimal_odd:
            best_odds[key] = odd

    candidates = []
    for key, prediction in by_key.items():
        odd = best_odds.get(key)
        if odd is None or prediction.probability < min_probability:
            continue
        if prediction.confidence < min_confidence:
            continue
        implied = 1.0 / odd.decimal_odd
        edge = prediction.probability - implied
        expected_value = prediction.probability * odd.decimal_odd - 1.0
        risk_bump = (risk_adjustments or {}).get(prediction.tournament_id, 0.0)
        required_edge = min_edge + risk_bump
        if edge < required_edge or expected_value <= 0:
            continue
        candidates.append(
            {
                "tournament_id": prediction.tournament_id,
                "player_id": prediction.player_id,
                "bookmaker_id": odd.bookmaker_id,
                "market_code": prediction.market_code,
                "selection_name": prediction.selection_name,
                "model_probability": round(prediction.probability, 6),
                "implied_probability": round(implied, 6),
                "edge_probability": round(edge, 6),
                "market_odd": round(odd.decimal_odd, 4),
                "expected_value": round(expected_value, 6),
                "confidence": round(prediction.confidence, 6),
                "required_edge": round(required_edge, 6),
            }
        )

    grouped: dict[tuple[int, str], list[dict]] = defaultdict(list)
    for candidate in sorted(candidates, key=lambda c: c["edge_probability"], reverse=True):
        group_key = (int(candidate["tournament_id"]), str(candidate["market_code"]))
        if len(grouped[group_key]) < max_candidates_per_market:
            grouped[group_key].append(candidate)
    return [candidate for group in grouped.values() for candidate in group]
