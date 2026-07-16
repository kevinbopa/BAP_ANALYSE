"""Team-level signals derived from the player layer (API-Football data).

Turns squads + injuries + per-match player ratings into two signals the
engine can consume:

  - ``availability_index`` (0..1): rating-weighted share of the squad that is
    actually available. 1.0 = full squad; losing a 7.5-rated starter hurts
    more than losing a 6.3-rated bench player.
  - ``key_absences``: names of unavailable players whose weighted importance
    crosses the "key player" threshold — surfaced in the explanation.

Everything degrades gracefully: no player data in DB → (1.0, ()) and the
engine behaves exactly as before the player layer existed.
"""
from __future__ import annotations

from dataclasses import dataclass

DEFAULT_PLAYER_WEIGHT = 6.5   # neutral match rating when no stats exist
KEY_PLAYER_RATING = 7.0       # above this, an absence is worth naming
RECENT_STATS_WINDOW = 10      # matches considered for a player's weight


@dataclass(frozen=True)
class TeamAvailability:
    availability_index: float
    key_absences: tuple[str, ...]
    squad_size: int
    injured_count: int


def compute_weighted_availability(
    squad: list[tuple[str, float | None, bool]],
) -> TeamAvailability:
    """Pure computation from (player_name, avg_recent_rating, is_available).

    availability_index = available_weight / total_weight, where each player's
    weight is their average recent rating (fallback DEFAULT_PLAYER_WEIGHT).
    """
    if not squad:
        return TeamAvailability(1.0, (), 0, 0)

    total_weight = 0.0
    available_weight = 0.0
    key_absences: list[str] = []
    injured = 0

    for name, avg_rating, is_available in squad:
        weight = avg_rating if avg_rating is not None else DEFAULT_PLAYER_WEIGHT
        total_weight += weight
        if is_available:
            available_weight += weight
        else:
            injured += 1
            if weight >= KEY_PLAYER_RATING:
                key_absences.append(name)

    index = available_weight / total_weight if total_weight > 0 else 1.0
    return TeamAvailability(
        availability_index=round(max(0.0, min(1.0, index)), 4),
        key_absences=tuple(key_absences),
        squad_size=len(squad),
        injured_count=injured,
    )


def load_team_availability(cursor, team_id: int) -> TeamAvailability:
    """SQL wrapper: squad members + active injuries + recent avg rating."""
    cursor.execute(
        """
        WITH squad AS (
            SELECT DISTINCT sm.player_id, p.player_name
            FROM core.team_squad_members sm
            JOIN core.players p ON p.player_id = sm.player_id
            WHERE sm.team_id = %(team_id)s
        ),
        recent_ratings AS (
            SELECT pms.player_id, AVG(pms.rating) AS avg_rating
            FROM (
                SELECT player_id, rating,
                       row_number() OVER (
                           PARTITION BY player_id ORDER BY kickoff_utc DESC NULLS LAST
                       ) AS rn
                FROM core.player_match_stats
                WHERE rating IS NOT NULL
            ) pms
            WHERE pms.rn <= %(window)s
            GROUP BY pms.player_id
        ),
        active_injuries AS (
            SELECT DISTINCT player_id
            FROM core.player_injuries
            WHERE team_id = %(team_id)s AND is_active
        )
        SELECT
            s.player_name,
            r.avg_rating,
            (ai.player_id IS NULL) AS is_available
        FROM squad s
        LEFT JOIN recent_ratings r ON r.player_id = s.player_id
        LEFT JOIN active_injuries ai ON ai.player_id = s.player_id
        """,
        {"team_id": team_id, "window": RECENT_STATS_WINDOW},
    )
    rows = [
        (str(name), float(avg) if avg is not None else None, bool(available))
        for name, avg, available in cursor.fetchall()
    ]
    return compute_weighted_availability(rows)
