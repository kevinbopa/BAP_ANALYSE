from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping


@dataclass(frozen=True)
class TeamStrengthSnapshot:
    team_id: int
    team_name: str
    elo_rating: float
    attack_rating: float
    defense_rating: float
    recent_form: float = 0.0
    fatigue_penalty: float = 0.0
    played_matches: int = 0
    data_quality: float = 0.0
    # Player layer (API-Football) — 1.0 = effectif complet.
    availability_index: float = 1.0
    key_absences: tuple[str, ...] = ()


@dataclass(frozen=True)
class FixtureFeatures:
    fixture_id: int
    home_team: TeamStrengthSnapshot
    away_team: TeamStrengthSnapshot
    home_advantage: float = 0.18
    draw_bias: float = 0.27
    market_temperature: float = 0.0
    external_adjustments: Mapping[str, float] = field(default_factory=dict)
    # Textes des correlations detectees pour ce match (moteur de correlations).
    insights: tuple[str, ...] = ()


@dataclass(frozen=True)
class MarketOdds:
    bookmaker: str
    home_odd: float
    draw_odd: float
    away_odd: float
    bookmaker_id: int | None = None
    source_count: int = 1
    home_spread: float = 0.0
    draw_spread: float = 0.0
    away_spread: float = 0.0


@dataclass(frozen=True)
class ImpliedProbabilities:
    home: float
    draw: float
    away: float
    margin: float


@dataclass(frozen=True)
class OutcomeProbabilities:
    home: float
    draw: float
    away: float

    def as_dict(self) -> dict[str, float]:
        return {"HOME": self.home, "DRAW": self.draw, "AWAY": self.away}


@dataclass(frozen=True)
class PoissonOutput:
    expected_home_goals: float
    expected_away_goals: float
    probabilities: OutcomeProbabilities


@dataclass(frozen=True)
class EloOutput:
    probabilities: OutcomeProbabilities
    rating_gap: float


@dataclass(frozen=True)
class PredictionVerdict:
    """Pure prediction output (no value-bet logic).

    Holds the model's predicted outcome and a "surety" score that reflects how
    confident the model is in WHICH outcome will happen — distinct from the
    epistemic ``confidence_score`` (which says how grounded the estimate is).

    A 80/15/5 split has high surety (clear favourite). A 35/35/30 split has
    low surety even if the model is epistemically confident it's a coin flip.
    """
    selection_code: str
    label: str
    probability: float
    runner_up_probability: float
    margin: float
    surety_score: float
    rationale: str
    # Le nul est "competitif" quand il talonne le pronostic (voir forecaster) :
    # l'argmax reste le pronostic optimal, mais le scenario nul merite d'etre
    # montre plutot que silencieusement ecarte.
    draw_competitive: bool = False


@dataclass(frozen=True)
class SelectionDecision:
    selection_code: str
    model_probability: float
    fair_odd: float
    market_odd: float | None
    implied_probability: float | None
    edge_probability: float | None
    expected_value: float | None
    fractional_kelly_fraction: float | None
    confidence_score: float
    ranking_score: float
    recommended: bool
    rationale: str
    bookmaker: str | None = None
    bookmaker_id: int | None = None
    consensus_odd: float | None = None
    price_outlier_score: float = 0.0
    # Marche porte par la selection : 1X2 (defaut), OU25, BTTS, HANDICAP...
    market_code: str = "1X2"
    # Ligne (handicap) portee par la selection ; None pour les marches sans ligne.
    line: float | None = None


@dataclass(frozen=True)
class AnalysisResult:
    fixture_id: int
    expected_home_goals: float
    expected_away_goals: float
    probabilities: OutcomeProbabilities
    poisson_probabilities: OutcomeProbabilities
    elo_probabilities: OutcomeProbabilities
    implied_probabilities: ImpliedProbabilities | None
    selections: tuple[SelectionDecision, ...]
    top_selection: SelectionDecision
    verdict: PredictionVerdict
    explanation: str = ""


@dataclass(frozen=True)
class ExactScorePrediction:
    home_goals: int
    away_goals: int
    probability: float
    fair_odd: float
    outcome_code: str

    @property
    def score_label(self) -> str:
        return f"{self.home_goals}-{self.away_goals}"


@dataclass(frozen=True)
class ExactScoreAnalysis:
    fixture_id: int
    match_analysis: AnalysisResult
    top_scores: tuple[ExactScorePrediction, ...]
    full_distribution: tuple[ExactScorePrediction, ...]


@dataclass(frozen=True)
class PoissonConfig:
    base_goal_rate: float = 1.35
    home_advantage_goals: float = 0.20
    max_goals: int = 8
    dixon_coles_rho: float = -0.08


@dataclass(frozen=True)
class EloConfig:
    logistic_scale: float = 400.0
    draw_probability_base: float = 0.27
    draw_probability_floor: float = 0.16
    draw_probability_ceiling: float = 0.33
    draw_decay: float = 300.0
    home_field_bonus: float = 55.0


@dataclass(frozen=True)
class DecisionConfig:
    poisson_weight: float = 0.65
    elo_weight: float = 0.35
    market_anchor_weight: float = 0.15
    min_edge_probability: float = 0.025
    min_expected_value: float = 0.015
    max_kelly_fraction: float = 0.05
    fractional_kelly_multiplier: float = 0.25
    confidence_floor: float = 0.45


@dataclass(frozen=True)
class EngineConfig:
    poisson: PoissonConfig = field(default_factory=PoissonConfig)
    elo: EloConfig = field(default_factory=EloConfig)
    decision: DecisionConfig = field(default_factory=DecisionConfig)
