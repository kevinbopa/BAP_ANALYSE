from __future__ import annotations

from io import BytesIO
import json
import os
from pathlib import Path
import sys
import tempfile
from urllib.parse import urlencode


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "apps" / "dashboard"))
os.environ["SPE_AUTH_DISABLED"] = "1"

import app as dashboard_app
import vercel_app

from app import (
    ActionReport,
    MetricCard,
    UserContext,
    render_back_page,
    render_golf_detail,
    render_golf_page,
    render_golf_strategy_page,
    render_bankroll_page,
    render_page,
)


def call_wsgi_app(
    environ: dict[str, object],
) -> tuple[str, list[tuple[str, str]], str]:
    response_status: list[str] = []
    response_headers: list[tuple[str, str]] = []

    def start_response(status: str, headers: list[tuple[str, str]]) -> None:
        response_status.append(status)
        response_headers.extend(headers)

    body = b"".join(dashboard_app.application(environ, start_response)).decode("utf-8")
    return response_status[0] if response_status else "", response_headers, body


def _check_time_helpers() -> bool:
    """Verifie format_timestamp / format_relative_time — le socle temporel
    de tout l'affichage de l'app. Un bug ici casse toutes les dates."""
    from datetime import datetime, timedelta, timezone as _tz
    from app import (
        format_timestamp, format_metric_timestamp,
        format_relative_time, format_timestamp_with_relative, _coerce_datetime,
    )

    now = datetime.now(tz=_tz.utc)
    checks = {
        "coerce None -> None": _coerce_datetime(None) is None,
        "coerce datetime naif -> tz-aware":
            _coerce_datetime(datetime(2026, 7, 24, 12, 0)).tzinfo is not None,
        "coerce ISO string": _coerce_datetime("2026-07-24T12:00:00Z") is not None,
        "coerce date": _coerce_datetime(datetime(2026, 7, 24).date()) is not None,
        "coerce garbage -> None": _coerce_datetime("pas une date") is None,
        "format None -> '-'": format_timestamp(None) == "-",
        "relative 30s -> 'a l'instant'":
            format_relative_time(now - timedelta(seconds=10)) == "a l'instant",
        "relative -5min":
            "il y a 5 min" == format_relative_time(now - timedelta(minutes=5)),
        "relative +2h": "dans 2 h" == format_relative_time(now + timedelta(hours=2, seconds=5)),
        "relative -3j": "il y a 3 j" == format_relative_time(now - timedelta(days=3)),
        "relative >30j retombe sur date absolue":
            "-" not in format_relative_time(now - timedelta(days=60))[:5]
            or format_relative_time(now - timedelta(days=60)).count("-") == 2,
        "format_timestamp_with_relative contient (il y a":
            "(il y a" in format_timestamp_with_relative(now - timedelta(hours=1)),
    }
    ok = all(checks.values())
    if not ok:
        for k, v in checks.items():
            print(f"  {'OK' if v else 'FAIL'} {k}")
    return ok


def main() -> int:
    dashboard_data = {
        "metrics": [
            MetricCard("Fixtures en base", "15"),
            MetricCard("Deals FIFA World Cup", "45", "accent"),
        ],
        "leagues": ["FIFA World Cup"],
        "runs": [],
        "model_run": None,
        "upcoming_matches": [
            {
                "league_name": "FIFA World Cup",
                "home_team_name": "France",
                "away_team_name": "Canada",
                "kickoff_utc": "2026-06-27 19:00 UTC",
                "status_code": "NS",
                "home_pct": 52.4,
                "draw_pct": 24.1,
                "away_pct": 23.5,
                "pronostic": "HOME",
                "position_count": 2,
                "position_summary": {"1X2|HOME": 1, "BTTS|BTTS_YES": 1},
                "expected_home_goals": 1.64,
                "expected_away_goals": 1.02,
                "confidence_pct": 68.2,
            }
        ],
        "ranked_deals": [
            {
                "rank_position": 1,
                "fixture_id": 10,
                "kickoff_utc": "2026-06-27 19:00 UTC",
                "home_team_name": "France",
                "away_team_name": "Canada",
                "bookmaker_name": "Stake",
                "market_code": "HANDICAP",
                "selection_code": "HOME",
                "line": -1.5,
                "model_pct": 52.4,
                "implied_pct": 46.8,
                "edge_pct": 5.6,
                "market_odd": 2.14,
                "fair_odd": 1.91,
                "ranking_pct": 81.3,
                "position_count": 1,
            }
        ],
    }
    html = render_page(
        "FIFA World Cup",
        data={**dashboard_data, "view": "predictions"},
        report=ActionReport(
            title="Test dashboard",
            status="success",
            payload={"ok": True},
        ),
        error_message=None,
    )

    checks = [
        "hero-visual football" in html,
        "Poste de decision football" in html,
        "Console web V1" not in html,
        "Back verifie" not in html,
        "Predictions" in html,
        "Deals classes" not in html,
        "periodbar" in html,
        "Semaine" in html,
        "Mois" in html,
        "France" in html,
        "Test dashboard" in html,
        "Strategie" in html,
        "Back" in html,
        "Pris x2" in html,
    ]
    if not all(checks):
        print("Dashboard render smoke test failed.")
        return 1

    deals_html = render_page(
        "FIFA World Cup",
        data={**dashboard_data, "view": "deals"},
        report=None,
        error_message=None,
    )
    deals_checks = [
        "Deals classes" in deals_html,
        "<h2>Predictions</h2>" not in deals_html,
        "periodbar" in deals_html,
        "Mois" in deals_html,
        "Pris x1" in deals_html,
        "Domicile handicap -1,5" in deals_html,
        "Bet365: Handicap asiatique / Handicap - choisir Domicile -1,5" in deals_html,
        "name='view' value='deals'" in deals_html or 'name="view" value="deals"' in deals_html,
    ]
    if not all(deals_checks):
        print("Dashboard deals page smoke test failed.")
        return 1

    progress_html = render_page(
        "FIFA World Cup",
        data={**dashboard_data, "view": "predictions"},
        report=ActionReport(
            title="Mise a jour en cours : Cycle complet V1",
            status="progress",
            payload={
                "progress_pct": 67,
                "current_step": 2,
                "total_steps": 3,
                "step_label": "Etape 2/3 · Recuperation des cotes 1X2",
                "info": "Le site reste utilisable pendant la mise a jour.",
                "detail": {"records_written": 3460},
                "updated_at": "2026-07-18T23:40:00+00:00",
            },
        ),
        error_message=None,
    )
    progress_checks = [
        "Mise a jour en cours : Cycle complet V1" in progress_html,
        "progress-shell" in progress_html,
        "width:67%" in progress_html,
        "Etape 2 / 3" in progress_html,
        "Recuperation des cotes 1X2" in progress_html,
        "action-live-region" in progress_html,
        "/action-status" in progress_html,
        "action-live-toast" in progress_html,
    ]
    if not all(progress_checks):
        print("Dashboard progress render smoke test failed.")
        return 1

    original_jobs_dir = dashboard_app._ACTION_JOBS_DIR
    original_last_sweep = dashboard_app._LAST_ACTION_JOB_SWEEP_AT
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            jobs_dir = Path(tmpdir)
            dashboard_app._ACTION_JOBS_DIR = jobs_dir
            dashboard_app._LAST_ACTION_JOB_SWEEP_AT = 0.0
            orphan_json = jobs_dir / "run_predictions_999.json"
            orphan_running = jobs_dir / "run_predictions_999.running"
            jobs_dir.mkdir(parents=True, exist_ok=True)
            orphan_running.write_text("11:00:05 UTC", encoding="utf-8")
            orphan_json.write_text(
                json.dumps(
                    {
                        "status": "running",
                        "title": "Mise a jour en cours : Lancer predictions",
                        "worker_pid": 999999,
                        "action": "run_predictions",
                        "progress_pct": 0,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            dashboard_app.sweep_action_job_registry(force=True)
            orphan_cleanup_checks = [
                not orphan_running.exists(),
                not orphan_json.exists(),
            ]
            if not all(orphan_cleanup_checks):
                print("Dashboard orphan action job cleanup smoke test failed.")
                return 1
    finally:
        dashboard_app._ACTION_JOBS_DIR = original_jobs_dir
        dashboard_app._LAST_ACTION_JOB_SWEEP_AT = original_last_sweep

    bankroll_html = render_bankroll_page(
        {
            "profile": {
                "code": "balanced",
                "label": "Equilibre",
                "description": "Profil test",
                "kelly_multiplier": 0.25,
                "stake_cap_pct": 3.0,
                "exposure_cap_pct": 15.0,
                "min_edge_pct": 1.0,
                "min_ev_pct": 0.0,
                "max_odd": 5.0,
                "max_positions": 12,
                "parlay_enabled": False,
                "category_caps_pct": {"SAFE": 8.0, "MODERE": 5.0, "RISQUE": 2.0},
            },
            "simulation": {
                "final_median": 103.0,
                "final_p5": 92.0,
                "final_p95": 118.0,
                "prob_loss_pct": 34.0,
                "prob_drawdown30_pct": 2.0,
                "simulations": 100,
            },
            "lines": [
                {
                    "fixture_id": 10,
                    "label": "France vs Canada",
                    "kickoff_utc": "2026-06-27 19:00 UTC",
                    "market_code": "HANDICAP",
                    "selection_code": "HOME",
                    "line": -1.5,
                    "source": "VALUE BET",
                    "bookmaker": "Stake",
                    "market_odd": 2.14,
                    "credible_probability": 0.56,
                    "expected_value": 0.08,
                    "stake_amount": 3.0,
                    "stake_pct": 3.0,
                    "win_profit": 3.42,
                    "expected_profit": 0.24,
                    "category": "MODERE",
                    "position_count": 2,
                }
            ],
            "exposure_amount": 3.0,
            "exposure_pct": 3.0,
            "expected_profit_amount": 0.24,
            "repartition": {"SAFE": 0.0, "MODERE": 100.0, "RISQUE": 0.0},
            "days": 30,
        },
        100.0,
        30,
        "balanced",
        "mois",
    )
    # La strategie football reprend la STRUCTURE GOLF : badge « PRIS xN »,
    # prise inline (cote/mise) au lieu du modal, et contexte du pari en ligne
    # de detail (« Ou poser: Bet365 > ... »).
    bankroll_checks = [
        "Strategie de paris" in bankroll_html,
        "PRIS x2" in bankroll_html,
        "clear_position" in bankroll_html,
        "return_to" in bankroll_html and "/bankroll?" in bankroll_html,
        "Domicile handicap -1,5" in bankroll_html,
        # Le chevron du chemin est echappe par escape(), comme au golf.
        "Ou poser: Bet365 &gt; Handicap asiatique / Handicap - choisir Domicile -1,5" in bankroll_html,
        # Le pari s'enonce en une phrase, comme au golf.
        "Parier sur France avec handicap -1,5" in bankroll_html,
        # Prise inline : le formulaire remplace le bouton de modal.
        "take_position" in bankroll_html and "stake_amount" in bankroll_html,
    ]
    if not all(bankroll_checks):
        print("Dashboard football strategy memory smoke test failed.")
        return 2

    saved_football_positions = []
    original_save_bet_annotation = dashboard_app.save_bet_annotation

    def fake_save_bet_annotation(key: str, **kwargs):
        saved_football_positions.append((key, kwargs))

    dashboard_app.save_bet_annotation = fake_save_bet_annotation
    try:
        body = urlencode({
            "action": "take_position",
            "key": "DEAL|f10|HANDICAP|HOME",
            "selection_label": "Domicile handicap",
            "line": "-1.5",
            "taken_odd": "2,14",
            "stake_amount": "3,50",
            "return_to": "/bankroll?montant=100&periode=mois&profil=balanced",
        }).encode("utf-8")
        response_status = []
        response_headers = []

        def start_response_football(status: str, headers: list[tuple[str, str]]) -> None:
            response_status.append(status)
            response_headers.extend(headers)

        list(dashboard_app.application({
            "REQUEST_METHOD": "POST",
            "PATH_INFO": "/",
            "QUERY_STRING": "",
            "CONTENT_LENGTH": str(len(body)),
            "wsgi.input": BytesIO(body),
        }, start_response_football))
    finally:
        dashboard_app.save_bet_annotation = original_save_bet_annotation

    football_post_checks = [
        response_status and response_status[0].startswith("303"),
        ("Location", "/bankroll?montant=100&periode=mois&profil=balanced") in response_headers,
        saved_football_positions
        and saved_football_positions[0][0] == "DEAL|f10|HANDICAP|HOME"
        and saved_football_positions[0][1].get("take_position") is True
        and saved_football_positions[0][1].get("taken_odd") == 2.14
        and saved_football_positions[0][1].get("stake_amount") == 3.5
        and saved_football_positions[0][1].get("selection_label") == "Domicile handicap -1,5"
        and saved_football_positions[0][1].get("line") == -1.5,
    ]
    if not all(football_post_checks):
        print("Dashboard football POST memory smoke test failed.")
        return 3

    back_html = render_back_page(
        {
            "stats": {
                "total": 1,
                "settled": 0,
                "pending": 1,
                "won": 0,
                "accuracy_pct": None,
                "profit_units": 0.0,
                "roi_pct": None,
                "taken_count": 1,
                "taken_profit_units": 0.0,
                "taken_roi_pct": None,
            },
            "rows": [
                {
                    "bet_kind": "PARLAY",
                    "parlay_id": 1,
                    "fixture_id": None,
                    "outright_market_id": None,
                    "league_name": None,
                    "home_team_name": None,
                    "away_team_name": None,
                    "subject_label": "France HOME + Canada BTTS_YES",
                    "kickoff_utc": None,
                    "market_code": "PARLAY",
                    "selection_code": "2 jambes",
                    "model_probability": None,
                    "model_fair_odd": None,
                    "market_odd": 3.4,
                    "taken_odd": 3.4,
                    "stake_amount": 5.0,
                    "edge_probability": None,
                    "result_code": None,
                    "profit_units": None,
                    "taken_profit_units": None,
                    "settled_at": None,
                    "actual_outcome": None,
                    "position_count": 1,
                    "note": "",
                    "tickets": [],
                    "legs_summary": "France HOME + Canada BTTS_YES",
                }
            ],
            "equity_series": [(0.0, 0.0)],
            "equity_count": 0,
            "league_options": [],
        },
        {"kind": "all", "market": "all", "status": "all", "league": "all", "taken": "all", "sort": "date_desc"},
    )
    back_checks = [
        "Combines" in back_html,
        "COMBINE" in back_html,
        "Pris x1" in back_html,
        "Supprimer" in back_html,
        "Valider mes paris" in back_html,
        "Back football - performance verifiee" in back_html,
        "name='sport_filter'" not in back_html,
        "name='sport' value='football'" in back_html,
    ]
    if not all(back_checks):
        print("Dashboard back render smoke test failed.")
        return 4

    golf_back_html = render_back_page(
        {
            "stats": {
                "total": 0,
                "settled": 0,
                "pending": 0,
                "won": 0,
                "accuracy_pct": None,
                "profit_units": 0.0,
                "roi_pct": None,
                "taken_count": 0,
                "taken_profit_units": 0.0,
                "taken_roi_pct": None,
            },
            "rows": [],
            "equity_series": [(0.0, 0.0)],
            "equity_count": 0,
            "league_options": ["ISCO Championship"],
            "golf_stats": {
                "total": 2,
                "pending": 2,
                "settled": 0,
                "won": 0,
                "profit_units": 0.0,
                "roi_pct": None,
                "taken_count": 1,
                "taken_profit": 0.0,
                "taken_roi_pct": None,
            },
            "golf_rows": [
                {
                    "deal_type": "MATCHUP",
                    "tournament_name": "ISCO Championship",
                    "tour_code": "opp",
                    "subject": "Taken Player vs Opponent",
                    "market_code": "ROUND_MATCHUP",
                    "market_odd": 1.76,
                    "result_code": None,
                    "profit_units": None,
                    "position_count": 1,
                    "total_stake": 2.45,
                    "avg_taken_odd": 1.76,
                    "last_taken_at": "2026-07-11 12:00:00",
                    "taken_profit": None,
                },
                {
                    "deal_type": "OUTRIGHT",
                    "tournament_name": "ISCO Championship",
                    "tour_code": "opp",
                    "subject": "Untaken Player",
                    "market_code": "TOURNAMENT_WINNER",
                    "market_odd": 151.0,
                    "result_code": None,
                    "profit_units": None,
                    "position_count": 0,
                    "total_stake": None,
                    "avg_taken_odd": None,
                    "last_taken_at": None,
                    "taken_profit": None,
                },
            ],
            "golf_equity_series": [(0.0, 0.0)],
            "golf_equity_count": 0,
        },
        {
            "kind": "all",
            "markets": [],
            "statuses": [],
            "league": "all",
            "taken": "1",
            "sort": "date_desc",
            "sport": "golf",
            "day": "",
            "date_kind": "prise",
        },
    )
    golf_back_checks = [
        "Taken Player" in golf_back_html,
        "PRIS x1" in golf_back_html,
        "Untaken Player" not in golf_back_html,
        "non pris" not in golf_back_html,
        "Prises seulement" in golf_back_html,
        "Back golf - performance verifiee" in golf_back_html,
        "name='sport_filter'" not in golf_back_html,
        "name='sport' value='golf'" in golf_back_html,
    ]
    if not all(golf_back_checks):
        print("Dashboard golf back filters render smoke test failed.")
        return 5

    golf_html = render_golf_page(
        {
            "view": "deals",
            "metrics": [
                MetricCard("Competitions", "1"),
                MetricCard("Deals outright", "0", "accent"),
            ],
            "selected_tournament": "all",
            "tournaments": [
                {
                    "golf_tournament_id": 1,
                    "tournament_name": "The Open Championship",
                    "sport_title": "Golf",
                    "tour_code": "pga",
                    "course_name": "Royal Portrush",
                    "current_round": 1,
                    "commence_time": None,
                }
            ],
            "deals": [
                {
                    "golf_deal_id": 101,
                    "tournament_name": "The Open Championship",
                    "tour_code": "pga",
                    "course_name": "Royal Portrush",
                    "selection_name": "Player A",
                    "market_code": "TOURNAMENT_WINNER",
                    "model_pct": 12.0,
                    "implied_pct": 10.0,
                    "edge_pct": 2.0,
                    "market_odd": 9.0,
                    "bookmaker_name": "Bet365",
                    "position_count": 2,
                    "total_stake": 15.0,
                    "position_ids": [11, 12],
                }
            ],
            "markets": {
                "TOURNAMENT_WINNER": [
                    {
                        "tournament_name": "The Open Championship",
                        "player_name": "Player A",
                        "market_code": "TOURNAMENT_WINNER",
                        "model_probability": 0.12,
                        "model_fair_odd": 8.33,
                        "best_odd": 9.0,
                        "bookmaker_name": "Bet365",
                        "sg_total": 1.2,
                        "sg_ott": 0.4,
                        "sg_app": 0.8,
                        "sg_arg": 0.1,
                        "sg_putt": -0.1,
                    }
                ]
            },
            "matchup_deals": [
                {
                    "golf_matchup_deal_id": 202,
                    "tournament_name": "The Open Championship",
                    "tour_code": "pga",
                    "course_name": "Royal Portrush",
                    "market_code": "TOURNAMENT_MATCHUP",
                    "pick_name": "Player A",
                    "opp_name": "Player B",
                    "model_pct": 56.0,
                    "edge_pct": 4.0,
                    "market_odd": 2.1,
                    "bookmaker_name": "Bet365",
                    "position_count": 1,
                    "total_stake": 10.0,
                    "position_ids": [13],
                }
            ],
            "golf_positions": [
                {
                    "position_id": 1,
                    "selection_label": "Player A",
                    "market_code": "TOURNAMENT_WINNER",
                    "taken_odd": 9.0,
                    "stake_amount": 5.0,
                    "taken_at": "2026-07-11 10:00:00",
                    "is_matchup": False,
                },
                {
                    "position_id": 2,
                    "selection_label": "Player A",
                    "market_code": "TOURNAMENT_WINNER",
                    "taken_odd": 9.0,
                    "stake_amount": 10.0,
                    "taken_at": "2026-07-11 10:01:00",
                    "is_matchup": False,
                },
                {
                    "position_id": 3,
                    "selection_label": "Player A vs Player B",
                    "market_code": "TOURNAMENT_MATCHUP",
                    "taken_odd": 2.1,
                    "stake_amount": 10.0,
                    "taken_at": "2026-07-11 10:02:00",
                    "is_matchup": True,
                },
            ],
        }
    )
    golf_checks = [
        "dashboard-actions" in golf_html,
        "Predictions golf" in golf_html,
        "Cycle golf complet" in golf_html,
        "name='golf_period'" in golf_html,
        "name='golf_date_from'" in golf_html,
        "name='golf_date_to'" in golf_html,
        "Back (performance)" not in golf_html,
        "Retour au board" not in golf_html,
        "periodbar" in golf_html,
        "golf_period=30d" in golf_html,
        "Ou poser tes paris" in golf_html,
        "Tournoi &gt; 72 Trous - Match - 2 options" in golf_html,
        "Couverture bookmaker vs modele" in golf_html,
        "The Open Championship" in golf_html,
        "Parier que Player A gagne le tournoi" in golf_html,
        "Parier que Player A bat Player B sur le tournoi" in golf_html,
        "PRIS : 3 ticket(s)" in golf_html,
        "PRIS x2" in golf_html,
        "PRIS x1" in golf_html,
        "Supprimer prise" in golf_html,
        "Ticket 1" in golf_html,
        "Ticket 2" in golf_html,
        "taken-row" in golf_html,
        "Sync DataGolf" in golf_html,
    ]
    if not all(golf_checks):
        print("Dashboard golf render smoke test failed.")
        return 6

    golf_strategy_html = render_golf_strategy_page(
        {
            "deals": [
                {
                    "golf_deal_id": 101,
                    "tournament_name": "The Open Championship",
                    "tour_code": "pga",
                    "course_name": "Royal Portrush",
                    "selection_name": "Player A",
                    "market_code": "TOURNAMENT_WINNER",
                    "model_pct": 30.0,
                    "market_odd": 5.0,
                    "bookmaker_name": "Bet365",
                    "position_count": 2,
                    "total_stake": 15.0,
                    "position_ids": [11, 12],
                }
            ],
            "matchup_deals": [],
            "golf_positions": [
                {
                    "position_id": 1,
                    "selection_label": "Player A",
                    "market_code": "TOURNAMENT_WINNER",
                    "taken_odd": 9.0,
                    "stake_amount": 5.0,
                    "taken_at": "2026-07-11 10:00:00",
                    "is_matchup": False,
                }
            ],
        },
        "balanced",
        100.0,
        "semaine",
        7,
    )
    golf_strategy_checks = [
        "Deja prises" in golf_strategy_html,
        "hero-visual golf" in golf_strategy_html,
        "Retour au board" not in golf_strategy_html,
        "Back (performance)" not in golf_strategy_html,
        "Positions deja prises" in golf_strategy_html,
        "PRIS x2" in golf_strategy_html,
        "Supprimer prise" in golf_strategy_html,
        "Ticket 2" in golf_strategy_html,
        "Supprimer" in golf_strategy_html,
        "Scope actif : 1 semaine (7 jours)" in golf_strategy_html,
        "Vision scope : Selectif" in golf_strategy_html,
        "Parametres reels : edge min" in golf_strategy_html,
        "name='periode'" in golf_strategy_html,
        "periode=semaine" in golf_strategy_html,
    ]
    if not all(golf_strategy_checks):
        print("Dashboard golf strategy render smoke test failed.")
        return 7

    golf_strategy_dedup_html = render_golf_strategy_page(
        {
            "deals": [],
            "matchup_deals": [
                {
                    "golf_matchup_deal_id": 875,
                    "tournament_name": "3M Open",
                    "tour_code": "pga",
                    "course_name": "TPC Twin Cities",
                    "market_code": "ROUND_MATCHUP",
                    "pick_name": "Corey Conners",
                    "opp_name": "Johnny Keefer",
                    "model_pct": 54.9,
                    "edge_pct": 4.9,
                    "market_odd": 2.0,
                    "bookmaker_name": "bet365",
                    "position_count": 0,
                    "total_stake": None,
                    "position_ids": [],
                },
                {
                    "golf_matchup_deal_id": 875,
                    "tournament_name": "3M Open",
                    "tour_code": "pga",
                    "course_name": "TPC Twin Cities",
                    "market_code": "ROUND_MATCHUP",
                    "pick_name": "Corey Conners",
                    "opp_name": "Johnny Keefer",
                    "model_pct": 54.9,
                    "edge_pct": 4.9,
                    "market_odd": 2.0,
                    "bookmaker_name": "bet365",
                    "position_count": 0,
                    "total_stake": None,
                    "position_ids": [],
                },
            ],
            "golf_positions": [],
        },
        "equilibre",
        100.0,
        "mois",
        30,
    )
    if golf_strategy_dedup_html.count("Parier que Corey Conners bat Johnny Keefer sur ce tour") != 1:
        print("Dashboard golf strategy deduplication smoke test failed.")
        return 71

    saved_golf_positions = []
    original_save_golf_position = dashboard_app.save_golf_position
    dashboard_app.save_golf_position = lambda deal_type, deal_id, taken_odd, stake_amount, **_kwargs: saved_golf_positions.append(
        (deal_type, deal_id, taken_odd, stake_amount)
    )
    try:
        body = urlencode({
            "action": "golf_take_position",
            "golf_deal_type": "MATCHUP",
            "golf_deal_id": "202",
            "taken_odd": "1,76",
            "stake_amount": "2,45",
            "return_to": "/bankroll?sport=golf&golf_profile=balanced&bankroll=100&periode=mois",
        }).encode("utf-8")
        response_status = []
        response_headers = []

        def start_response(status: str, headers: list[tuple[str, str]]) -> None:
            response_status.append(status)
            response_headers.extend(headers)

        list(dashboard_app.application({
            "REQUEST_METHOD": "POST",
            "PATH_INFO": "/bankroll",
            "QUERY_STRING": "sport=golf&golf_profile=balanced&bankroll=100&periode=mois",
            "CONTENT_LENGTH": str(len(body)),
            "wsgi.input": BytesIO(body),
        }, start_response))
    finally:
        dashboard_app.save_golf_position = original_save_golf_position

    bankroll_post_checks = [
        response_status and response_status[0].startswith("303"),
        ("Location", "/bankroll?sport=golf&golf_profile=balanced&bankroll=100&periode=mois") in response_headers,
        saved_golf_positions == [("MATCHUP", 202, 1.76, 2.45)],
    ]
    if not all(bankroll_post_checks):
        print("Dashboard golf bankroll POST smoke test failed.")
        return 8

    golf_detail_html = render_golf_detail(
        {
            "tournament": {
                "golf_tournament_id": 1,
                "tournament_name": "The Open Championship",
                "tour_code": "pga",
                "course_name": "Royal Portrush",
                "commence_time": None,
            },
            "markets": {
                "TOURNAMENT_WINNER": [
                    {
                        "player_name": "Player A",
                        "country_code": "CA",
                        "market_code": "TOURNAMENT_WINNER",
                        "model_probability": 0.12,
                        "model_fair_odd": 8.33,
                        "best_odd": 9.0,
                        "bookmaker_name": "Bet365",
                        "sg_total": 1.2,
                        "sg_ott": 0.4,
                        "sg_app": 0.8,
                        "sg_arg": 0.1,
                        "sg_putt": -0.1,
                    }
                ]
            },
            "deals": [
                {
                    "selection_name": "Player A",
                    "market_code": "TOURNAMENT_WINNER",
                    "model_pct": 12.0,
                    "implied_pct": 10.0,
                    "edge_pct": 2.0,
                    "market_odd": 9.0,
                    "bookmaker_name": "Bet365",
                }
            ],
            "matchup_deals": [
                {
                    "market_code": "TOURNAMENT_MATCHUP",
                    "pick_name": "Player A",
                    "opp_name": "Player B",
                    "model_pct": 56.0,
                    "edge_pct": 4.0,
                    "market_odd": 2.1,
                    "bookmaker_name": "Bet365",
                }
            ],
            "matchups": [
                {
                    "market_code": "TOURNAMENT_MATCHUP",
                    "p1_name": "Player A",
                    "p2_name": "Player B",
                    "p1_probability": 0.56,
                    "p2_probability": 0.44,
                    "tie_probability": 0.0,
                    "deal_odd": 2.1,
                    "deal_edge": 0.04,
                    "deal_bookmaker_name": "Bet365",
                }
            ],
        }
    )
    golf_detail_checks = [
        "Analyse golf - The Open Championship" in golf_detail_html,
        "Pourquoi le modele aime ces joueurs" in golf_detail_html,
        "Paris recommandes" in golf_detail_html,
        "Analyse des confrontations" in golf_detail_html,
        "Marches couverts et limites" in golf_detail_html,
    ]
    if not all(golf_detail_checks):
        print("Dashboard golf detail render smoke test failed.")
        return 9

    original_check_database_ready = dashboard_app.check_database_ready
    original_current_golf_quality_snapshot = dashboard_app.current_golf_quality_snapshot
    dashboard_app.check_database_ready = lambda: (
        True,
        {
            "database": "up",
            "expected_migration": "0044_example.sql",
            "latest_applied_migration": "0044_example.sql",
            "migration_in_sync": True,
        },
    )
    dashboard_app.current_golf_quality_snapshot = lambda *args, **kwargs: {
        "status": "ok",
        "summary": "Qualite golf OK.",
        "tournaments_in_scope": 3,
        "active_deals_invalid": 0,
    }
    try:
        live_status, _live_headers, live_body = call_wsgi_app({
            "REQUEST_METHOD": "GET",
            "PATH_INFO": "/health/live",
            "QUERY_STRING": "",
            "CONTENT_LENGTH": "0",
            "wsgi.input": BytesIO(b""),
        })
        ready_status, _ready_headers, ready_body = call_wsgi_app({
            "REQUEST_METHOD": "GET",
            "PATH_INFO": "/health/ready",
            "QUERY_STRING": "",
            "CONTENT_LENGTH": "0",
            "wsgi.input": BytesIO(b""),
        })
        data_status, _data_headers, data_body = call_wsgi_app({
            "REQUEST_METHOD": "GET",
            "PATH_INFO": "/health/data",
            "QUERY_STRING": "",
            "CONTENT_LENGTH": "0",
            "wsgi.input": BytesIO(b""),
        })
        release_status, _release_headers, release_body = call_wsgi_app({
            "REQUEST_METHOD": "GET",
            "PATH_INFO": "/release",
            "QUERY_STRING": "",
            "CONTENT_LENGTH": "0",
            "wsgi.input": BytesIO(b""),
        })
    finally:
        dashboard_app.check_database_ready = original_check_database_ready
        dashboard_app.current_golf_quality_snapshot = original_current_golf_quality_snapshot

    live_payload = json.loads(live_body)
    ready_payload = json.loads(ready_body)
    data_payload = json.loads(data_body)
    release_payload = json.loads(release_body)
    health_checks = [
        live_status.startswith("200"),
        ready_status.startswith("200"),
        data_status.startswith("200"),
        release_status.startswith("200"),
        live_payload.get("status") == "live",
        ready_payload.get("status") == "ready",
        data_payload.get("status") == "ok",
        data_payload.get("golf_quality", {}).get("summary") == "Qualite golf OK.",
        ready_payload.get("migration_in_sync") is True,
        ready_payload.get("database") == "up",
        release_payload.get("app") == "bp-edge-dashboard",
        bool(release_payload.get("version")),
        "generated_at" in live_payload,
    ]
    if not all(health_checks):
        print("Dashboard health endpoints smoke test failed.")
        return 10

    original_consume_finished_action_report = dashboard_app.consume_finished_action_report
    original_peek_running_action_report = dashboard_app.peek_running_action_report
    dashboard_app.consume_finished_action_report = lambda: ActionReport(
        title="Predictions golf terminees",
        status="success",
        payload={"info": "51 deals crees.", "updated_at": "2026-07-20T11:05:00+00:00"},
    )
    dashboard_app.peek_running_action_report = lambda: None
    try:
        action_status, _action_headers, action_body = call_wsgi_app({
            "REQUEST_METHOD": "GET",
            "PATH_INFO": "/action-status",
            "QUERY_STRING": "",
            "CONTENT_LENGTH": "0",
            "wsgi.input": BytesIO(b""),
        })
    finally:
        dashboard_app.consume_finished_action_report = original_consume_finished_action_report
        dashboard_app.peek_running_action_report = original_peek_running_action_report

    action_payload = json.loads(action_body)
    action_status_checks = [
        action_status.startswith("200"),
        action_payload.get("status") == "ok",
        bool(action_payload.get("report")),
        action_payload.get("report", {}).get("status") == "success",
        "Predictions golf terminees" in str(action_payload.get("report", {}).get("html") or ""),
    ]
    if not all(action_status_checks):
        print("Dashboard action status endpoint smoke test failed.")
        return 10

    vercel_adapter_checks = [
        callable(vercel_app.app),
        vercel_app.application is vercel_app.app,
    ]
    if not all(vercel_adapter_checks):
        print("Vercel adapter smoke test failed.")
        return 11

    client_user = UserContext(42, "client@bp-edge.local", "Client Test", "CLIENT")
    original_auth_disabled = dashboard_app.AUTH_DISABLED
    original_load_bankroll_account = dashboard_app.load_bankroll_account
    original_current_user = dashboard_app.current_user_from_request
    original_set_user_bankroll = dashboard_app.set_user_bankroll
    original_validate_back_payload = dashboard_app.validate_back_payload
    dashboard_app.AUTH_DISABLED = False
    dashboard_app.load_bankroll_account = lambda _user: {
        "current_amount": 125.0,
        "open_stake": 25.0,
        "currency_code": "CAD",
        "events": [],
    }
    dashboard_app.current_user_from_request = lambda _environ: client_user
    bankroll_updates: list[tuple[int, float, str]] = []
    dashboard_app.set_user_bankroll = (
        lambda user, amount, reason="": bankroll_updates.append((user.user_id, amount, reason))
    )
    dashboard_app.validate_back_payload = lambda _sport: {"_summary_qs": "validated=1"}
    try:
        client_bankroll_account_html = dashboard_app.render_bankroll_account_page(client_user)
        client_back_html = render_back_page(
            {
                "stats": {
                    "total": 1,
                    "settled": 0,
                    "pending": 1,
                    "won": 0,
                    "accuracy_pct": None,
                    "profit_units": 0.0,
                    "roi_pct": None,
                    "taken_count": 1,
                    "taken_profit_units": 0.0,
                    "taken_roi_pct": None,
                },
                "rows": [
                    {
                        "bet_kind": "PARLAY",
                        "parlay_id": 1,
                        "fixture_id": None,
                        "outright_market_id": None,
                        "league_name": None,
                        "home_team_name": None,
                        "away_team_name": None,
                        "subject_label": "France HOME + Canada BTTS_YES",
                        "kickoff_utc": None,
                        "market_code": "PARLAY",
                        "selection_code": "2 jambes",
                        "model_probability": None,
                        "model_fair_odd": None,
                        "market_odd": 3.4,
                        "taken_odd": 3.4,
                        "stake_amount": 5.0,
                        "edge_probability": None,
                        "result_code": None,
                        "profit_units": None,
                        "taken_profit_units": None,
                        "settled_at": None,
                        "actual_outcome": None,
                        "position_count": 1,
                        "note": "",
                        "tickets": [],
                        "legs_summary": "France HOME + Canada BTTS_YES",
                    }
                ],
                "equity_series": [(0.0, 0.0)],
                "equity_count": 0,
                "league_options": [],
            },
            {"kind": "all", "market": "all", "status": "all", "league": "all", "taken": "all", "sort": "date_desc"},
            client_user,
        )

        bankroll_status, _bankroll_headers, bankroll_body = call_wsgi_app({
            "REQUEST_METHOD": "POST",
            "PATH_INFO": "/bankroll/account",
            "QUERY_STRING": "",
            "CONTENT_LENGTH": str(len(bankroll_form := urlencode({
                "action": "set_bankroll",
                "amount": "160",
                "reason": "deposit",
            }).encode("utf-8"))),
            "wsgi.input": BytesIO(bankroll_form),
        })
        validation_status, validation_headers, _validation_body = call_wsgi_app({
            "REQUEST_METHOD": "POST",
            "PATH_INFO": "/back",
            "QUERY_STRING": "",
            "CONTENT_LENGTH": str(len(validation_form := urlencode({
                "action": "validate_back",
                "sport": "football",
                "qs": "sport=football",
            }).encode("utf-8"))),
            "wsgi.input": BytesIO(validation_form),
        })
        blocked_status, blocked_headers, _blocked_body = call_wsgi_app({
            "REQUEST_METHOD": "POST",
            "PATH_INFO": "/bankroll",
            "QUERY_STRING": "montant=100&periode=mois&profil=balanced",
            "CONTENT_LENGTH": str(len(blocked_form := urlencode({
                "action": "take_position",
                "key": "DEAL|f10|HANDICAP|HOME",
                "selection_label": "Domicile handicap",
                "line": "-1.5",
                "taken_odd": "2.14",
                "stake_amount": "3.5",
                "return_to": "/bankroll?montant=100&periode=mois&profil=balanced",
            }).encode("utf-8"))),
            "wsgi.input": BytesIO(blocked_form),
        })
    finally:
        dashboard_app.AUTH_DISABLED = original_auth_disabled
        dashboard_app.load_bankroll_account = original_load_bankroll_account
        dashboard_app.current_user_from_request = original_current_user
        dashboard_app.set_user_bankroll = original_set_user_bankroll
        dashboard_app.validate_back_payload = original_validate_back_payload

    client_permission_checks = [
        "name=\"amount\"" in client_bankroll_account_html,
        "Enregistrer" in client_bankroll_account_html,
        "Mode lecture seule" in client_back_html,
        "Valider mes paris" in client_back_html,
        "Supprimer" not in client_back_html,
        bankroll_status.startswith("200"),
        bankroll_updates == [(42, 160.0, "deposit")],
        "Bankroll mise a jour." in bankroll_body,
        validation_status.startswith("303"),
        ("Location", "/back?sport=football&validated=1") in validation_headers,
        blocked_status.startswith("303"),
        any(
            header == "Location"
            and "action_error=Permission+requise%3A+POSITION_WRITE_OWN" in value
            for header, value in blocked_headers
        ),
    ]
    if not all(client_permission_checks):
        print("Dashboard client permissions smoke test failed.")
        return 12

    original_vercel = os.environ.get("VERCEL")
    os.environ["VERCEL"] = "1"
    try:
        vercel_controls_html = dashboard_app.render_controls(
            "FIFA World Cup",
            ["FIFA World Cup"],
            user=UserContext(1, "admin@bp-edge.local", "Admin", "ADMIN"),
        )
        vercel_golf_html = dashboard_app.render_golf_page(
            {
                "view": "predictions",
                "metrics": [],
                "selected_tournament": "all",
                "tournaments": [],
                "markets": {},
                "deals": [],
                "matchup_deals": [],
                "golf_positions": [],
            },
            user=UserContext(1, "admin@bp-edge.local", "Admin", "ADMIN"),
        )
    finally:
        if original_vercel is None:
            os.environ.pop("VERCEL", None)
        else:
            os.environ["VERCEL"] = original_vercel

    vercel_ui_checks = [
        "Mode Vercel" in vercel_controls_html,
        "Cycle complet V1" not in vercel_controls_html,
        "Mode Vercel" in vercel_golf_html,
        "Cycle golf complet" not in vercel_golf_html,
    ]
    if not all(vercel_ui_checks):
        print("Dashboard Vercel UI guard smoke test failed.")
        return 13

    if not _check_time_helpers():
        print("Time helpers smoke test failed.")
        return 14

    print("Dashboard render smoke test succeeded.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
