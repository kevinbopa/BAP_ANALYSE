"""
Extraction des donnees pour le rapport annuel.

Isole toutes les queries DB dans un seul module reutilisable par :
  - generate_rapport_annuel.py (generation Excel)
  - le hook validate_back qui declenche l'auto-update
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
import sys
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "services" / "prediction" / "src"))

from spe_prediction.db import DatabaseSettings, connect_db


# =============================================================================
# COUTS FIXES — reference statique (a maintenir a la main quand un abo change)
# =============================================================================

# Format : (jour_du_mois_charge, service, categorie, montant_cad)
# Chaque service est preleve le meme jour chaque mois.
SUBSCRIPTIONS = [
    (2,  "TheSportsDB",              "Data — historique sport",  16.79),
    (9,  "DataGolf (Scratch Plus)",  "Data — modele golf",       44.19),
    (9,  "The Odds API",             "Data — cotes multi-books", 44.19),
    (9,  "API-Football",             "Data — lineups foot",      27.01),
    (10, "ChatGPT",                  "Outil dev / analyse",      28.73),
    (12, "Google Workspace",         "Communication / email",    31.03),
    (20, "Claude (Anthropic)",       "Outil dev / analyse",      32.19),
]


def couts_fixes_annuels(year: int) -> list[dict[str, Any]]:
    """Retourne une entree par (mois, service) pour l'annee — pour poser
    chaque prelevement a sa date exacte dans le tracker mensuel."""
    entries: list[dict[str, Any]] = []
    for month in range(1, 13):
        for day, service, categorie, montant in SUBSCRIPTIONS:
            # Cape le jour au dernier du mois (fevrier 30 -> 28, etc.)
            try:
                dt = date(year, month, day)
            except ValueError:
                dt = date(year, month, 28)
            entries.append({
                "date": dt, "service": service, "categorie": categorie,
                "montant": float(montant),
            })
    return entries


# =============================================================================
# PARIS PRIS — enrichis avec contexte + resultat
# =============================================================================

@dataclass
class BetRecord:
    position_id: int
    taken_at: datetime
    bet_kind: str          # DEAL / PRONOSTIC / OUTRIGHT / PARLAY / GOLF
    sport: str             # FOOT / GOLF
    market_code: str
    selection_label: str
    taken_odd: float
    stake_amount: float
    model_probability: float | None
    # Contexte
    label_match: str       # "Arsenal vs Chelsea" ou "Rocket Classic"
    league_or_tournament: str
    kickoff_or_start: datetime | None
    bookmaker: str | None
    # Resultat
    status: str            # "PENDING" / "WON" / "LOST" / "CASHOUT" / "VOID"
    settled_at: datetime | None
    payout: float          # montant retourne au compte (0 si perdu, mise+profit si gagne)
    profit: float          # gain net (payout - stake, sauf VOID = 0)
    cashout_amount: float | None
    cashed_out_at: datetime | None
    # CLV si calcule
    clv_pct: float | None
    closing_line_odd: float | None
    # Annotations utilisateur (voir migration 0048)
    user_note: str | None
    reviewed_at: datetime | None


def _sport_of(bet_kind: str, market_code: str) -> str:
    if bet_kind == "GOLF" or "GOLF" in (market_code or "").upper():
        return "GOLF"
    return "FOOT"


def _to_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _status_from_event(event_type: str | None, cashed_out_at: Any) -> str:
    if cashed_out_at is not None:
        return "CASHOUT"
    if event_type == "BET_WON":
        return "WON"
    if event_type == "BET_LOST":
        return "LOST"
    if event_type in ("BET_VOID", "STAKE_VOIDED"):
        return "VOID"
    return "PENDING"


def _label_for(row: dict[str, Any]) -> str:
    if row.get("mat_tournament"):
        return row["mat_tournament"]
    if row.get("out_tournament"):
        return row["out_tournament"]
    if row.get("home_team") and row.get("away_team"):
        return f"{row['home_team']} vs {row['away_team']}"
    return "-"


def _league_for(row: dict[str, Any]) -> str:
    return (
        row.get("league_name")
        or row.get("mat_tournament")
        or row.get("out_tournament")
        or "-"
    )


def _kickoff_for(row: dict[str, Any]) -> datetime | None:
    return (
        row.get("fixture_kickoff")
        or row.get("mat_date_start")
        or row.get("out_date_start")
    )


def fetch_all_bets(year: int, user_id: int = 1) -> list[BetRecord]:
    """Tous les paris pris pendant l'annee, enrichis avec resultat + contexte.

    Un seul query lateral aggrege le dernier evenement de reglement de chaque
    position (BET_WON/LOST/VOID) pour eviter le N+1.
    """
    connection = connect_db(DatabaseSettings.from_env())
    try:
        with connection.cursor() as cursor:
            cursor.execute("""
                SELECT p.position_id, p.bet_kind, p.market_code, p.selection_code,
                       p.selection_label, p.taken_odd, p.stake_amount,
                       p.taken_at, p.cashed_out_at, p.cashout_amount,
                       p.model_probability, p.fixture_id, p.golf_deal_id,
                       p.golf_matchup_deal_id, p.clv_pct, p.closing_line_odd,
                       p.user_note, p.reviewed_at,
                       -- foot
                       f.kickoff_utc AS fixture_kickoff,
                       l.league_name,
                       ht.team_name AS home_team, at.team_name AS away_team,
                       fb.bookmaker_name AS foot_bookmaker,
                       -- golf outright
                       gt_o.tournament_name AS out_tournament,
                       gt_o.date_start AS out_date_start,
                       gd_o.market_code AS gd_market,
                       gb_o.bookmaker_name AS out_bookmaker,
                       -- golf matchup
                       gt_m.tournament_name AS mat_tournament,
                       gt_m.date_start AS mat_date_start,
                       gd_m.market_code AS gm_market,
                       gb_m.bookmaker_name AS mat_bookmaker,
                       -- ledger : dernier reglement
                       e.event_type, e.amount_delta, e.created_at AS settled_at
                FROM model.user_bet_positions p
                LEFT JOIN core.fixtures f ON f.fixture_id = p.fixture_id
                LEFT JOIN core.leagues l ON l.league_id = f.league_id
                LEFT JOIN core.teams ht ON ht.team_id = f.home_team_id
                LEFT JOIN core.teams at ON at.team_id = f.away_team_id
                LEFT JOIN LATERAL (
                    SELECT b.bookmaker_name FROM model.value_bets vb
                    JOIN core.bookmakers b ON b.bookmaker_id = vb.bookmaker_id
                    WHERE vb.fixture_id = p.fixture_id
                      AND vb.market_code = p.market_code
                      AND vb.selection_code = p.selection_code
                    ORDER BY vb.detected_at DESC LIMIT 1
                ) fb ON true
                LEFT JOIN model.golf_deals gd_o ON gd_o.golf_deal_id = p.golf_deal_id
                LEFT JOIN core.golf_tournaments gt_o
                    ON gt_o.golf_tournament_id = gd_o.golf_tournament_id
                LEFT JOIN core.bookmakers gb_o
                    ON gb_o.bookmaker_id = gd_o.bookmaker_id
                LEFT JOIN model.golf_matchup_deals gd_m
                    ON gd_m.golf_matchup_deal_id = p.golf_matchup_deal_id
                LEFT JOIN core.golf_tournaments gt_m
                    ON gt_m.golf_tournament_id = gd_m.golf_tournament_id
                LEFT JOIN core.bookmakers gb_m
                    ON gb_m.bookmaker_id = gd_m.bookmaker_id
                LEFT JOIN LATERAL (
                    SELECT event_type, amount_delta, created_at
                    FROM model.user_bankroll_events e2
                    WHERE e2.position_id = p.position_id
                      AND e2.event_type IN ('BET_WON','BET_LOST','BET_VOID','STAKE_VOIDED')
                    ORDER BY e2.created_at DESC LIMIT 1
                ) e ON true
                WHERE p.deleted_at IS NULL
                  AND p.user_id = %(user_id)s
                  AND EXTRACT(YEAR FROM p.taken_at) = %(year)s
                ORDER BY p.taken_at ASC
            """, {"user_id": user_id, "year": year})
            cols = [d[0] for d in cursor.description]
            rows = [dict(zip(cols, r)) for r in cursor.fetchall()]
    finally:
        connection.close()

    records: list[BetRecord] = []
    for row in rows:
        stake = _to_float(row["stake_amount"]) or 0.0
        payout = 0.0
        profit = 0.0
        cashout_amount = _to_float(row.get("cashout_amount"))
        status = _status_from_event(row.get("event_type"), row.get("cashed_out_at"))

        if status == "CASHOUT":
            payout = cashout_amount or 0.0
            profit = payout - stake
        elif status == "WON":
            payout = _to_float(row.get("amount_delta")) or 0.0
            profit = payout - stake
        elif status == "LOST":
            payout = 0.0
            profit = -stake
        elif status == "VOID":
            payout = stake
            profit = 0.0
        # PENDING : payout/profit = 0 par defaut

        bookmaker = (
            row.get("foot_bookmaker")
            or row.get("mat_bookmaker")
            or row.get("out_bookmaker")
        )

        records.append(BetRecord(
            position_id=int(row["position_id"]),
            taken_at=row["taken_at"],
            bet_kind=str(row["bet_kind"] or ""),
            sport=_sport_of(row["bet_kind"], row["market_code"]),
            market_code=str(row["market_code"] or ""),
            selection_label=str(row.get("selection_label") or "-"),
            taken_odd=_to_float(row["taken_odd"]) or 0.0,
            stake_amount=stake,
            model_probability=_to_float(row.get("model_probability")),
            label_match=_label_for(row),
            league_or_tournament=_league_for(row),
            kickoff_or_start=_kickoff_for(row),
            bookmaker=bookmaker,
            status=status,
            settled_at=row.get("settled_at") or row.get("cashed_out_at"),
            payout=round(payout, 2),
            profit=round(profit, 2),
            cashout_amount=cashout_amount,
            cashed_out_at=row.get("cashed_out_at"),
            clv_pct=_to_float(row.get("clv_pct")),
            closing_line_odd=_to_float(row.get("closing_line_odd")),
            user_note=row.get("user_note"),
            reviewed_at=row.get("reviewed_at"),
        ))
    return records


# =============================================================================
# TRESORERIE — depots, retraits (source manuelle pour l'instant)
# =============================================================================

# Format : (date, montant, reference, canal)
# TODO(v2) : brancher sur historique bet365 via API si dispo.
DEPOTS_MANUELS: list[tuple[date, float, str, str]] = [
    (date(2026, 7, 1),  10.00, "D6123771934IP", "INTERAC"),
    (date(2026, 7, 4),  10.00, "D6128376520IP", "INTERAC"),
    (date(2026, 7, 8),  10.00, "D6134578502IP", "INTERAC"),
    (date(2026, 7, 10), 250.00, "D6138291434IP", "INTERAC"),
    (date(2026, 7, 11), 80.00, "D6138380475IP", "INTERAC"),
    (date(2026, 7, 13), 59.00, "D6142185970IP", "INTERAC"),
    (date(2026, 7, 15), 50.00, "D6144521821IP", "INTERAC"),
    (date(2026, 7, 18), 300.00, "D6149206434IP", "INTERAC"),
    (date(2026, 7, 20), 20.00, "D6151956097IP", "INTERAC"),
    (date(2026, 7, 20), 185.00, "D6151958512IP", "INTERAC"),
]

# statut : "Effectue" ou "Annule"
RETRAITS_MANUELS: list[tuple[date, float, str, str]] = [
    (date(2026, 7, 11), 30.00,  "W745839570IN", "Effectue"),
    (date(2026, 7, 11), 30.00,  "745839570",    "Annule"),
    (date(2026, 7, 13), 59.37,  "W746292820IN", "Effectue"),
    (date(2026, 7, 13), 70.00,  "W746385182IN", "Effectue"),
    (date(2026, 7, 18), 25.00,  "W747250409IN", "Effectue"),
    (date(2026, 7, 18), 450.00, "W747272005IN", "Effectue"),
]


# =============================================================================
# AGREGATS
# =============================================================================

def monthly_summary(records: list[BetRecord], year: int) -> list[dict[str, Any]]:
    """Recap par mois : nb paris, mise, gain, perte, cashout, P&L, ROI."""
    rows = []
    for month in range(1, 13):
        month_bets = [r for r in records if r.taken_at.year == year and r.taken_at.month == month]
        n_total = len(month_bets)
        n_won = sum(1 for r in month_bets if r.status == "WON")
        n_lost = sum(1 for r in month_bets if r.status == "LOST")
        n_cashout = sum(1 for r in month_bets if r.status == "CASHOUT")
        n_void = sum(1 for r in month_bets if r.status == "VOID")
        n_pending = sum(1 for r in month_bets if r.status == "PENDING")

        stake_total = sum(r.stake_amount for r in month_bets)
        settled = [r for r in month_bets if r.status in ("WON", "LOST", "CASHOUT", "VOID")]
        stake_settled = sum(r.stake_amount for r in settled)
        profit_total = sum(r.profit for r in month_bets)
        roi = (profit_total / stake_settled * 100.0) if stake_settled > 0 else None

        # Split foot / golf
        stake_foot = sum(r.stake_amount for r in month_bets if r.sport == "FOOT")
        profit_foot = sum(r.profit for r in month_bets if r.sport == "FOOT")
        stake_golf = sum(r.stake_amount for r in month_bets if r.sport == "GOLF")
        profit_golf = sum(r.profit for r in month_bets if r.sport == "GOLF")

        # Couts fixes du mois
        costs_month = sum(
            e["montant"] for e in couts_fixes_annuels(year)
            if e["date"].month == month
        )

        rows.append({
            "month": month, "year": year,
            "n_total": n_total, "n_won": n_won, "n_lost": n_lost,
            "n_cashout": n_cashout, "n_void": n_void, "n_pending": n_pending,
            "stake_total": stake_total, "stake_settled": stake_settled,
            "profit_total": profit_total, "roi": roi,
            "stake_foot": stake_foot, "profit_foot": profit_foot,
            "stake_golf": stake_golf, "profit_golf": profit_golf,
            "costs_fixes": costs_month,
            "pnl_net_business": profit_total - costs_month,
        })
    return rows


MONTHS_FR = [
    "", "Janvier", "Fevrier", "Mars", "Avril", "Mai", "Juin",
    "Juillet", "Aout", "Septembre", "Octobre", "Novembre", "Decembre",
]


if __name__ == "__main__":
    # Smoke test
    records = fetch_all_bets(2026, user_id=2)
    print(f"Paris recuperes : {len(records)}")
    if records:
        r = records[-1]
        print(f"Dernier : {r.taken_at} | {r.sport} | {r.selection_label} "
              f"@ {r.taken_odd} mise {r.stake_amount}$ status={r.status}")
    summary = monthly_summary(records, 2026)
    for row in summary:
        if row["n_total"] > 0:
            print(f"  {MONTHS_FR[row['month']]:10} : {row['n_total']} paris, "
                  f"P&L {row['profit_total']:+.2f}$, "
                  f"couts {row['costs_fixes']:.2f}$, "
                  f"net {row['pnl_net_business']:+.2f}$")
