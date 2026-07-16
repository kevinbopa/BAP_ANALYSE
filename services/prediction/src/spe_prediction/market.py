from __future__ import annotations

from spe_prediction.domain import ImpliedProbabilities, MarketOdds


def remove_overround(odds: MarketOdds) -> ImpliedProbabilities:
    home_raw = 1.0 / odds.home_odd
    draw_raw = 1.0 / odds.draw_odd
    away_raw = 1.0 / odds.away_odd

    margin = (home_raw + draw_raw + away_raw) - 1.0
    normalizer = home_raw + draw_raw + away_raw

    return ImpliedProbabilities(
        home=home_raw / normalizer,
        draw=draw_raw / normalizer,
        away=away_raw / normalizer,
        margin=margin,
    )
