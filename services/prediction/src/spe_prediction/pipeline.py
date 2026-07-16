from __future__ import annotations

from spe_prediction.engine import MatchAnalysisEngine
from spe_prediction.repository import PredictionRepository


class PredictionPipeline:
    def __init__(self, repository: PredictionRepository, engine: MatchAnalysisEngine | None = None) -> None:
        self._repository = repository
        self._engine = engine or MatchAnalysisEngine()

    def run(self) -> int:
        count = 0
        for context in self._repository.load_pending_fixtures():
            result = self._engine.analyze_fixture(context.fixture, context.market_odds)
            self._repository.save_analysis_result(result, context)
            count += 1
        return count
