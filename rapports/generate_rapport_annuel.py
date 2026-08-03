"""
Rapport annuel BPREDICTION.

Genere un fichier Excel avec :
  - Dashboard annuel (12 mois d'un coup)
  - Onglet DETAIL par mois : chaque pari trace individuellement
  - Couts fixes annualises
  - Cash flow (depots/retraits)
  - Projection break-even
  - Un onglet PARIS tous_paris.log au complet

Usage :
    py rapports/generate_rapport_annuel.py [--year 2026] [--user-id 2]
    py rapports/generate_rapport_annuel.py --output /path/custom.xlsx

Automation :
    Le hook validate_back appelle main(year=..., user_id=...) apres chaque
    validation manuelle des paris. Le fichier ecrase la version precedente,
    l'utilisateur retrouve toujours ses donnees a jour.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime
from pathlib import Path
import sys

from openpyxl import Workbook
from openpyxl.chart import BarChart, LineChart, PieChart, Reference
from openpyxl.chart.label import DataLabelList
from openpyxl.styles import (
    Alignment, Border, Font, PatternFill, Side,
)
from openpyxl.utils import get_column_letter

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

from report_data import (
    BetRecord, DEPOTS_MANUELS, RETRAITS_MANUELS, MONTHS_FR,
    SUBSCRIPTIONS, couts_fixes_annuels, fetch_all_bets, monthly_summary,
)


# =============================================================================
# STYLES
# =============================================================================
FONT_TITLE = Font(name="Calibri", size=18, bold=True, color="0E1B2E")
FONT_SUBTITLE = Font(name="Calibri", size=12, bold=True, color="0E1B2E")
FONT_HEADER = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
FONT_TOTAL = Font(name="Calibri", size=12, bold=True)
FONT_BODY = Font(name="Calibri", size=11)
FONT_MUTED = Font(name="Calibri", size=10, italic=True, color="6B7280")
FONT_SMALL = Font(name="Calibri", size=9)

FILL_HEADER = PatternFill("solid", fgColor="0E1B2E")
FILL_ACCENT = PatternFill("solid", fgColor="14B8A6")
FILL_ZEBRA = PatternFill("solid", fgColor="F5F7FA")
FILL_ALERT = PatternFill("solid", fgColor="FEE2E2")
FILL_SUCCESS = PatternFill("solid", fgColor="D1FAE5")
FILL_INFO = PatternFill("solid", fgColor="DBEAFE")
FILL_GOLD = PatternFill("solid", fgColor="FEF3C7")
FILL_PENDING = PatternFill("solid", fgColor="E5E7EB")

CENTER = Alignment(horizontal="center", vertical="center")
RIGHT = Alignment(horizontal="right", vertical="center")
LEFT = Alignment(horizontal="left", vertical="center")
WRAP = Alignment(horizontal="left", vertical="top", wrap_text=True)

_thin = Side(style="thin", color="D1D5DB")
BORDER = Border(top=_thin, bottom=_thin, left=_thin, right=_thin)

FMT_CAD = "#,##0.00 \"$\";[Red]-#,##0.00 \"$\""
FMT_CAD_ACCENT = "[Green]+#,##0.00 \"$\";[Red]-#,##0.00 \"$\""
FMT_PCT = "0.0%"
FMT_PCT_ACCENT = "[Green]+0.00%;[Red]-0.00%"
FMT_INT = "#,##0"
FMT_DATE = "yyyy-mm-dd"
FMT_DATETIME = "yyyy-mm-dd hh:mm"


# =============================================================================
# HELPERS
# =============================================================================

def apply_header(ws, row, headers, fill=FILL_HEADER, font=FONT_HEADER):
    for col, value in enumerate(headers, start=1):
        cell = ws.cell(row=row, column=col, value=value)
        cell.font = font
        cell.fill = fill
        cell.alignment = CENTER
        cell.border = BORDER


def set_col_widths(ws, widths):
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w


def _naive(dt):
    """Excel n'accepte pas les datetimes tz-aware. Naivise en gardant l'heure
    de Quebec (America/Toronto = UTC-4/-5), le plus lisible pour l'utilisateur."""
    if dt is None:
        return None
    if isinstance(dt, datetime) and dt.tzinfo is not None:
        from zoneinfo import ZoneInfo
        return dt.astimezone(ZoneInfo("America/Toronto")).replace(tzinfo=None)
    return dt


def status_fill(status: str) -> PatternFill:
    return {
        "WON": FILL_SUCCESS,
        "LOST": FILL_ALERT,
        "CASHOUT": FILL_INFO,
        "VOID": FILL_ZEBRA,
        "PENDING": FILL_PENDING,
    }.get(status, FILL_ZEBRA)


def kpi_block(ws, start_row, cells):
    for i, (label, value, fmt, fill) in enumerate(cells):
        col = 1 + i * 2
        ws.cell(row=start_row, column=col, value=label).font = FONT_MUTED
        ws.merge_cells(start_row=start_row, start_column=col,
                       end_row=start_row, end_column=col + 1)
        vcell = ws.cell(row=start_row + 1, column=col, value=value)
        vcell.font = Font(name="Calibri", size=18, bold=True)
        vcell.fill = fill
        if fmt:
            vcell.number_format = fmt
        vcell.alignment = CENTER
        ws.merge_cells(start_row=start_row + 1, start_column=col,
                       end_row=start_row + 1, end_column=col + 1)


# =============================================================================
# SHEET 1 — DASHBOARD ANNUEL
# =============================================================================

def build_dashboard(wb: Workbook, year: int, records: list[BetRecord],
                    summaries: list[dict]) -> None:
    ws = wb.create_sheet("Dashboard", 0)
    ws.sheet_view.showGridLines = False

    # --- Titre
    ws.merge_cells("A1:H1")
    ws["A1"] = f"BPREDICTION — RAPPORT ANNUEL {year}"
    ws["A1"].font = FONT_TITLE
    ws["A1"].alignment = CENTER
    ws["A1"].fill = FILL_ACCENT
    ws.row_dimensions[1].height = 36

    ws.merge_cells("A2:H2")
    ws["A2"] = f"Mis a jour le {datetime.now().strftime('%Y-%m-%d %H:%M')} · Devise CAD · Auto-genere apres validation des paris"
    ws["A2"].font = FONT_MUTED
    ws["A2"].alignment = CENTER

    # Totaux annuels
    n_total = len(records)
    n_settled = sum(1 for r in records if r.status != "PENDING")
    n_won = sum(1 for r in records if r.status == "WON")
    stake_total = sum(r.stake_amount for r in records)
    profit_total = sum(r.profit for r in records)
    stake_settled = sum(r.stake_amount for r in records if r.status in ("WON", "LOST", "CASHOUT", "VOID"))
    roi = (profit_total / stake_settled * 100) if stake_settled > 0 else 0
    costs_year = sum(s["costs_fixes"] for s in summaries)
    net_business = profit_total - costs_year

    # --- KPI ligne 1 : activite
    ws.merge_cells("A4:H4")
    ws["A4"] = "Activite paris annuelle"
    ws["A4"].font = FONT_SUBTITLE
    ws["A4"].fill = FILL_ZEBRA

    kpi_block(ws, 5, [
        ("Nb paris pris",       n_total,      FMT_INT,  FILL_INFO),
        ("Nb regles",           n_settled,    FMT_INT,  FILL_INFO),
        ("Nb gagnes",           n_won,        FMT_INT,  FILL_SUCCESS),
        ("Volume mise",         stake_total,  FMT_CAD,  FILL_INFO),
    ])

    # --- KPI ligne 2 : performance
    ws.merge_cells("A8:H8")
    ws["A8"] = "Performance trading"
    ws["A8"].font = FONT_SUBTITLE
    ws["A8"].fill = FILL_ZEBRA

    kpi_block(ws, 9, [
        ("P&L trading",         profit_total,   FMT_CAD_ACCENT,
         FILL_SUCCESS if profit_total >= 0 else FILL_ALERT),
        ("ROI reel",            roi / 100,      FMT_PCT_ACCENT,
         FILL_SUCCESS if roi >= 0 else FILL_ALERT),
        ("Taux victoire",       (n_won / n_settled) if n_settled else 0, FMT_PCT, FILL_GOLD),
        ("Mise moy/pari",       (stake_total / n_total) if n_total else 0, FMT_CAD, FILL_ZEBRA),
    ])

    # --- KPI ligne 3 : bilan business
    ws.merge_cells("A12:H12")
    ws["A12"] = "Bilan business (trading - couts fixes)"
    ws["A12"].font = FONT_SUBTITLE
    ws["A12"].fill = FILL_ZEBRA

    kpi_block(ws, 13, [
        ("Couts fixes annuel",  -costs_year,    FMT_CAD_ACCENT, FILL_ALERT),
        ("P&L trading annuel",  profit_total,   FMT_CAD_ACCENT,
         FILL_SUCCESS if profit_total >= 0 else FILL_ALERT),
        ("NET business",        net_business,   FMT_CAD_ACCENT,
         FILL_SUCCESS if net_business >= 0 else FILL_ALERT),
        ("Break-even manque",   max(0, -net_business), FMT_CAD, FILL_GOLD),
    ])

    # --- Tableau 12 mois
    ws.merge_cells("A17:M17")
    ws["A17"] = "Recap par mois"
    ws["A17"].font = FONT_SUBTITLE
    ws["A17"].fill = FILL_ZEBRA

    headers = [
        "Mois", "Nb paris", "Gagnes", "Perdus", "Cashout", "En cours",
        "Mise CAD", "P&L trading", "ROI", "Couts fixes", "NET business",
        "Foot P&L", "Golf P&L",
    ]
    apply_header(ws, 18, headers)

    for i, s in enumerate(summaries, start=19):
        ws.cell(row=i, column=1, value=MONTHS_FR[s["month"]])
        ws.cell(row=i, column=2, value=s["n_total"]).number_format = FMT_INT
        ws.cell(row=i, column=3, value=s["n_won"]).number_format = FMT_INT
        ws.cell(row=i, column=4, value=s["n_lost"]).number_format = FMT_INT
        ws.cell(row=i, column=5, value=s["n_cashout"]).number_format = FMT_INT
        ws.cell(row=i, column=6, value=s["n_pending"]).number_format = FMT_INT
        ws.cell(row=i, column=7, value=s["stake_total"]).number_format = FMT_CAD
        c_pnl = ws.cell(row=i, column=8, value=s["profit_total"])
        c_pnl.number_format = FMT_CAD_ACCENT
        c_roi = ws.cell(row=i, column=9,
                        value=(s["roi"] / 100) if s["roi"] is not None else None)
        if c_roi.value is not None:
            c_roi.number_format = FMT_PCT_ACCENT
        ws.cell(row=i, column=10, value=s["costs_fixes"]).number_format = FMT_CAD
        c_net = ws.cell(row=i, column=11, value=s["pnl_net_business"])
        c_net.number_format = FMT_CAD_ACCENT
        if s["pnl_net_business"] < 0:
            c_net.fill = FILL_ALERT
        elif s["pnl_net_business"] > 0:
            c_net.fill = FILL_SUCCESS
        ws.cell(row=i, column=12, value=s["profit_foot"]).number_format = FMT_CAD_ACCENT
        ws.cell(row=i, column=13, value=s["profit_golf"]).number_format = FMT_CAD_ACCENT
        for col in range(1, 14):
            ws.cell(row=i, column=col).border = BORDER
            if i % 2 == 0:
                ws.cell(row=i, column=col).fill = FILL_ZEBRA

    # Ligne total
    total_row = 19 + 12
    ws.cell(row=total_row, column=1, value="TOTAL ANNEE").font = FONT_TOTAL
    ws.cell(row=total_row, column=1).fill = FILL_GOLD
    ws.cell(row=total_row, column=2, value=n_total).number_format = FMT_INT
    ws.cell(row=total_row, column=3, value=n_won).number_format = FMT_INT
    ws.cell(row=total_row, column=4, value=sum(s["n_lost"] for s in summaries)).number_format = FMT_INT
    ws.cell(row=total_row, column=5, value=sum(s["n_cashout"] for s in summaries)).number_format = FMT_INT
    ws.cell(row=total_row, column=6, value=sum(s["n_pending"] for s in summaries)).number_format = FMT_INT
    ws.cell(row=total_row, column=7, value=stake_total).number_format = FMT_CAD
    ws.cell(row=total_row, column=8, value=profit_total).number_format = FMT_CAD_ACCENT
    ws.cell(row=total_row, column=9, value=roi / 100).number_format = FMT_PCT_ACCENT
    ws.cell(row=total_row, column=10, value=costs_year).number_format = FMT_CAD
    ws.cell(row=total_row, column=11, value=net_business).number_format = FMT_CAD_ACCENT
    for col in range(1, 14):
        ws.cell(row=total_row, column=col).font = FONT_TOTAL
        ws.cell(row=total_row, column=col).fill = FILL_GOLD

    # --- Graphique P&L par mois
    chart = BarChart()
    chart.type = "col"
    chart.title = f"P&L trading vs couts fixes par mois {year}"
    chart.style = 12
    chart.y_axis.title = "CAD"
    chart.x_axis.title = "Mois"
    data = Reference(ws, min_col=8, min_row=18, max_col=8, max_row=18 + 12)
    costs = Reference(ws, min_col=10, min_row=18, max_col=10, max_row=18 + 12)
    net = Reference(ws, min_col=11, min_row=18, max_col=11, max_row=18 + 12)
    chart.add_data(data, titles_from_data=True)
    chart.add_data(costs, titles_from_data=True)
    chart.add_data(net, titles_from_data=True)
    cats = Reference(ws, min_col=1, min_row=19, max_row=19 + 11)
    chart.set_categories(cats)
    chart.height = 12
    chart.width = 26
    ws.add_chart(chart, "A33")

    set_col_widths(ws, [12, 10, 9, 9, 9, 9, 12, 14, 10, 12, 14, 12, 12])


# =============================================================================
# SHEET 2 — TOUS LES PARIS (log complet, filtrable)
# =============================================================================

def build_all_bets(wb: Workbook, records: list[BetRecord]) -> None:
    ws = wb.create_sheet("Tous les paris")
    ws.sheet_view.showGridLines = False

    ws.merge_cells("A1:M1")
    ws["A1"] = f"Journal complet des paris — {len(records)} paris"
    ws["A1"].font = FONT_TITLE
    ws["A1"].alignment = CENTER
    ws["A1"].fill = FILL_ACCENT
    ws.row_dimensions[1].height = 32

    ws.merge_cells("A2:M2")
    ws["A2"] = "Filtre : clique sur la fleche de chaque colonne pour filtrer/trier"
    ws["A2"].font = FONT_MUTED
    ws["A2"].alignment = CENTER

    headers = [
        "Date/Heure", "Sport", "Match / Tournoi", "Ligue / Sport",
        "Marche", "Pari (selection)", "Cote", "Mise CAD", "Book",
        "Proba modele %", "Statut", "P&L CAD", "CLV %",
        "Note perso", "Revise le",
    ]
    apply_header(ws, 4, headers)

    for i, r in enumerate(records, start=5):
        ws.cell(row=i, column=1, value=_naive(r.taken_at)).number_format = FMT_DATETIME
        ws.cell(row=i, column=2, value=r.sport)
        ws.cell(row=i, column=3, value=r.label_match)
        ws.cell(row=i, column=4, value=r.league_or_tournament).font = FONT_MUTED
        ws.cell(row=i, column=5, value=r.market_code)
        ws.cell(row=i, column=6, value=r.selection_label)
        ws.cell(row=i, column=7, value=r.taken_odd).number_format = "0.00"
        ws.cell(row=i, column=8, value=r.stake_amount).number_format = FMT_CAD
        ws.cell(row=i, column=9, value=r.bookmaker or "-").font = FONT_MUTED
        c_p = ws.cell(row=i, column=10,
                      value=(r.model_probability * 100) if r.model_probability is not None else None)
        if c_p.value is not None:
            c_p.number_format = "0.0"
        status_cell = ws.cell(row=i, column=11, value=r.status)
        status_cell.fill = status_fill(r.status)
        status_cell.alignment = CENTER
        status_cell.font = Font(bold=True, size=10)
        c_pnl = ws.cell(row=i, column=12, value=r.profit if r.status != "PENDING" else None)
        if c_pnl.value is not None:
            c_pnl.number_format = FMT_CAD_ACCENT
        c_clv = ws.cell(row=i, column=13, value=(r.clv_pct / 100) if r.clv_pct is not None else None)
        if c_clv.value is not None:
            c_clv.number_format = FMT_PCT_ACCENT
        # Colonnes annotations (14, 15)
        note_cell = ws.cell(row=i, column=14, value=r.user_note or "")
        note_cell.alignment = WRAP
        rev_cell = ws.cell(row=i, column=15, value=_naive(r.reviewed_at))
        if r.reviewed_at:
            rev_cell.number_format = FMT_DATE
            rev_cell.fill = FILL_SUCCESS
        for col in range(1, 16):
            ws.cell(row=i, column=col).border = BORDER
            if i % 2 == 0 and col != 11:  # ne pas ecraser le fill du statut
                ws.cell(row=i, column=col).fill = FILL_ZEBRA

    # Active autofilter
    if records:
        ws.auto_filter.ref = f"A4:O{4 + len(records)}"
    ws.freeze_panes = "B5"

    set_col_widths(ws, [18, 7, 30, 22, 10, 30, 8, 12, 12, 12, 10, 12, 10, 40, 12])


# =============================================================================
# SHEET 3+ — UN ONGLET DETAIL PAR MOIS AVEC ACTIVITE
# =============================================================================

def build_month_sheet(wb: Workbook, year: int, month: int,
                      records: list[BetRecord],
                      summary: dict) -> None:
    """Onglet detail pour UN mois : KPI + tableau paris du mois + couts fixes."""
    month_records = [r for r in records if r.taken_at.year == year and r.taken_at.month == month]
    if not month_records and summary["n_total"] == 0 and summary["costs_fixes"] == 0:
        return  # skip les mois vides

    name = f"{MONTHS_FR[month]}"[:31]
    ws = wb.create_sheet(name)
    ws.sheet_view.showGridLines = False

    ws.merge_cells("A1:J1")
    ws["A1"] = f"{MONTHS_FR[month]} {year}"
    ws["A1"].font = FONT_TITLE
    ws["A1"].alignment = CENTER
    ws["A1"].fill = FILL_ACCENT
    ws.row_dimensions[1].height = 32

    # --- KPI du mois
    kpi_block(ws, 3, [
        ("Nb paris",         summary["n_total"],       FMT_INT, FILL_INFO),
        ("Volume mise",      summary["stake_total"],   FMT_CAD, FILL_INFO),
        ("P&L trading",      summary["profit_total"],  FMT_CAD_ACCENT,
         FILL_SUCCESS if summary["profit_total"] >= 0 else FILL_ALERT),
        ("ROI",              (summary["roi"] / 100) if summary["roi"] else 0, FMT_PCT_ACCENT,
         FILL_SUCCESS if (summary["roi"] or 0) >= 0 else FILL_ALERT),
    ])

    kpi_block(ws, 6, [
        ("Gagnes",           summary["n_won"],       FMT_INT, FILL_SUCCESS),
        ("Perdus",           summary["n_lost"],      FMT_INT, FILL_ALERT),
        ("Cashout",          summary["n_cashout"],   FMT_INT, FILL_INFO),
        ("En cours",         summary["n_pending"],   FMT_INT, FILL_PENDING),
    ])

    kpi_block(ws, 9, [
        ("P&L Foot",         summary["profit_foot"], FMT_CAD_ACCENT,
         FILL_SUCCESS if summary["profit_foot"] >= 0 else FILL_ALERT),
        ("P&L Golf",         summary["profit_golf"], FMT_CAD_ACCENT,
         FILL_SUCCESS if summary["profit_golf"] >= 0 else FILL_ALERT),
        ("Couts fixes",     -summary["costs_fixes"], FMT_CAD_ACCENT, FILL_ALERT),
        ("NET business",     summary["pnl_net_business"], FMT_CAD_ACCENT,
         FILL_SUCCESS if summary["pnl_net_business"] >= 0 else FILL_ALERT),
    ])

    # --- Tableau paris du mois
    ws.merge_cells("A13:M13")
    ws["A13"] = f"Paris pris en {MONTHS_FR[month]} ({len(month_records)} paris)"
    ws["A13"].font = FONT_SUBTITLE
    ws["A13"].fill = FILL_ZEBRA

    headers = [
        "Date/Heure", "Sport", "Match / Tournoi", "Marche",
        "Pari (selection)", "Cote", "Mise CAD", "Book",
        "Statut", "Paye CAD", "P&L CAD", "CLV %", "Cashout $",
        "Note perso", "Revise",
    ]
    apply_header(ws, 14, headers)

    for i, r in enumerate(month_records, start=15):
        ws.cell(row=i, column=1, value=_naive(r.taken_at)).number_format = FMT_DATETIME
        ws.cell(row=i, column=2, value=r.sport)
        ws.cell(row=i, column=3, value=r.label_match)
        ws.cell(row=i, column=4, value=r.market_code).font = FONT_MUTED
        ws.cell(row=i, column=5, value=r.selection_label)
        ws.cell(row=i, column=6, value=r.taken_odd).number_format = "0.00"
        ws.cell(row=i, column=7, value=r.stake_amount).number_format = FMT_CAD
        ws.cell(row=i, column=8, value=r.bookmaker or "-").font = FONT_MUTED
        st = ws.cell(row=i, column=9, value=r.status)
        st.fill = status_fill(r.status)
        st.alignment = CENTER
        st.font = Font(bold=True, size=10)
        ws.cell(row=i, column=10,
                value=r.payout if r.status != "PENDING" else None).number_format = FMT_CAD
        c_pnl = ws.cell(row=i, column=11,
                        value=r.profit if r.status != "PENDING" else None)
        if c_pnl.value is not None:
            c_pnl.number_format = FMT_CAD_ACCENT
        c_clv = ws.cell(row=i, column=12,
                        value=(r.clv_pct / 100) if r.clv_pct is not None else None)
        if c_clv.value is not None:
            c_clv.number_format = FMT_PCT_ACCENT
        ws.cell(row=i, column=13,
                value=r.cashout_amount if r.cashout_amount is not None else None).number_format = FMT_CAD
        note_cell = ws.cell(row=i, column=14, value=r.user_note or "")
        note_cell.alignment = WRAP
        rev_cell = ws.cell(row=i, column=15,
                           value="✓" if r.reviewed_at else "")
        rev_cell.alignment = CENTER
        if r.reviewed_at:
            rev_cell.fill = FILL_SUCCESS
            rev_cell.font = Font(bold=True, color="0E1B2E")
        for col in range(1, 16):
            ws.cell(row=i, column=col).border = BORDER
            if i % 2 == 0 and col != 9:
                ws.cell(row=i, column=col).fill = FILL_ZEBRA

    if month_records:
        ws.auto_filter.ref = f"A14:O{14 + len(month_records)}"
    ws.freeze_panes = "B15"

    # --- Couts fixes du mois
    costs_start = 15 + len(month_records) + 3
    ws.cell(row=costs_start, column=1,
            value=f"Couts fixes {MONTHS_FR[month]} {year}").font = FONT_SUBTITLE
    apply_header(ws, costs_start + 1,
                 ["Date", "Service", "Categorie", "Montant CAD"])

    month_costs = [e for e in couts_fixes_annuels(year) if e["date"].month == month]
    for i, e in enumerate(month_costs, start=costs_start + 2):
        ws.cell(row=i, column=1, value=e["date"]).number_format = FMT_DATE
        ws.cell(row=i, column=2, value=e["service"])
        ws.cell(row=i, column=3, value=e["categorie"]).font = FONT_MUTED
        ws.cell(row=i, column=4, value=e["montant"]).number_format = FMT_CAD
        for col in range(1, 5):
            ws.cell(row=i, column=col).border = BORDER

    ct_total_row = costs_start + 2 + len(month_costs)
    ws.cell(row=ct_total_row, column=1, value="TOTAL COUTS")
    ws.merge_cells(start_row=ct_total_row, start_column=1,
                   end_row=ct_total_row, end_column=3)
    ws.cell(row=ct_total_row, column=1).font = FONT_TOTAL
    ws.cell(row=ct_total_row, column=1).alignment = RIGHT
    ws.cell(row=ct_total_row, column=1).fill = FILL_GOLD
    t = ws.cell(row=ct_total_row, column=4, value=summary["costs_fixes"])
    t.number_format = FMT_CAD
    t.font = FONT_TOTAL
    t.fill = FILL_GOLD

    set_col_widths(ws, [18, 7, 30, 12, 30, 8, 12, 12, 10, 12, 12, 10, 12, 40, 10])


# =============================================================================
# SHEET N-1 — TRESORERIE (depots/retraits) - manuel juillet
# =============================================================================

def build_tresorerie(wb: Workbook, year: int) -> None:
    year_depots = [d for d in DEPOTS_MANUELS if d[0].year == year]
    year_retraits = [r for r in RETRAITS_MANUELS if r[0].year == year]
    if not year_depots and not year_retraits:
        return

    ws = wb.create_sheet("Tresorerie")
    ws.sheet_view.showGridLines = False

    ws.merge_cells("A1:F1")
    ws["A1"] = f"Tresorerie bet365 {year} (depots & retraits manuels)"
    ws["A1"].font = FONT_TITLE
    ws["A1"].alignment = CENTER
    ws["A1"].fill = FILL_ACCENT
    ws.row_dimensions[1].height = 32

    total_d = sum(d[1] for d in year_depots)
    total_r = sum(r[1] for r in year_retraits if r[3] == "Effectue")

    kpi_block(ws, 3, [
        ("Total depots",  total_d,           FMT_CAD, FILL_SUCCESS),
        ("Total retraits", total_r,          FMT_CAD, FILL_INFO),
        ("Depots NETS",   total_d - total_r, FMT_CAD_ACCENT, FILL_GOLD),
        ("Nb operations", len(year_depots) + len([r for r in year_retraits if r[3] == "Effectue"]),
         FMT_INT, FILL_ZEBRA),
    ])

    # Depots
    ws.cell(row=7, column=1, value="DEPOTS").font = FONT_SUBTITLE
    apply_header(ws, 8, ["Date", "Reference", "Canal", "Montant CAD"])
    for i, (dt, mnt, ref, canal) in enumerate(year_depots, start=9):
        ws.cell(row=i, column=1, value=dt).number_format = FMT_DATE
        ws.cell(row=i, column=2, value=ref).font = FONT_MUTED
        ws.cell(row=i, column=3, value=canal)
        ws.cell(row=i, column=4, value=mnt).number_format = FMT_CAD
        for col in range(1, 5):
            ws.cell(row=i, column=col).border = BORDER

    # Retraits
    retraits_start = 9 + len(year_depots) + 2
    ws.cell(row=retraits_start, column=1, value="RETRAITS").font = FONT_SUBTITLE
    apply_header(ws, retraits_start + 1, ["Date", "Reference", "Statut", "Montant CAD"])
    for i, (dt, mnt, ref, statut) in enumerate(year_retraits, start=retraits_start + 2):
        ws.cell(row=i, column=1, value=dt).number_format = FMT_DATE
        ws.cell(row=i, column=2, value=ref).font = FONT_MUTED
        ws.cell(row=i, column=3, value=statut)
        c = ws.cell(row=i, column=4, value=mnt)
        c.number_format = FMT_CAD
        if statut == "Annule":
            for col in range(1, 5):
                ws.cell(row=i, column=col).font = Font(italic=True, color="9CA3AF", strike=True)
        for col in range(1, 5):
            ws.cell(row=i, column=col).border = BORDER

    set_col_widths(ws, [14, 24, 14, 16, 14, 14])


# =============================================================================
# SHEET N — PROJECTION BREAK-EVEN (identique au precedent, chiffres annuels)
# =============================================================================

def build_projection(wb: Workbook, year: int, records: list[BetRecord],
                     summaries: list[dict]) -> None:
    ws = wb.create_sheet("Projection break-even")
    ws.sheet_view.showGridLines = False

    ws.merge_cells("A1:F1")
    ws["A1"] = "Projection break-even & scenarios 12 mois"
    ws["A1"].font = FONT_TITLE
    ws["A1"].alignment = CENTER
    ws["A1"].fill = FILL_ACCENT
    ws.row_dimensions[1].height = 32

    total_couts = sum(SUBSCRIPTIONS[i][3] for i in range(len(SUBSCRIPTIONS)))
    ws.cell(row=3, column=1,
            value=f"Cout fixe mensuel : {total_couts:.2f} $ CAD").font = FONT_SUBTITLE

    ws.cell(row=5, column=1,
            value="Break-even : ROI mensuel requis par taille de bankroll").font = FONT_SUBTITLE

    apply_header(ws, 6, ["Bankroll CAD", "ROI mensuel requis", "Verdict"])
    scenarios = [
        (2000,  "Irrealiste"),
        (3500,  "Tres difficile"),
        (5000,  "Atteignable si edge prouve"),
        (8000,  "Confortable pour un sharp"),
        (15000, "Facile"),
        (30000, "Marge confortable"),
    ]
    for i, (bk, verdict) in enumerate(scenarios, start=7):
        ws.cell(row=i, column=1, value=bk).number_format = FMT_CAD
        c = ws.cell(row=i, column=2, value=total_couts / bk)
        c.number_format = FMT_PCT
        v_cell = ws.cell(row=i, column=3, value=verdict)
        if "Irrealiste" in verdict or "difficile" in verdict:
            v_cell.fill = FILL_ALERT
        elif "Atteignable" in verdict:
            v_cell.fill = FILL_GOLD
        else:
            v_cell.fill = FILL_SUCCESS
        for col in range(1, 4):
            ws.cell(row=i, column=col).border = BORDER

    ws.cell(row=15, column=1,
            value="Scenarios 12 mois — bankroll depart 2 000 $").font = FONT_SUBTITLE
    apply_header(ws, 16, ["Mois", "Sans injection", "Injection 500$/mois",
                          "Injection + coupes"])

    bk_a = bk_b = bk_c = 2000
    for m in range(1, 13):
        bk_a = bk_a * 1.04 - total_couts
        bk_b = bk_b * 1.04 - total_couts + 500
        bk_c = bk_c * 1.04 - (total_couts - 28.73) + 500
        row = 16 + m
        ws.cell(row=row, column=1, value=f"M{m}")
        ws.cell(row=row, column=2, value=bk_a).number_format = FMT_CAD
        ws.cell(row=row, column=3, value=bk_b).number_format = FMT_CAD
        ws.cell(row=row, column=4, value=bk_c).number_format = FMT_CAD
        for col in range(1, 5):
            ws.cell(row=row, column=col).border = BORDER
            if row % 2 == 0:
                ws.cell(row=row, column=col).fill = FILL_ZEBRA
        if bk_a < 0:
            ws.cell(row=row, column=2).fill = FILL_ALERT
        if bk_b < 0:
            ws.cell(row=row, column=3).fill = FILL_ALERT

    line = LineChart()
    line.title = "Bankroll projetee sur 12 mois — 3 scenarios"
    line.style = 12
    line.y_axis.title = "Bankroll CAD"
    line.x_axis.title = "Mois"
    data = Reference(ws, min_col=2, min_row=16, max_col=4, max_row=28)
    cats = Reference(ws, min_col=1, min_row=17, max_row=28)
    line.add_data(data, titles_from_data=True)
    line.set_categories(cats)
    line.height = 12
    line.width = 24
    ws.add_chart(line, "F16")

    set_col_widths(ws, [18, 22, 22, 24])


# =============================================================================
# MAIN
# =============================================================================

def generate(year: int = 2026, user_id: int = 2, output: Path | None = None) -> Path:
    records = fetch_all_bets(year, user_id=user_id)
    summaries = monthly_summary(records, year)

    wb = Workbook()
    default = wb.active
    wb.remove(default)

    build_dashboard(wb, year, records, summaries)
    build_all_bets(wb, records)
    for month in range(1, 13):
        build_month_sheet(wb, year, month, records, summaries[month - 1])
    build_tresorerie(wb, year)
    build_projection(wb, year, records, summaries)

    if output is None:
        output = _HERE / f"rapport_annuel_{year}.xlsx"
    wb.save(output)
    return output


def _cli() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--year", type=int, default=2026)
    parser.add_argument("--user-id", type=int, default=2)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()
    path = generate(year=args.year, user_id=args.user_id, output=args.output)
    print(f"OK : {path}")


if __name__ == "__main__":
    _cli()
