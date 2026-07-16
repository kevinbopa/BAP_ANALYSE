from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
from statistics import median
from typing import Protocol, Sequence

from spe_prediction.domain import (
    AnalysisResult,
    DecisionConfig,
    FixtureFeatures,
    MarketOdds,
    SelectionDecision,
    TeamStrengthSnapshot,
)
from spe_prediction.context import load_context_adjustments, merge_adjustments
from spe_prediction.correlations import competition_family, fixture_insight_signals
from spe_prediction.decision import build_selection_decisions
from spe_prediction.derived_markets import (
    OU25_GOAL_LINE,
    TwoWayOffer,
    build_selection_decision,
    build_two_way_decisions,
    calibrated_derived_probabilities,
    consensus_two_way,
    handicap_win_probability,
)
from spe_prediction.market_models import predict_derived_probabilities, predict_market
from spe_prediction.exact_score import build_exact_score_analysis_from_result
from spe_prediction.market import remove_overround
from spe_prediction.player_signals import TeamAvailability, load_team_availability
from spe_prediction.rating import compute_elo_ratings, competition_weight as _rating_competition_weight
from spe_prediction.serialization import prediction_record, ranking_record, value_bet_record
from spe_prediction.sql_templates import (
    DEAL_RANKING_INSERT_SQL,
    PREDICTION_INSERT_SQL,
    VALUE_BET_INSERT_SQL,
)


@dataclass(frozen=True)
class FixtureContext:
    fixture: FixtureFeatures
    market_odds: MarketOdds | None = None
    bookmaker_offers: tuple[MarketOdds, ...] = ()
    # Cotes des marches derives (ligne 2.5 pour les totals).
    totals_offers: tuple[TwoWayOffer, ...] = ()
    btts_offers: tuple[TwoWayOffer, ...] = ()


class PredictionRepository(Protocol):
    def load_pending_fixtures(self) -> Sequence[FixtureContext]:
        ...

    def save_analysis_result(self, result: AnalysisResult, context: FixtureContext | None = None) -> None:
        ...


class InMemoryPredictionRepository:
    def __init__(self, fixtures: Sequence[FixtureContext]) -> None:
        self._fixtures = list(fixtures)
        self.saved_results: list[AnalysisResult] = []

    def load_pending_fixtures(self) -> Sequence[FixtureContext]:
        return tuple(self._fixtures)

    def save_analysis_result(self, result: AnalysisResult, context: FixtureContext | None = None) -> None:
        self.saved_results.append(result)


def _target_bookmaker_from_env() -> str:
    import os
    return os.getenv("SPE_TARGET_BOOKMAKER", "bet365").strip().lower()


class PostgresPredictionRepository:
    def __init__(
        self,
        connection,
        scope_name: str = "DEFAULT_V1_SCOPE",
        target_bookmaker: str | None = None,
        league_name: str | None = None,
    ) -> None:
        self._connection = connection
        self._scope_name = scope_name
        self._decision_config = DecisionConfig()
        # Cible du run : une competition precise (pipeline leger, a la
        # demande depuis le dashboard) ou None = balayage global.
        self._league_name = (league_name or "").strip() or None
        self._target_bookmaker = (
            target_bookmaker.strip().lower()
            if target_bookmaker is not None
            else _target_bookmaker_from_env()
        )
        self._analysis_scope_id = self._ensure_scope()
        self._model_run_id = self._create_model_run()

    def load_pending_fixtures(self) -> Sequence[FixtureContext]:
        """Fixtures a analyser.

        Mode competition (league_name fourni) : seulement cette competition —
        le pipeline ne charge pas toute l'Europe pour predire une ligue.
        Mode global : toutes competitions confondues sur 7 jours (la fenetre
        "meilleurs deals de la semaine"), plafond eleve pour ne noyer
        personne derriere les ligues aux calendriers charges.
        """
        fixtures: list[FixtureContext] = []
        with self._connection.cursor() as cursor:
            self._elo_ratings = self._compute_opponent_adjusted_elo(cursor)
            cursor.execute(
                """
                SELECT
                    f.fixture_id,
                    home.team_id,
                    home.team_name,
                    away.team_id,
                    away.team_name,
                    f.kickoff_utc,
                    l.league_name
                FROM core.fixtures f
                JOIN core.leagues l ON l.league_id = f.league_id
                JOIN core.teams home ON home.team_id = f.home_team_id
                JOIN core.teams away ON away.team_id = f.away_team_id
                WHERE (f.kickoff_utc IS NULL
                       OR (f.kickoff_utc >= (now() - interval '24 hours')
                           AND f.kickoff_utc <= now() + (%(horizon_days)s * interval '1 day')))
                  AND (%(league_name)s::text IS NULL OR l.league_name = %(league_name)s)
                ORDER BY f.kickoff_utc NULLS LAST, f.fixture_id
                LIMIT %(fixture_cap)s
                """,
                {
                    "league_name": self._league_name,
                    "horizon_days": 14 if self._league_name else 7,
                    "fixture_cap": 120 if self._league_name else 250,
                },
            )
            rows = cursor.fetchall()

            for fixture_id, home_team_id, home_team_name, away_team_id, away_team_name, kickoff, league_name in rows:
                bookmaker_offers = self._load_market_offers(cursor, int(fixture_id))
                market_odds = self._build_market_consensus(bookmaker_offers)
                as_of = kickoff or self._evaluation_now()
                home_snapshot = self._build_team_snapshot(cursor, int(home_team_id), str(home_team_name), as_of)
                away_snapshot = self._build_team_snapshot(cursor, int(away_team_id), str(away_team_name), as_of)
                home_advantage = self._home_advantage_for_league(str(league_name))
                draw_bias = 0.27
                external_adjustments: dict[str, float] = {}
                if market_odds is not None:
                    implied = remove_overround(market_odds)
                    draw_bias = max(0.18, min(0.34, (0.27 * 0.55) + (implied.draw * 0.45)))
                    goal_gap_delta = max(-0.55, min(0.55, (implied.home - implied.away) * 1.10))
                    total_goal_delta = max(-0.16, min(0.16, (0.27 - implied.draw) * 0.70))
                    external_adjustments = {
                        "home_goal_delta": total_goal_delta + goal_gap_delta,
                        "away_goal_delta": total_goal_delta - goal_gap_delta,
                    }
                context_adjustments = self._context_for_fixture(cursor, int(fixture_id))
                if context_adjustments:
                    external_adjustments = merge_adjustments(external_adjustments, context_adjustments)
                insight_texts, insight_adjustments = self._insights_for_fixture(
                    cursor, int(home_team_id), int(away_team_id), as_of,
                    league_name=str(league_name),
                    home_elo=home_snapshot.elo_rating,
                    away_elo=away_snapshot.elo_rating,
                )
                if insight_adjustments:
                    external_adjustments = merge_adjustments(external_adjustments, insight_adjustments)
                fixtures.append(
                    FixtureContext(
                        fixture=FixtureFeatures(
                            fixture_id=int(fixture_id),
                            home_team=home_snapshot,
                            away_team=away_snapshot,
                            home_advantage=home_advantage,
                            draw_bias=draw_bias,
                            external_adjustments=external_adjustments,
                            insights=insight_texts,
                        ),
                        market_odds=market_odds,
                        bookmaker_offers=bookmaker_offers,
                        totals_offers=self._load_totals_offers(cursor, int(fixture_id)),
                        btts_offers=self._load_btts_offers(cursor, int(fixture_id)),
                    )
                )
        return tuple(fixtures)

    def save_analysis_result(self, result: AnalysisResult, context: FixtureContext | None = None) -> None:
        exact_score_analysis = (
            build_exact_score_analysis_from_result(context.fixture, result)
            if context is not None
            else None
        )
        with self._connection.cursor() as cursor:
            prediction = prediction_record(
                result,
                model_run_id=str(self._model_run_id),
                exact_score_analysis=exact_score_analysis,
            )
            cursor.execute(PREDICTION_INSERT_SQL, prediction)
            prediction_id = int(cursor.fetchone()[0])

            consensus_odds = context.market_odds if context is not None else None
            bookmaker_offers = context.bookmaker_offers if context is not None else ()
            data_quality_score = self._fixture_data_quality(context)
            freshness_score = self._fixture_freshness(consensus_odds)
            deal_candidates = self._build_deal_candidates(result, bookmaker_offers, consensus_odds)
            derived_candidates = self._build_derived_deal_candidates(
                context,
                exact_score_analysis,
                confidence_score=result.top_selection.confidence_score,
                cursor=cursor,
            )
            extended_candidates = self._build_extended_deals(
                cursor, context, result, exact_score_analysis,
                confidence_score=result.top_selection.confidence_score,
            )
            # UNE conviction par match, TOUS marches confondus (1X2, O/U,
            # BTTS) : plusieurs paris sur le meme match ne sont jamais
            # independants — seul le mieux score merite le tableau. Les
            # lignes restantes sont LE MEME pari chez 2 books max.
            all_candidates = self._select_best_conviction(
                tuple(deal_candidates) + derived_candidates + extended_candidates
            )

            # Supersede-on-write : ce run devient LA recommandation finale du
            # fixture. Les value bets non regles des runs precedents sont
            # annules (CANCELLED/VOID) pour que le track record ne compte
            # qu'une recommandation par match — la derniere avant kickoff.
            cursor.execute(
                """
                UPDATE model.value_bets
                SET status_code = 'CANCELLED',
                    result_code = 'VOID',
                    profit_units = 0,
                    settled_at = now()
                WHERE fixture_id = %s
                  AND result_code IS NULL
                """,
                (result.fixture_id,),
            )

            for rank_position, selection in enumerate(all_candidates, start=1):
                record = value_bet_record(
                    result,
                    selection,
                    prediction_id=prediction_id,
                    bookmaker_id=selection.bookmaker_id,
                )
                cursor.execute(VALUE_BET_INSERT_SQL, record)
                value_bet_id = int(cursor.fetchone()[0])

                ranking = ranking_record(
                    result,
                    selection,
                    prediction_id=prediction_id,
                    rank_position=rank_position,
                    analysis_scope_id=str(self._analysis_scope_id),
                    value_bet_id=value_bet_id,
                    data_quality_score=data_quality_score,
                    freshness_score=freshness_score,
                )
                cursor.execute(DEAL_RANKING_INSERT_SQL, ranking)

        self._connection.commit()

    def finalize_run(self) -> None:
        with self._connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE model.model_runs
                SET status_code = 'SUCCESS',
                    finished_at = now()
                WHERE model_run_id = %s
                """,
                (str(self._model_run_id),),
            )
        self._connection.commit()

    def _ensure_scope(self) -> str:
        with self._connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO core.analysis_scopes (
                    scope_name,
                    sport_code,
                    criteria_json,
                    is_active
                )
                VALUES (%s, 'SOCCER', %s::jsonb, true)
                ON CONFLICT (scope_name) DO UPDATE
                SET is_active = true
                RETURNING analysis_scope_id
                """,
                (
                    self._scope_name,
                    json.dumps(
                        {
                            "markets": ["1X2", "OU25", "BTTS"],
                            "leagues": ["La Liga", "Ligue 1", "Bundesliga", "Serie A"],
                        }
                    ),
                ),
            )
            analysis_scope_id = str(cursor.fetchone()[0])
        self._connection.commit()
        return analysis_scope_id

    def _create_model_run(self) -> str:
        with self._connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO model.model_runs (
                    analysis_scope_id,
                    model_name,
                    model_version,
                    run_type,
                    status_code,
                    started_at,
                    parameters_json
                )
                VALUES (%s, 'poisson_market_hybrid_v1', '1.2.0', 'INFERENCE', 'RUNNING', now(), %s::jsonb)
                RETURNING model_run_id
                """,
                (
                    self._analysis_scope_id,
                    json.dumps({"scope": self._scope_name}),
                ),
            )
            model_run_id = str(cursor.fetchone()[0])
        self._connection.commit()
        return model_run_id

    def _compute_opponent_adjusted_elo(self, cursor) -> dict[int, float]:
        """Replay the whole completed-match history chronologically to get
        opponent-adjusted Elo ratings (see spe_prediction.rating)."""
        cursor.execute(
            """
            SELECT f.kickoff_utc, f.home_team_id, f.away_team_id,
                   fs.home_score, fs.away_score, l.league_name
            FROM core.fixtures f
            JOIN core.fixture_scores fs ON fs.fixture_id = f.fixture_id
            JOIN core.leagues l ON l.league_id = f.league_id
            WHERE fs.home_score IS NOT NULL
              AND fs.away_score IS NOT NULL
              AND f.kickoff_utc IS NOT NULL
            ORDER BY f.kickoff_utc ASC, f.fixture_id ASC
            """
        )
        return compute_elo_ratings(cursor.fetchall())

    @staticmethod
    def compute_fatigue_penalty(
        last_match_utc: datetime | None,
        matches_last_21_days: int,
        as_of: datetime,
    ) -> float:
        """Fatigue 0..1 from calendar congestion.

        Two signals: short rest (fewer than ~6 days since the previous match)
        and fixture congestion (number of matches in the last 21 days; 6+ in
        three weeks is an extreme schedule). Elo consumes this as a rating
        malus, Poisson as an xG reduction.
        """
        if last_match_utc is None:
            return 0.0
        ts = last_match_utc if last_match_utc.tzinfo else last_match_utc.replace(tzinfo=timezone.utc)
        rest_days = max(0.0, (as_of - ts).total_seconds() / 86400.0)
        short_rest = max(0.0, min(1.0, (6.0 - rest_days) / 6.0))
        congestion = max(0.0, min(1.0, matches_last_21_days / 6.0))
        return round(min(1.0, 0.65 * short_rest + 0.35 * congestion), 3)

    def _fatigue_for_team(self, cursor, team_id: int, as_of: datetime) -> float:
        cursor.execute(
            """
            SELECT
                MAX(f.kickoff_utc) AS last_match,
                COUNT(*) FILTER (
                    WHERE f.kickoff_utc >= %(as_of)s - interval '21 days'
                ) AS matches_21d
            FROM core.fixtures f
            JOIN core.fixture_scores fs ON fs.fixture_id = f.fixture_id
            WHERE (f.home_team_id = %(team_id)s OR f.away_team_id = %(team_id)s)
              AND fs.home_score IS NOT NULL
              AND f.kickoff_utc IS NOT NULL
              AND f.kickoff_utc < %(as_of)s
            """,
            {"team_id": team_id, "as_of": as_of},
        )
        row = cursor.fetchone()
        last_match, matches_21d = (row if row else (None, 0))
        return self.compute_fatigue_penalty(last_match, int(matches_21d or 0), as_of)

    def _build_team_snapshot(
        self, cursor, team_id: int, team_name: str, as_of: datetime | None = None
    ) -> TeamStrengthSnapshot:
        cursor.execute(
            """
            WITH team_matches AS (
                SELECT
                    f.fixture_id,
                    f.kickoff_utc,
                    l.league_name,
                    CASE WHEN f.home_team_id = %(team_id)s THEN true ELSE false END AS is_home,
                    CASE
                        WHEN f.home_team_id = %(team_id)s THEN fs.home_score
                        ELSE fs.away_score
                    END AS goals_for,
                    CASE
                        WHEN f.home_team_id = %(team_id)s THEN fs.away_score
                        ELSE fs.home_score
                    END AS goals_against,
                    CASE
                        WHEN fs.home_score IS NULL OR fs.away_score IS NULL THEN null
                        WHEN (
                            CASE
                                WHEN f.home_team_id = %(team_id)s THEN fs.home_score
                                ELSE fs.away_score
                            END
                        ) > (
                            CASE
                                WHEN f.home_team_id = %(team_id)s THEN fs.away_score
                                ELSE fs.home_score
                            END
                        ) THEN 1.0
                        WHEN (
                            CASE
                                WHEN f.home_team_id = %(team_id)s THEN fs.home_score
                                ELSE fs.away_score
                            END
                        ) = (
                            CASE
                                WHEN f.home_team_id = %(team_id)s THEN fs.away_score
                                ELSE fs.home_score
                            END
                        ) THEN 0.5
                        ELSE 0.0
                    END AS result_points
                FROM core.fixtures f
                JOIN core.leagues l ON l.league_id = f.league_id
                JOIN core.fixture_scores fs ON fs.fixture_id = f.fixture_id
                WHERE (f.home_team_id = %(team_id)s OR f.away_team_id = %(team_id)s)
                  AND fs.home_score IS NOT NULL
                  AND fs.away_score IS NOT NULL
                ORDER BY f.kickoff_utc DESC NULLS LAST, f.fixture_id DESC
                LIMIT 80
            )
            SELECT
                kickoff_utc,
                league_name,
                goals_for,
                goals_against,
                result_points
            FROM team_matches
            """,
            {"team_id": team_id},
        )
        rows = cursor.fetchall()

        played_matches = len(rows)
        layer_metrics = self._compute_layered_team_metrics(rows)
        avg_goals_for = layer_metrics["goals_for"]
        avg_goals_against = layer_metrics["goals_against"]
        avg_result_points = layer_metrics["result_points"]
        recent_form = layer_metrics["recent_form"]
        data_quality = layer_metrics["data_quality"]

        # Opponent-adjusted Elo (replayed full history) is the primary rating.
        # The synthetic points-based Elo remains as fallback when the team has
        # no completed match in the replay (e.g. brand-new team in DB).
        adjusted_elo = getattr(self, "_elo_ratings", {}).get(team_id)
        if adjusted_elo is not None and played_matches >= 3:
            elo_rating = adjusted_elo
        else:
            elo_rating = (
                1500.0
                + (avg_result_points - 0.5) * 400.0
                + (avg_goals_for - avg_goals_against) * 35.0
                + recent_form * 28.0
            )
        attack_rating = max(0.05, avg_goals_for - 1.0)
        defense_rating = max(0.05, avg_goals_against)

        if played_matches < 3:
            elo_rating = 1500.0
            recent_form = 0.0

        effective_as_of = as_of or self._evaluation_now()
        fatigue_penalty = self._fatigue_for_team(cursor, team_id, effective_as_of)
        availability = self._availability_for_team(cursor, team_id)

        return TeamStrengthSnapshot(
            team_id=team_id,
            team_name=team_name,
            elo_rating=elo_rating,
            attack_rating=attack_rating,
            defense_rating=defense_rating,
            recent_form=recent_form,
            fatigue_penalty=fatigue_penalty,
            played_matches=played_matches,
            data_quality=data_quality,
            availability_index=availability.availability_index,
            key_absences=availability.key_absences,
        )

    def _availability_for_team(self, cursor, team_id: int) -> TeamAvailability:
        try:
            return load_team_availability(cursor, team_id)
        except Exception:
            # Player-layer tables absent (migration 0008 not applied) or any
            # query issue: neutral availability, engine unaffected.
            self._connection.rollback()
            return TeamAvailability(1.0, (), 0, 0)

    def _context_for_fixture(self, cursor, fixture_id: int) -> dict[str, float]:
        try:
            return load_context_adjustments(cursor, fixture_id)
        except Exception:
            self._connection.rollback()
            return {}

    def _rest_days_for_team(self, cursor, team_id: int, as_of: datetime) -> float | None:
        cursor.execute(
            """
            SELECT MAX(f.kickoff_utc)
            FROM core.fixtures f
            JOIN core.fixture_scores fs ON fs.fixture_id = f.fixture_id
            WHERE (f.home_team_id = %(team_id)s OR f.away_team_id = %(team_id)s)
              AND fs.home_score IS NOT NULL
              AND f.kickoff_utc IS NOT NULL
              AND f.kickoff_utc < %(as_of)s
            """,
            {"team_id": team_id, "as_of": as_of},
        )
        row = cursor.fetchone()
        if not row or row[0] is None:
            return None
        last = row[0] if row[0].tzinfo else row[0].replace(tzinfo=timezone.utc)
        return max(0.0, (as_of - last).total_seconds() / 86400.0)

    def _last_match_was_big_loss(self, cursor, team_id: int, as_of: datetime) -> bool:
        cursor.execute(
            """
            SELECT CASE WHEN f.home_team_id = %(tid)s
                        THEN fs.home_score - fs.away_score
                        ELSE fs.away_score - fs.home_score END AS margin
            FROM core.fixtures f
            JOIN core.fixture_scores fs ON fs.fixture_id = f.fixture_id
            WHERE (f.home_team_id = %(tid)s OR f.away_team_id = %(tid)s)
              AND fs.home_score IS NOT NULL
              AND f.kickoff_utc IS NOT NULL
              AND f.kickoff_utc < %(as_of)s
            ORDER BY f.kickoff_utc DESC
            LIMIT 1
            """,
            {"tid": team_id, "as_of": as_of},
        )
        row = cursor.fetchone()
        return bool(row and row[0] is not None and int(row[0]) <= -2)

    STRONG_OPPONENT_ELO = 1700.0

    def _insights_for_fixture(
        self,
        cursor,
        home_team_id: int,
        away_team_id: int,
        as_of: datetime,
        league_name: str = "",
        home_elo: float = 1500.0,
        away_elo: float = 1500.0,
    ) -> tuple[tuple[str, ...], dict[str, float]]:
        """Charge les insights de correlation des deux equipes et les convertit
        en textes d'explication + ajustements xG (valides uniquement, et
        seulement quand la condition du split est remplie au match courant)."""
        try:
            cursor.execute(
                """
                SELECT insight_code, team_id, subject_label, effect_value,
                       baseline_value, sample_with, sample_without, is_validated,
                       details_json
                FROM model.correlation_insights
                WHERE team_id = ANY(%(teams)s)
                ORDER BY is_validated DESC, q_value ASC NULLS LAST, effect_value DESC
                LIMIT 12
                """,
                {"teams": [home_team_id, away_team_id]},
            )
            rows = cursor.fetchall()
            if not rows:
                return (), {}
            home_rest = self._rest_days_for_team(cursor, home_team_id, as_of)
            away_rest = self._rest_days_for_team(cursor, away_team_id, as_of)
            family = competition_family(league_name)
            home_context = {
                "comp_family": family,
                "after_big_loss": self._last_match_was_big_loss(cursor, home_team_id, as_of),
                "opponent_is_strong": away_elo >= self.STRONG_OPPONENT_ELO,
            }
            away_context = {
                "comp_family": family,
                "after_big_loss": self._last_match_was_big_loss(cursor, away_team_id, as_of),
                "opponent_is_strong": home_elo >= self.STRONG_OPPONENT_ELO,
            }
            return fixture_insight_signals(
                rows, home_team_id, away_team_id, home_rest, away_rest,
                home_context=home_context, away_context=away_context,
            )
        except Exception:
            self._connection.rollback()
            return (), {}

    # Half-life of past-match relevance, in days. A 365-day-old match counts
    # half as much as a fresh one; a 2-year-old match a quarter as much.
    RECENCY_HALF_LIFE_DAYS = 365.0
    # Reference date "now" for the decay. Set lazily per call via _evaluation_now()
    # so tests can stub it; production reads datetime.now(timezone.utc).
    _RECENCY_NOW_OVERRIDE: datetime | None = None

    def _evaluation_now(self) -> datetime:
        return self._RECENCY_NOW_OVERRIDE or datetime.now(timezone.utc)

    def _compute_layered_team_metrics(self, rows) -> dict[str, float]:
        empty = {
            "goals_for": 1.20,
            "goals_against": 1.20,
            "result_points": 0.50,
            "recent_form": 0.0,
            "data_quality": 0.0,
        }
        if not rows:
            return empty

        now = self._evaluation_now()
        weighted_rows: list[tuple[float, float, float, float, float]] = []
        for row in rows:
            kickoff_utc, league_name, goals_for, goals_against, result_points = row
            if goals_for is None or goals_against is None or result_points is None:
                continue
            recency = self._recency_weight(kickoff_utc, now)
            competition = self._competition_weight(str(league_name))
            weight = recency * competition
            if weight <= 0.0:
                continue
            weighted_rows.append((
                weight,
                float(goals_for),
                float(goals_against),
                float(result_points),
                recency,
            ))

        if not weighted_rows:
            return empty

        # Recent (decayed weight > 0.5 → roughly last year), form-focused window.
        recent_rows = [r for r in weighted_rows if r[4] > 0.50]
        long_term_rows = weighted_rows

        long_term = self._weighted_mean(long_term_rows)
        recent = self._weighted_mean(recent_rows) if recent_rows else long_term

        # Blend long-term baseline with recent form (heavy recent tilt).
        blend_recent = 0.65 if recent_rows else 0.0
        blend_long = 1.0 - blend_recent
        goals_for = recent["goals_for"] * blend_recent + long_term["goals_for"] * blend_long
        goals_against = (
            recent["goals_against"] * blend_recent + long_term["goals_against"] * blend_long
        )
        result_points = (
            recent["result_points"] * blend_recent + long_term["result_points"] * blend_long
        )

        recent_form_signal = (recent["result_points"] - 0.5) * 2.0 if recent_rows else 0.0

        # Data quality reflects how many decay-weighted matches we actually have.
        effective_matches = sum(r[0] for r in weighted_rows)
        data_quality = max(0.0, min(1.0, effective_matches / 12.0))

        return {
            "goals_for": goals_for,
            "goals_against": goals_against,
            "result_points": result_points,
            "recent_form": recent_form_signal,
            "data_quality": data_quality,
        }

    @staticmethod
    def _weighted_mean(rows: list[tuple[float, float, float, float, float]]) -> dict[str, float]:
        total = sum(r[0] for r in rows)
        if total <= 0.0:
            return {"goals_for": 1.20, "goals_against": 1.20, "result_points": 0.50}
        return {
            "goals_for": sum(r[1] * r[0] for r in rows) / total,
            "goals_against": sum(r[2] * r[0] for r in rows) / total,
            "result_points": sum(r[3] * r[0] for r in rows) / total,
        }

    @classmethod
    def _recency_weight(cls, kickoff_utc, now: datetime) -> float:
        if kickoff_utc is None:
            return 0.40  # Match with unknown date is downweighted but kept.
        if isinstance(kickoff_utc, datetime):
            ts = kickoff_utc if kickoff_utc.tzinfo else kickoff_utc.replace(tzinfo=timezone.utc)
        else:
            return 0.40
        age_days = max(0.0, (now - ts).total_seconds() / 86400.0)
        return float(math.pow(0.5, age_days / cls.RECENCY_HALF_LIFE_DAYS))

    def _competition_weight(self, league_name: str) -> float:
        """Hierarchical competition multiplier — canonical logic lives in
        spe_prediction.rating so the Elo K-factor, the layered metrics and
        the XGBoost trainer all agree."""
        return _rating_competition_weight(league_name)

    def _load_totals_offers(self, cursor, fixture_id: int) -> tuple[TwoWayOffer, ...]:
        cursor.execute(
            """
            SELECT b.bookmaker_id, b.bookmaker_name, o.over_odd, o.under_odd
            FROM reporting.v_latest_odds_totals o
            JOIN core.bookmakers b ON b.bookmaker_id = o.bookmaker_id
            WHERE o.fixture_id = %s
              AND o.total_line = %s
            ORDER BY b.bookmaker_name
            """,
            (fixture_id, OU25_GOAL_LINE),
        )
        return tuple(
            TwoWayOffer(
                market_code="OU25",
                bookmaker=str(bookmaker_name),
                first_odd=float(over_odd),
                second_odd=float(under_odd),
                bookmaker_id=int(bookmaker_id),
            )
            for bookmaker_id, bookmaker_name, over_odd, under_odd in cursor.fetchall()
        )

    def _load_btts_offers(self, cursor, fixture_id: int) -> tuple[TwoWayOffer, ...]:
        cursor.execute(
            """
            SELECT b.bookmaker_id, b.bookmaker_name, o.yes_odd, o.no_odd
            FROM reporting.v_latest_odds_btts o
            JOIN core.bookmakers b ON b.bookmaker_id = o.bookmaker_id
            WHERE o.fixture_id = %s
            ORDER BY b.bookmaker_name
            """,
            (fixture_id,),
        )
        return tuple(
            TwoWayOffer(
                market_code="BTTS",
                bookmaker=str(bookmaker_name),
                first_odd=float(yes_odd),
                second_odd=float(no_odd),
                bookmaker_id=int(bookmaker_id),
            )
            for bookmaker_id, bookmaker_name, yes_odd, no_odd in cursor.fetchall()
        )

    def _load_market_offers(self, cursor, fixture_id: int) -> tuple[MarketOdds, ...]:
        cursor.execute(
            """
            SELECT b.bookmaker_id, b.bookmaker_name, o.home_odd, o.draw_odd, o.away_odd
            FROM reporting.v_latest_odds_1x2 o
            JOIN core.bookmakers b ON b.bookmaker_id = o.bookmaker_id
            WHERE o.fixture_id = %s
            ORDER BY b.bookmaker_name
            """,
            (fixture_id,),
        )
        offers: list[MarketOdds] = []
        for bookmaker_id, bookmaker_name, home_odd, draw_odd, away_odd in cursor.fetchall():
            offers.append(
                MarketOdds(
                    bookmaker=str(bookmaker_name),
                    bookmaker_id=int(bookmaker_id),
                    home_odd=float(home_odd),
                    draw_odd=float(draw_odd),
                    away_odd=float(away_odd),
                )
            )
        return tuple(offers)

    def _build_market_consensus(self, offers: Sequence[MarketOdds]) -> MarketOdds | None:
        if not offers:
            return None

        home_values = [offer.home_odd for offer in offers]
        draw_values = [offer.draw_odd for offer in offers]
        away_values = [offer.away_odd for offer in offers]

        def _spread(values: list[float]) -> float:
            midpoint = median(values)
            if midpoint <= 0:
                return 0.0
            return (max(values) - min(values)) / midpoint

        return MarketOdds(
            bookmaker="CONSENSUS",
            home_odd=float(median(home_values)),
            draw_odd=float(median(draw_values)),
            away_odd=float(median(away_values)),
            source_count=len(offers),
            home_spread=_spread(home_values),
            draw_spread=_spread(draw_values),
            away_spread=_spread(away_values),
        )

    def _home_advantage_for_league(self, league_name: str) -> float:
        lowered = league_name.lower()
        if "world cup" in lowered:
            return 0.04
        return 0.16

    @staticmethod
    def _matches_target_bookmaker(bookmaker_name: str | None, target: str) -> bool:
        if not bookmaker_name or not target:
            return False
        normalized = bookmaker_name.strip().lower().replace(" ", "").replace("-", "")
        return target.replace(" ", "").replace("-", "") in normalized

    def _build_deal_candidates(
        self,
        result: AnalysisResult,
        bookmaker_offers: Sequence[MarketOdds],
        consensus_odds: MarketOdds | None,
    ) -> tuple[SelectionDecision, ...]:
        if not bookmaker_offers:
            return tuple()

        # Deals are surfaced from the target bookmaker (where the user actually
        # bets) when it prices this fixture. The consensus passed below stays
        # computed from ALL offers, so edge/price_outlier keep their meaning.
        # If the target book has no offer here, fall back to every book so the
        # board still shows actionable information.
        target = getattr(self, "_target_bookmaker", "") or ""
        if target:
            target_offers = tuple(
                offer for offer in bookmaker_offers
                if self._matches_target_bookmaker(offer.bookmaker, target)
            )
            if target_offers:
                bookmaker_offers = target_offers

        candidates: list[SelectionDecision] = []
        for offer in bookmaker_offers:
            implied = remove_overround(offer)
            decisions = build_selection_decisions(
                probabilities=result.probabilities,
                market_odds=offer,
                implied_probabilities=implied,
                confidence_score=result.top_selection.confidence_score,
                config=self._decision_config,
                consensus_odds=consensus_odds,
            )
            candidates.extend(selection for selection in decisions if selection.recommended)

        candidates.sort(
            key=lambda selection: (
                selection.ranking_score,
                selection.expected_value or -999.0,
                selection.edge_probability or -999.0,
                selection.price_outlier_score,
            ),
            reverse=True,
        )
        if not candidates:
            return tuple()

        # UNE conviction par match : le classement ne retient que LA meilleure
        # selection du fixture (celle du candidat le mieux score). Proposer
        # HOME et AWAY sur le meme match n'est jamais "le meilleur deal" —
        # c'est deux paris correles dont un seul peut gagner.
        best_selection_code = candidates[0].selection_code
        candidates = [c for c in candidates if c.selection_code == best_selection_code]

        # Diversification: cap at 2 rows for that selection (best book + a
        # runner-up for comparability).
        return tuple(candidates[:2])

    @staticmethod
    def _select_best_conviction(
        candidates: tuple[SelectionDecision, ...],
    ) -> tuple[SelectionDecision, ...]:
        """Arbitrage final du match : garde la meilleure conviction
        (marche + selection) tous marches confondus, 2 books max."""
        if not candidates:
            return candidates
        best = max(candidates, key=lambda d: d.ranking_score)
        best_key = (best.market_code, best.selection_code)
        kept = [
            d for d in candidates
            if (d.market_code, d.selection_code) == best_key
        ]
        kept.sort(key=lambda d: d.ranking_score, reverse=True)
        return tuple(kept[:2])

    def _build_derived_deal_candidates(
        self,
        context: FixtureContext | None,
        exact_score_analysis,
        confidence_score: float,
        cursor=None,
    ) -> tuple[SelectionDecision, ...]:
        """Candidats value bet sur les marches derives (O/U 2.5, BTTS).

        Probabilite modele vs cote reelle, gardes/cible identiques au 1X2.
        Source de proba : le MODELE DEDIE de buts (XGBoost, bat le Poisson-derive
        sur holdout) si dispo, sinon repli sur la proba derivee-du-Poisson.
        """
        if context is None or exact_score_analysis is None:
            return tuple()
        probabilities = None
        if cursor is not None:
            probabilities = predict_derived_probabilities(
                cursor,
                context.fixture.home_team.team_id,
                context.fixture.away_team.team_id,
                self._evaluation_now(),
                context.fixture.home_advantage,
            )
        if probabilities is None:
            probabilities = calibrated_derived_probabilities(exact_score_analysis.full_distribution)

        target = getattr(self, "_target_bookmaker", "") or ""
        selected: list[SelectionDecision] = []
        for offers in (context.totals_offers, context.btts_offers):
            if not offers:
                continue
            market_code = offers[0].market_code
            consensus = consensus_two_way(offers)
            playable_offers = offers
            if target:
                target_offers = tuple(
                    offer for offer in offers
                    if self._matches_target_bookmaker(offer.bookmaker, target)
                )
                if target_offers:
                    playable_offers = target_offers

            candidates: list[SelectionDecision] = []
            for offer in playable_offers:
                decisions = build_two_way_decisions(
                    market_code=market_code,
                    probabilities=probabilities,
                    offer=offer,
                    confidence_score=confidence_score,
                    config=self._decision_config,
                    consensus=consensus,
                )
                candidates.extend(d for d in decisions if d.recommended)
            if not candidates:
                continue
            candidates.sort(
                key=lambda d: (
                    d.ranking_score,
                    d.expected_value or -999.0,
                    d.edge_probability or -999.0,
                ),
                reverse=True,
            )
            best_code = candidates[0].selection_code
            selected.extend([d for d in candidates if d.selection_code == best_code][:2])

        # Garde anti-correlation intra-match : OVER et BTTS_YES (ou UNDER et
        # BTTS_NO) recommandes ensemble ne sont pas deux convictions — c'est
        # LA MEME these ("il y aura des buts" / "il n'y en aura pas") exprimee
        # sur deux marches correles. Miser les deux double l'exposition a un
        # seul desaccord avec le book. On ne retient que la mieux scoree,
        # comme pour le HOME+AWAY du 1X2.
        goals_theses = ({"OVER", "BTTS_YES"}, {"UNDER", "BTTS_NO"})
        for thesis in goals_theses:
            in_thesis = [d for d in selected if d.selection_code in thesis]
            markets_hit = {d.market_code for d in in_thesis}
            if len(markets_hit) > 1:
                best_market = max(in_thesis, key=lambda d: d.ranking_score).market_code
                selected = [
                    d for d in selected
                    if d.selection_code not in thesis or d.market_code == best_market
                ]
        return tuple(selected)

    # ------------------------------------------------------------------
    # Marches supplementaires : O/U 1.5/3.5 (modeles dedies), DNB & double
    # chance (derives du 1X2). Memes gardes/cible que les autres deals.
    # ------------------------------------------------------------------
    def _pick_two_way(self, offers, market_code, probabilities, confidence_score, target):
        consensus = consensus_two_way(offers)
        playable = offers
        if target:
            t = tuple(o for o in offers if self._matches_target_bookmaker(o.bookmaker, target))
            if t:
                playable = t
        candidates: list[SelectionDecision] = []
        for offer in playable:
            candidates.extend(
                d for d in build_two_way_decisions(
                    market_code=market_code, probabilities=probabilities, offer=offer,
                    confidence_score=confidence_score, config=self._decision_config,
                    consensus=consensus,
                ) if d.recommended
            )
        if not candidates:
            return []
        candidates.sort(key=lambda d: (d.ranking_score, d.expected_value or -999.0), reverse=True)
        best = candidates[0].selection_code
        return [d for d in candidates if d.selection_code == best][:2]

    def _load_alt_totals_offers(self, cursor, fixture_id, line, market_code):
        cursor.execute(
            """
            SELECT DISTINCT ON (t.bookmaker_id) b.bookmaker_code, t.bookmaker_id,
                   t.over_odd, t.under_odd
            FROM core.fixture_odds_totals t
            JOIN core.bookmakers b ON b.bookmaker_id = t.bookmaker_id
            WHERE t.fixture_id = %s AND t.total_line = %s
            ORDER BY t.bookmaker_id, t.captured_at DESC
            """,
            (fixture_id, line),
        )
        return [
            TwoWayOffer(market_code=market_code, bookmaker=str(code), bookmaker_id=int(bid),
                        first_odd=float(over), second_odd=float(under))
            for code, bid, over, under in cursor.fetchall()
            if float(over) > 1.0 and float(under) > 1.0
        ]

    def _load_market_rows(self, cursor, fixture_id, market_code):
        cursor.execute(
            """
            SELECT DISTINCT ON (m.bookmaker_id, m.selection_code)
                   m.bookmaker_id, b.bookmaker_code, m.selection_code, m.decimal_odd
            FROM core.fixture_odds_market m
            JOIN core.bookmakers b ON b.bookmaker_id = m.bookmaker_id
            WHERE m.fixture_id = %s AND m.market_code = %s
            ORDER BY m.bookmaker_id, m.selection_code, m.captured_at DESC
            """,
            (fixture_id, market_code),
        )
        by_book: dict[int, dict] = {}
        for bid, code, sel, odd in cursor.fetchall():
            slot = by_book.setdefault(int(bid), {"code": str(code), "odds": {}})
            slot["odds"][str(sel)] = float(odd)
        return by_book

    def _load_handicap_offers(self, cursor, fixture_id, target):
        """Meilleure cote par (cote, ligne) sur le marche SPREAD, deja devigee
        2-way (home@L pairee avec away@-L du meme book). Exchanges exclus
        (prix placeholder, lecon du golf) ; si la cible n'a aucune ligne,
        repli sur tous les books (meme logique que les autres marches)."""
        cursor.execute(
            """
            SELECT DISTINCT ON (m.bookmaker_id, m.selection_code, m.line)
                m.bookmaker_id, b.bookmaker_code, b.bookmaker_name,
                m.selection_code, m.line, m.decimal_odd
            FROM core.fixture_odds_market m
            JOIN core.bookmakers b ON b.bookmaker_id = m.bookmaker_id
            WHERE m.fixture_id = %s AND m.market_code = 'SPREAD'
              AND b.bookmaker_code !~ '_EX_'
              AND b.bookmaker_code NOT IN ('MATCHBOOK', 'SMARKETS', 'BETDAQ')
            ORDER BY m.bookmaker_id, m.selection_code, m.line, m.captured_at DESC
            """,
            (fixture_id,),
        )
        raw_rows = cursor.fetchall()
        rows = [r for r in raw_rows
                if not target or self._matches_target_bookmaker(str(r[1]), target)]
        if not rows:
            rows = raw_rows  # cible sans ligne handicap -> tous les books
        by_book: dict[int, dict] = {}
        for bid, code, name, sel, line, odd in rows:
            slot = by_book.setdefault(int(bid), {"name": name, "HOME": {}, "AWAY": {}})
            slot[str(sel)][round(float(line), 2)] = float(odd)
        # Pour chaque book, apparie home@L / away@-L, devige, garde la meilleure cote.
        best: dict[tuple, tuple] = {}
        for bid, v in by_book.items():
            for line_home, home_odd in v["HOME"].items():
                away_odd = v["AWAY"].get(round(-line_home, 2))
                if away_odd is None or home_odd <= 1.0 or away_odd <= 1.0:
                    continue
                overround = 1.0 / home_odd + 1.0 / away_odd
                if overround <= 0:
                    continue
                for sel, sel_line, odd in (("HOME", line_home, home_odd),
                                           ("AWAY", round(-line_home, 2), away_odd)):
                    fair = (1.0 / odd) / overround
                    key = (sel, sel_line)
                    if key not in best or odd > best[key][1]:
                        best[key] = (int(bid), odd, fair, v["name"])
        return best

    def _build_extended_deals(self, cursor, context, result, exact_score_analysis, confidence_score):
        if context is None or result is None:
            return tuple()
        home_id = context.fixture.home_team.team_id
        away_id = context.fixture.away_team.team_id
        home_adv = context.fixture.home_advantage
        target = getattr(self, "_target_bookmaker", "") or ""
        ph, pd, pa = result.probabilities.home, result.probabilities.draw, result.probabilities.away
        selected: list[SelectionDecision] = []

        # O/U 1.5 et 3.5 : modeles dedies + cotes lignes alternatives.
        for market_code, line, model_key in (("OU15", 1.5, "over15"), ("OU35", 3.5, "over35")):
            offers = self._load_alt_totals_offers(cursor, result.fixture_id, line, market_code)
            if not offers:
                continue
            p_over = predict_market(model_key, cursor, home_id, away_id, None, home_adv)
            if p_over is None:
                continue
            selected.extend(self._pick_two_way(
                offers, market_code, {"OVER": p_over, "UNDER": 1.0 - p_over},
                confidence_score, target,
            ))

        # DNB : derive du 1X2 (proba renormalisee hors nul).
        dnb = self._load_market_rows(cursor, result.fixture_id, "DNB")
        denom = ph + pa
        if dnb and denom > 0:
            offers = [
                TwoWayOffer(market_code="DNB", bookmaker=v["code"], bookmaker_id=bid,
                            first_odd=v["odds"]["HOME"], second_odd=v["odds"]["AWAY"])
                for bid, v in dnb.items()
                if "HOME" in v["odds"] and "AWAY" in v["odds"]
                and v["odds"]["HOME"] > 1.0 and v["odds"]["AWAY"] > 1.0
            ]
            if offers:
                selected.extend(self._pick_two_way(
                    offers, "DNB", {"HOME": ph / denom, "AWAY": pa / denom},
                    confidence_score, target,
                ))

        # Double chance : 3 issues chevauchantes -> devig propre (somme -> 2).
        dc_probs = {"DC_1X": ph + pd, "DC_12": ph + pa, "DC_X2": pd + pa}
        dc = self._load_market_rows(cursor, result.fixture_id, "DOUBLE_CHANCE")
        dc_candidates: list[SelectionDecision] = []
        for bid, v in dc.items():
            odds = v["odds"]
            if not all(s in odds and odds[s] > 1.0 for s in ("DC_1X", "DC_12", "DC_X2")):
                continue
            if target and not self._matches_target_bookmaker(v["code"], target):
                continue
            raw = {s: 1.0 / odds[s] for s in dc_probs}
            total = sum(raw.values())
            if total <= 0:
                continue
            for sel, model_p in dc_probs.items():
                implied = min(1.0, raw[sel] / (total / 2.0))
                dc_candidates.append(build_selection_decision(
                    market_code="DOUBLE_CHANCE", selection_code=sel,
                    probability=model_p, market_odd=odds[sel],
                    implied_probability=implied, confidence_score=confidence_score,
                    config=self._decision_config, bookmaker=v["code"], bookmaker_id=bid,
                ))
        dc_candidates = [d for d in dc_candidates if d.recommended]
        if dc_candidates:
            dc_candidates.sort(key=lambda d: (d.ranking_score, d.expected_value or -999.0), reverse=True)
            best = dc_candidates[0].selection_code
            selected.extend([d for d in dc_candidates if d.selection_code == best][:2])

        # Handicap asiatique : P(couvre la ligne) depuis la distribution de
        # marge (matrice Dixon-Coles), vs meilleure cote devigee 2-way par
        # (cote, ligne). Ligne 0 exclue (== DNB, deja couvert) ; lignes
        # extremes exclues (queues de distribution peu fiables).
        if exact_score_analysis is not None:
            hcp_offers = self._load_handicap_offers(cursor, result.fixture_id, target)
            hcp_candidates: list[SelectionDecision] = []
            for (sel, line), (book_id, odd, fair_implied, _book_name) in hcp_offers.items():
                if abs(line) < 0.05 or abs(line) > 2.5:
                    continue
                model_p = handicap_win_probability(
                    exact_score_analysis.full_distribution, line, is_home=(sel == "HOME"),
                )
                hcp_candidates.append(build_selection_decision(
                    market_code="HANDICAP", selection_code=sel,
                    probability=model_p, market_odd=odd,
                    implied_probability=fair_implied, confidence_score=confidence_score,
                    config=self._decision_config, bookmaker=str(_book_name or ""),
                    bookmaker_id=book_id, line=line,
                ))
            hcp_candidates = [d for d in hcp_candidates if d.recommended]
            if hcp_candidates:
                hcp_candidates.sort(key=lambda d: (d.ranking_score, d.expected_value or -999.0), reverse=True)
                top = hcp_candidates[0]
                # UNE conviction handicap par match : meme cote + meme ligne.
                selected.extend([
                    d for d in hcp_candidates
                    if d.selection_code == top.selection_code and d.line == top.line
                ][:2])
        return tuple(selected)

    @staticmethod
    def _fixture_data_quality(context: FixtureContext | None) -> float:
        if context is None:
            return 0.0
        team_avg = (
            context.fixture.home_team.data_quality + context.fixture.away_team.data_quality
        ) / 2.0
        market_count = context.market_odds.source_count if context.market_odds else 0
        market_coverage = min(1.0, market_count / 10.0)
        return round(max(0.0, min(1.0, 0.55 * team_avg + 0.45 * market_coverage)), 4)

    @staticmethod
    def _fixture_freshness(consensus_odds: MarketOdds | None) -> float:
        if consensus_odds is None:
            return 0.0
        average_spread = (
            consensus_odds.home_spread + consensus_odds.draw_spread + consensus_odds.away_spread
        ) / 3.0
        # Tight spread = fresh, aligned market. We treat 0.30+ spread as stale.
        return round(max(0.0, min(1.0, 1.0 - (average_spread / 0.30))), 4)
