from __future__ import annotations

from datetime import date
from typing import Any, Sequence


def _scope_sql(
    alias: str,
    params: list[Any],
    *,
    golf_tournament_id: int | None = None,
    tours: Sequence[str] | None = None,
    date_from: date | str | None = None,
    date_to: date | str | None = None,
) -> str:
    clauses: list[str] = []
    if golf_tournament_id is not None:
        clauses.append(f"{alias}.golf_tournament_id = %s")
        params.append(golf_tournament_id)
    normalized_tours = [str(tour).strip().lower() for tour in (tours or ()) if str(tour).strip()]
    if normalized_tours:
        clauses.append(f"{alias}.tour_code = ANY(%s)")
        params.append(normalized_tours)
    date_expr = f"COALESCE({alias}.date_start, {alias}.commence_time::date)"
    if date_from:
        clauses.append(f"{date_expr} >= %s::date")
        params.append(str(date_from)[:10])
    if date_to:
        clauses.append(f"{date_expr} <= %s::date")
        params.append(str(date_to)[:10])
    return (" AND " + " AND ".join(clauses)) if clauses else ""


def _fetch_one_dict(cursor, query: str, params: Sequence[Any]) -> dict[str, Any]:
    cursor.execute(query, tuple(params))
    row = cursor.fetchone()
    if row is None:
        return {}
    return {
        column.name: row[index]
        for index, column in enumerate(cursor.description)
    }


def summarize_golf_quality_snapshot(snapshot: dict[str, Any]) -> str:
    issues = list(snapshot.get("issues") or [])
    if issues:
        return " | ".join(issues)
    return "Qualite golf OK."


def compute_golf_quality_snapshot(
    cursor,
    *,
    golf_tournament_id: int | None = None,
    tours: Sequence[str] | None = None,
    date_from: date | str | None = None,
    date_to: date | str | None = None,
) -> dict[str, Any]:
    params: list[Any] = []
    scope = _scope_sql(
        "gt",
        params,
        golf_tournament_id=golf_tournament_id,
        tours=tours,
        date_from=date_from,
        date_to=date_to,
    )
    snapshot = _fetch_one_dict(
        cursor,
        f"""
        WITH scope_tournaments AS (
            SELECT gt.golf_tournament_id,
                   gt.tournament_name,
                   gt.tour_code,
                   COALESCE(gt.date_start, gt.commence_time::date) AS event_date,
                   gt.catalog_status,
                   gt.last_field_sync_at
            FROM core.golf_tournaments gt
            WHERE gt.completed_at IS NULL {scope}
        ),
        latest_prediction_batch AS (
            SELECT gp.golf_tournament_id, MAX(gp.generated_at) AS generated_at
            FROM model.golf_predictions gp
            JOIN scope_tournaments st ON st.golf_tournament_id = gp.golf_tournament_id
            GROUP BY gp.golf_tournament_id
        ),
        latest_predictions AS (
            SELECT gp.*
            FROM model.golf_predictions gp
            JOIN latest_prediction_batch lb
              ON lb.golf_tournament_id = gp.golf_tournament_id
             AND lb.generated_at = gp.generated_at
        )
        SELECT
            (SELECT COUNT(*) FROM scope_tournaments) AS tournaments_in_scope,
            (SELECT COUNT(*) FROM scope_tournaments
              WHERE catalog_status IN ('FIELD_SYNCED', 'ODDS_SYNCED', 'COMPLETED')) AS tournaments_field_ready,
            (SELECT COUNT(*) FROM scope_tournaments
              WHERE catalog_status = 'DISCOVERED') AS tournaments_catalog_only,
            (SELECT COUNT(*) FROM scope_tournaments
              WHERE event_date BETWEEN current_date AND current_date + 7
                AND catalog_status = 'DISCOVERED'
                AND COALESCE(tour_code, '') <> 'liv') AS upcoming_catalog_only,
            (SELECT COUNT(*) FROM scope_tournaments st
              WHERE event_date BETWEEN current_date AND current_date + 7
                AND catalog_status IN ('FIELD_SYNCED', 'ODDS_SYNCED', 'COMPLETED')
                AND COALESCE(tour_code, '') <> 'liv'
                AND NOT EXISTS (
                    SELECT 1 FROM latest_prediction_batch lb
                    WHERE lb.golf_tournament_id = st.golf_tournament_id
                )) AS upcoming_without_predictions,
            (SELECT COUNT(*) FROM latest_predictions) AS latest_prediction_rows,
            (SELECT COUNT(*) FROM latest_predictions lp
              JOIN scope_tournaments st ON st.golf_tournament_id = lp.golf_tournament_id
              WHERE st.event_date BETWEEN current_date - 1 AND current_date + 7
                AND lp.golf_player_id IS NULL) AS latest_prediction_rows_null_player,
            (SELECT COUNT(*) FROM latest_predictions lp
              JOIN scope_tournaments st ON st.golf_tournament_id = lp.golf_tournament_id
              WHERE st.event_date BETWEEN current_date - 1 AND current_date + 7
                AND lp.golf_player_id IS NOT NULL
                AND NOT EXISTS (
                    SELECT 1 FROM core.golf_pretournament_preds pp
                    WHERE pp.golf_tournament_id = lp.golf_tournament_id
                      AND pp.golf_player_id = lp.golf_player_id
                      AND pp.market_code = lp.market_code
                )) AS latest_visible_invalid_prediction_rows,
            (SELECT COUNT(*) FROM model.golf_deals gd
              JOIN scope_tournaments st ON st.golf_tournament_id = gd.golf_tournament_id
              WHERE gd.status_code = 'ACTIVE' AND gd.result_code IS NULL) AS active_deals,
            (SELECT COUNT(*) FROM model.golf_deals gd
              JOIN scope_tournaments st ON st.golf_tournament_id = gd.golf_tournament_id
              WHERE gd.status_code = 'ACTIVE' AND gd.result_code IS NULL
                AND (
                    gd.golf_player_id IS NULL
                    OR NOT EXISTS (
                        SELECT 1 FROM core.golf_pretournament_preds pp
                        WHERE pp.golf_tournament_id = gd.golf_tournament_id
                          AND pp.golf_player_id = gd.golf_player_id
                          AND pp.market_code = gd.market_code
                    )
                )) AS active_deals_invalid,
            (SELECT COUNT(*) FROM model.golf_matchup_deals gmd
              JOIN scope_tournaments st ON st.golf_tournament_id = gmd.golf_tournament_id
              WHERE gmd.status_code = 'ACTIVE' AND gmd.result_code IS NULL) AS active_matchups,
            (SELECT COUNT(*) FROM model.golf_matchup_deals gmd
              JOIN scope_tournaments st ON st.golf_tournament_id = gmd.golf_tournament_id
              WHERE gmd.status_code = 'ACTIVE' AND gmd.result_code IS NULL
                AND (
                    NOT EXISTS (
                        SELECT 1 FROM core.golf_pretournament_preds pp1
                        WHERE pp1.golf_tournament_id = gmd.golf_tournament_id
                          AND pp1.golf_player_id = gmd.pick_golf_player_id
                    )
                    OR NOT EXISTS (
                        SELECT 1 FROM core.golf_pretournament_preds pp2
                        WHERE pp2.golf_tournament_id = gmd.golf_tournament_id
                          AND pp2.golf_player_id = gmd.opponent_golf_player_id
                    )
                )) AS active_matchups_invalid,
            (SELECT MAX(last_field_sync_at) FROM scope_tournaments) AS last_field_sync_at,
            (SELECT MAX(generated_at) FROM latest_predictions) AS last_prediction_at
        """,
        params,
    )
    metrics = {key: int(snapshot.get(key) or 0) for key in (
        "tournaments_in_scope",
        "tournaments_field_ready",
        "tournaments_catalog_only",
        "upcoming_catalog_only",
        "upcoming_without_predictions",
        "latest_prediction_rows",
        "latest_prediction_rows_null_player",
        "latest_visible_invalid_prediction_rows",
        "active_deals",
        "active_deals_invalid",
        "active_matchups",
        "active_matchups_invalid",
    )}
    snapshot.update(metrics)
    issues: list[str] = []
    status = "ok"
    if metrics["active_deals_invalid"] > 0:
        issues.append(f"{metrics['active_deals_invalid']} deal(s) actifs hors field")
        status = "error"
    if metrics["active_matchups_invalid"] > 0:
        issues.append(f"{metrics['active_matchups_invalid']} matchup(s) actifs hors field")
        status = "error"
    if metrics["latest_visible_invalid_prediction_rows"] > 0:
        issues.append(
            f"{metrics['latest_visible_invalid_prediction_rows']} prediction(s) visibles incoherentes"
        )
        status = "error"
    if status != "error" and metrics["latest_prediction_rows_null_player"] > 0:
        issues.append(
            f"{metrics['latest_prediction_rows_null_player']} prediction(s) du dernier batch sans joueur relie"
        )
        status = "warning"
    if metrics["upcoming_without_predictions"] > 0:
        issues.append(
            f"{metrics['upcoming_without_predictions']} tournoi(s) proches sans predictions"
        )
        if status == "ok":
            status = "warning"
    if metrics["upcoming_catalog_only"] > 0:
        issues.append(
            f"{metrics['upcoming_catalog_only']} tournoi(s) proches encore au stade catalogue"
        )
        if status == "ok":
            status = "warning"
    snapshot["status"] = status
    snapshot["issues"] = issues
    snapshot["summary"] = summarize_golf_quality_snapshot(snapshot)
    return snapshot
