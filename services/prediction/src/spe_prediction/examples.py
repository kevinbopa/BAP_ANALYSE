from __future__ import annotations

from spe_prediction.domain import FixtureFeatures, MarketOdds, TeamStrengthSnapshot
from spe_prediction.engine import MatchAnalysisEngine
from spe_prediction.exact_score import ExactScoreAgent


def build_example_fixture() -> FixtureFeatures:
    return FixtureFeatures(
        fixture_id=1001,
        home_team=TeamStrengthSnapshot(
            team_id=1,
            team_name="Arsenal",
            elo_rating=1710.0,
            attack_rating=0.42,
            defense_rating=0.28,
            recent_form=0.35,
        ),
        away_team=TeamStrengthSnapshot(
            team_id=2,
            team_name="Chelsea",
            elo_rating=1665.0,
            attack_rating=0.21,
            defense_rating=0.31,
            recent_form=0.05,
        ),
        home_advantage=0.16,
        draw_bias=0.26,
    )


def build_example_market() -> MarketOdds:
    return MarketOdds(
        bookmaker="Stake",
        home_odd=2.05,
        draw_odd=3.45,
        away_odd=3.90,
        bookmaker_id=1,
    )


def build_example_result():
    engine = MatchAnalysisEngine()
    return engine.analyze_fixture(build_example_fixture(), build_example_market())


def build_example_exact_score_result():
    agent = ExactScoreAgent()
    return agent.analyze_fixture(build_example_fixture(), build_example_market())
