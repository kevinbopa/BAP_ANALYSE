"""Optional XGBoost gradient-boosted predictor for 1X2 outcomes.

This is an *optional* third model alongside Poisson + Elo. The xgboost
library is loaded lazily — when unavailable, or when no trained model is
on disk, ``XGBoostPredictor.available`` stays False and the engine
seamlessly falls back to the analytical Poisson/Elo/market blend.

Training pipeline lives in ``services/prediction/train_xgboost.py``.

Class order convention used by the trained model:
    0 = HOME, 1 = DRAW, 2 = AWAY
"""
from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path
from typing import Any

from spe_prediction.domain import FixtureFeatures, MarketOdds, OutcomeProbabilities

_logger = logging.getLogger(__name__)


try:  # pragma: no cover - import-time branch
    import xgboost  # noqa: F401
    _XGBOOST_AVAILABLE = True
except ImportError:  # pragma: no cover
    _XGBOOST_AVAILABLE = False


DEFAULT_FEATURE_NAMES: tuple[str, ...] = (
    "elo_home",
    "elo_away",
    "elo_diff",
    "attack_home",
    "attack_away",
    "defense_home",
    "defense_away",
    "form_home",
    "form_away",
    "fatigue_home",
    "fatigue_away",
    "home_advantage",
    "data_quality_home",
    "data_quality_away",
    "implied_home",
    "implied_draw",
    "implied_away",
    "market_spread",
    "market_source_count",
)


@dataclass(frozen=True)
class XGBoostConfig:
    """Where the trained joblib model lives on disk and which features it expects."""
    model_path: str | None = None
    feature_names: tuple[str, ...] = DEFAULT_FEATURE_NAMES


def extract_features(
    fixture: FixtureFeatures,
    market_odds: MarketOdds | None,
) -> list[float]:
    """Extract the numeric feature vector for a single fixture.

    The feature order **must** match what the trained model expects;
    see ``DEFAULT_FEATURE_NAMES`` and the training script.
    """
    home = fixture.home_team
    away = fixture.away_team

    implied_home = 0.34
    implied_draw = 0.33
    implied_away = 0.33
    market_spread = 0.10
    market_source_count = 0.0

    if market_odds is not None:
        from spe_prediction.market import remove_overround
        implied = remove_overround(market_odds)
        implied_home = implied.home
        implied_draw = implied.draw
        implied_away = implied.away
        market_spread = (
            market_odds.home_spread + market_odds.draw_spread + market_odds.away_spread
        ) / 3.0
        market_source_count = float(market_odds.source_count)

    return [
        float(home.elo_rating),
        float(away.elo_rating),
        float(home.elo_rating - away.elo_rating),
        float(home.attack_rating),
        float(away.attack_rating),
        float(home.defense_rating),
        float(away.defense_rating),
        float(home.recent_form),
        float(away.recent_form),
        float(home.fatigue_penalty),
        float(away.fatigue_penalty),
        float(fixture.home_advantage),
        float(home.data_quality),
        float(away.data_quality),
        float(implied_home),
        float(implied_draw),
        float(implied_away),
        float(market_spread),
        market_source_count,
    ]


class XGBoostPredictor:
    """Lazy XGBoost wrapper — initialises empty when xgboost/joblib aren't
    available or when the model file is missing.
    """

    def __init__(self, config: XGBoostConfig | None = None) -> None:
        self._config = config or XGBoostConfig()
        self._model: Any = None
        if not _XGBOOST_AVAILABLE:
            return
        if not self._config.model_path:
            return
        path = Path(self._config.model_path)
        if not path.exists():
            _logger.info("XGBoost model file not found at %s — falling back to 3-way blend.", path)
            return
        try:  # pragma: no cover - file IO branch
            import joblib  # type: ignore
            self._model = joblib.load(str(path))
        except Exception as exc:  # noqa: BLE001
            _logger.warning("Failed to load XGBoost model from %s: %s", path, exc)
            self._model = None

    @property
    def available(self) -> bool:
        return self._model is not None

    def predict(
        self,
        fixture: FixtureFeatures,
        market_odds: MarketOdds | None,
    ) -> OutcomeProbabilities | None:
        if self._model is None:
            return None
        features = extract_features(fixture, market_odds)
        try:  # pragma: no cover - runtime branch
            import numpy as np  # type: ignore
            matrix = np.array([features], dtype=float)
            proba = self._model.predict_proba(matrix)[0]
        except Exception as exc:  # noqa: BLE001
            _logger.warning("XGBoost inference failed: %s", exc)
            return None
        if len(proba) < 3:
            return None
        return OutcomeProbabilities(
            home=float(proba[0]),
            draw=float(proba[1]),
            away=float(proba[2]),
        )


class StaticPredictor:
    """Test helper — exposes the same surface as XGBoostPredictor.

    Lets unit tests inject a deterministic prediction without depending on
    the actual xgboost runtime or a trained model file.
    """

    def __init__(self, probabilities: OutcomeProbabilities) -> None:
        self._probabilities = probabilities

    @property
    def available(self) -> bool:
        return True

    def predict(
        self,
        fixture: FixtureFeatures,
        market_odds: MarketOdds | None,
    ) -> OutcomeProbabilities | None:
        return self._probabilities
