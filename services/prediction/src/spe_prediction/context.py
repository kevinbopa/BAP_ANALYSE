"""Extra-sportive context factors — foundation layer.

Each factor is stored in ``core.fixture_context_factors`` as a normalized
value in [-1, +1] FROM THE HOME TEAM'S PERSPECTIVE (+1 = strongly favours
the home side), with a per-row weight in [0, 1].

This module converts those rows into xG deltas the Poisson model already
consumes via ``FixtureFeatures.external_adjustments``. Factor scales are
deliberately conservative — extra-sportive effects are real but small, and
each factor is capped so no manual entry can dominate the statistical model.

Sources land progressively:
  - MANUAL: saisie humaine (dashboard) — disponible des maintenant
  - API:    meteo, arbitre... (connecteurs a venir)
  - MODEL:  facteurs derives (voyage calcule des coordonnees stades, etc.)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping


@dataclass(frozen=True)
class ContextFactorSpec:
    code: str
    label_fr: str
    # Max absolute xG swing this factor can apply to ONE team.
    max_goal_delta: float
    # True: the factor moves both teams in opposite directions (zero-sum,
    # e.g. crowd support). False: it can depress both (e.g. heavy rain).
    zero_sum: bool


FACTOR_SPECS: dict[str, ContextFactorSpec] = {
    "WEATHER": ContextFactorSpec("WEATHER", "Meteo", max_goal_delta=0.12, zero_sum=False),
    "STAKES": ContextFactorSpec("STAKES", "Enjeu du match", max_goal_delta=0.15, zero_sum=True),
    "TRAVEL": ContextFactorSpec("TRAVEL", "Deplacement / decalage", max_goal_delta=0.10, zero_sum=True),
    "REFEREE": ContextFactorSpec("REFEREE", "Tendance arbitrale", max_goal_delta=0.08, zero_sum=True),
    "CROWD": ContextFactorSpec("CROWD", "Public / ambiance", max_goal_delta=0.10, zero_sum=True),
    "NEWS": ContextFactorSpec("NEWS", "Actualite extra-sportive", max_goal_delta=0.12, zero_sum=True),
}


def aggregate_context_factors(
    rows: Iterable[tuple[str, float, float]],
) -> dict[str, float]:
    """(factor_code, factor_value, weight) rows → xG deltas.

    Returns {"home_goal_delta": x, "away_goal_delta": y} to be MERGED
    (added) into the fixture's external_adjustments.
    """
    home_delta = 0.0
    away_delta = 0.0
    for code, value, weight in rows:
        spec = FACTOR_SPECS.get(str(code).upper())
        if spec is None:
            continue
        clamped_value = max(-1.0, min(1.0, float(value)))
        clamped_weight = max(0.0, min(1.0, float(weight)))
        swing = spec.max_goal_delta * clamped_value * clamped_weight
        if spec.zero_sum:
            home_delta += swing
            away_delta -= swing
        else:
            # Non zero-sum (meteo): une valeur negative deprime les deux
            # attaques, une valeur positive les stimule legerement.
            home_delta += swing
            away_delta += swing
    return {"home_goal_delta": round(home_delta, 4), "away_goal_delta": round(away_delta, 4)}


def merge_adjustments(
    base: Mapping[str, float],
    extra: Mapping[str, float],
) -> dict[str, float]:
    merged = dict(base)
    for key, value in extra.items():
        merged[key] = merged.get(key, 0.0) + value
    return merged


def load_context_adjustments(cursor, fixture_id: int) -> dict[str, float]:
    """SQL wrapper over core.fixture_context_factors."""
    cursor.execute(
        """
        SELECT factor_code, factor_value, weight
        FROM core.fixture_context_factors
        WHERE fixture_id = %s
        """,
        (fixture_id,),
    )
    rows = [(str(c), float(v), float(w)) for c, v, w in cursor.fetchall()]
    if not rows:
        return {}
    return aggregate_context_factors(rows)
