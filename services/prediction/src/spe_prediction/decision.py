from __future__ import annotations

from dataclasses import replace

from spe_prediction.domain import (
    DecisionConfig,
    ImpliedProbabilities,
    MarketOdds,
    OutcomeProbabilities,
    SelectionDecision,
)

# Empirical guard: any single-bet 1X2 edge above this is almost always a model error
# rather than a true value bet. Edges in this range are kept but heavily down-weighted.
EDGE_CREDIBILITY_CEILING = 0.08
# Hard cap: edges above this we refuse to score at all.
EDGE_HARD_CEILING = 0.18
# Edges on long-odds (implied < 12%) selections are statistically the most
# error-prone — the favourite-longshot bias plus default-Poisson tails lift
# underdog probabilities above their true frequency.
LONGSHOT_IMPLIED_THRESHOLD = 0.12
LONGSHOT_EDGE_CEILING = 0.04


def _fair_odd(probability: float) -> float:
    return 1.0 / probability if probability > 0 else 999.0


def _expected_value(probability: float, odd: float) -> float:
    return (probability * odd) - 1.0


def _price_outlier_score(market_odd: float | None, consensus_odd: float | None) -> float:
    if market_odd is None or consensus_odd is None or consensus_odd <= 1.0:
        return 0.0
    return (market_odd / consensus_odd) - 1.0


def _fractional_kelly(probability: float, odd: float, decision_config: DecisionConfig) -> float:
    b = odd - 1.0
    q = 1.0 - probability
    if b <= 0:
        return 0.0
    raw_kelly = ((b * probability) - q) / b
    conservative = max(0.0, raw_kelly) * decision_config.fractional_kelly_multiplier
    return min(conservative, decision_config.max_kelly_fraction)


def _edge_credibility(edge_probability: float | None, confidence_score: float) -> float:
    if edge_probability is None or edge_probability <= 0.0:
        return 0.0
    if edge_probability >= EDGE_HARD_CEILING:
        return 0.0
    # Above EDGE_CREDIBILITY_CEILING (8%), confidence in the edge decays toward 0
    # by EDGE_HARD_CEILING (18%). Below, credibility scales with model confidence.
    if edge_probability <= EDGE_CREDIBILITY_CEILING:
        return min(1.0, edge_probability / EDGE_CREDIBILITY_CEILING) * max(0.30, confidence_score)
    decay = (EDGE_HARD_CEILING - edge_probability) / (EDGE_HARD_CEILING - EDGE_CREDIBILITY_CEILING)
    return decay * max(0.20, confidence_score) * 0.70


def _ranking_score(
    probability: float,
    edge_probability: float | None,
    expected_value: float | None,
    confidence_score: float,
    price_outlier: float,
    credibility: float,
) -> float:
    edge_component = min(1.0, max(0.0, edge_probability or 0.0) / 0.08) * 0.32
    ev_component = min(1.0, max(0.0, expected_value or 0.0) / 0.20) * 0.22
    price_component = min(1.0, max(0.0, price_outlier) / 0.12) * 0.18
    confidence_component = confidence_score * 0.16
    probability_component = min(1.0, probability / 0.65) * 0.04
    raw = edge_component + ev_component + price_component + confidence_component + probability_component
    # Multiply by credibility so faux-edges (huge gap vs market) never crown the ranking.
    return min(0.999, raw * (0.35 + 0.65 * credibility))


def build_selection_decisions(
    probabilities: OutcomeProbabilities,
    market_odds: MarketOdds | None,
    implied_probabilities: ImpliedProbabilities | None,
    confidence_score: float,
    config: DecisionConfig,
    consensus_odds: MarketOdds | None = None,
) -> tuple[SelectionDecision, ...]:
    probability_map = probabilities.as_dict()
    odds_map = (
        {
            "HOME": market_odds.home_odd,
            "DRAW": market_odds.draw_odd,
            "AWAY": market_odds.away_odd,
        }
        if market_odds
        else {}
    )
    implied_map = (
        {
            "HOME": implied_probabilities.home,
            "DRAW": implied_probabilities.draw,
            "AWAY": implied_probabilities.away,
        }
        if implied_probabilities
        else {}
    )
    consensus_map = (
        {
            "HOME": consensus_odds.home_odd,
            "DRAW": consensus_odds.draw_odd,
            "AWAY": consensus_odds.away_odd,
        }
        if consensus_odds
        else {}
    )

    decisions: list[SelectionDecision] = []
    for selection_code, probability in probability_map.items():
        fair_odd = _fair_odd(probability)
        market_odd = odds_map.get(selection_code)
        implied_probability = implied_map.get(selection_code)
        consensus_odd = consensus_map.get(selection_code)
        edge_probability = (
            probability - implied_probability if implied_probability is not None else None
        )
        expected_value = (
            _expected_value(probability, market_odd) if market_odd is not None else None
        )
        price_outlier = _price_outlier_score(market_odd, consensus_odd)
        kelly_fraction = (
            _fractional_kelly(probability, market_odd, config) if market_odd is not None else None
        )
        credibility = _edge_credibility(edge_probability, confidence_score)
        longshot_failsafe = (
            implied_probability is not None
            and implied_probability < LONGSHOT_IMPLIED_THRESHOLD
            and (edge_probability or 0.0) > LONGSHOT_EDGE_CEILING
        )
        recommended = (
            market_odd is not None
            and edge_probability is not None
            and expected_value is not None
            and confidence_score >= config.confidence_floor
            and edge_probability >= config.min_edge_probability
            and edge_probability < EDGE_HARD_CEILING
            and expected_value >= config.min_expected_value
            and market_odd >= fair_odd
            and credibility >= 0.20
            and not longshot_failsafe
        )

        rationale = (
            f"{selection_code}: proba modele {probability:.3f}"
            if market_odd is None
            else (
                f"{selection_code}: proba modele {probability:.3f}, "
                f"marche {implied_probability:.3f}, edge {edge_probability:.3f}, EV {expected_value:.3f}, "
                f"prix relatif {price_outlier:+.3f}, credibilite {credibility:.2f}"
            )
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
                bookmaker=market_odds.bookmaker if market_odds is not None else None,
                bookmaker_id=market_odds.bookmaker_id if market_odds is not None else None,
                consensus_odd=consensus_odd,
                price_outlier_score=price_outlier,
            )
        )

    # Garde de coherence : si HOME et AWAY sont recommandes SIMULTANEMENT,
    # l'edge des deux cotes provient d'un seul desaccord — le modele met
    # moins de NUL que le marche. Ce n'est pas deux convictions sur le match,
    # c'est un pari deguise contre le nul (la ou les modeles foot sont les
    # plus faibles). On ne retient que la meilleure conviction.
    home_idx = next(i for i, d in enumerate(decisions) if d.selection_code == "HOME")
    away_idx = next(i for i, d in enumerate(decisions) if d.selection_code == "AWAY")
    if decisions[home_idx].recommended and decisions[away_idx].recommended:
        weaker_idx = (
            home_idx
            if decisions[home_idx].ranking_score <= decisions[away_idx].ranking_score
            else away_idx
        )
        weaker = decisions[weaker_idx]
        decisions[weaker_idx] = replace(
            weaker,
            recommended=False,
            rationale=weaker.rationale
            + " | ecarte: edge des deux cotes = desaccord sur le nul, seule la meilleure conviction est retenue",
        )

    decisions.sort(
        key=lambda item: (
            item.recommended,
            item.ranking_score,
            item.expected_value or -999.0,
            item.edge_probability or -999.0,
            item.model_probability,
        ),
        reverse=True,
    )
    return tuple(decisions)
