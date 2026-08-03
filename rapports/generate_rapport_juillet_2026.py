"""
Rapport financier juillet 2026 — BPREDICTION.

Genere un fichier Excel complet avec :
  - Executive summary (dashboard)
  - Couts fixes (subscriptions)
  - Activite compte bet365 (depots, retraits, mises, P&L)
  - Bilan mensuel + graphiques
  - Projection break-even

Toutes les donnees sont figees ici pour tracabilite historique.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

from openpyxl import Workbook
from openpyxl.chart import BarChart, LineChart, PieChart, Reference
from openpyxl.chart.label import DataLabelList
from openpyxl.styles import (
    Alignment, Border, Font, PatternFill, Side,
)
from openpyxl.utils import get_column_letter


# =============================================================================
# DONNEES SOURCE — juillet 2026
# =============================================================================

COUTS_FIXES_JUILLET = [
    # (date, service, categorie, montant_cad)
    (date(2026, 7, 2),  "TheSportsDB",              "Data — historique sport",  16.79),
    (date(2026, 7, 9),  "DataGolf (Scratch Plus)",  "Data — modele golf",       44.19),
    (date(2026, 7, 9),  "The Odds API",             "Data — cotes multi-books", 44.19),
    (date(2026, 7, 9),  "API-Football",             "Data — lineups foot",      27.01),
    (date(2026, 7, 10), "ChatGPT",                  "Outil dev / analyse",      28.73),
    (date(2026, 7, 12), "Google Workspace",         "Communication / email",    31.03),
    (date(2026, 7, 20), "Claude (Anthropic)",       "Outil dev / analyse",      32.19),
]

DEPOTS_JUILLET = [
    # (date, heure, reference, montant, canal)
    (date(2026, 7, 1),  "22:49", "D6123771934IP", 10.00,  "INTERAC"),
    (date(2026, 7, 4),  "15:13", "D6128376520IP", 10.00,  "INTERAC"),
    (date(2026, 7, 8),  "13:23", "D6134578502IP", 10.00,  "INTERAC"),
    (date(2026, 7, 10), "20:43", "D6138291434IP", 250.00, "INTERAC"),
    (date(2026, 7, 11), "00:44", "D6138380475IP", 80.00,  "INTERAC"),
    (date(2026, 7, 13), "18:20", "D6142185970IP", 59.00,  "INTERAC"),
    (date(2026, 7, 15), "09:40", "D6144521821IP", 50.00,  "INTERAC"),
    (date(2026, 7, 18), "17:28", "D6149206434IP", 300.00, "INTERAC"),
    (date(2026, 7, 20), "07:43", "D6151956097IP", 20.00,  "INTERAC"),
    (date(2026, 7, 20), "07:48", "D6151958512IP", 185.00, "INTERAC"),
]

RETRAITS_JUILLET = [
    # (date, heure, reference, montant, statut)
    (date(2026, 7, 11), "12:08", "W745839570IN",  30.00,  "Effectue"),
    (date(2026, 7, 11), "12:16", "745839570",     30.00,  "Annule"),  # Ne compte pas
    (date(2026, 7, 13), "08:12", "W746292820IN",  59.37,  "Effectue"),
    (date(2026, 7, 13), "19:37", "W746385182IN",  70.00,  "Effectue"),
    (date(2026, 7, 18), "16:33", "W747250409IN",  25.00,  "Effectue"),
    (date(2026, 7, 18), "17:55", "W747272005IN",  450.00, "Effectue"),
]

# Recap 12 mois bet365 (page "Mon activite")
BILAN_12M_BET365 = {
    "gains_pertes": -109.31,      # Total gains - total mises
    "depots_nets": 369.63,        # Depots - retraits
    "total_depots": 974.00,
    "total_retraits": 604.37,
    "montant_mise": 3908.00,      # Volume betted
    "duree_jouee_heures": 19.0,
}

# Detail activite trading pour le 28/07 (jour observe)
ACTIVITE_28_JUILLET = {
    "gains": 185.38,
    "mises_solde_retirable": 160.41,
    "gains_pertes_jour": 24.97,
    "en_cours_debut": 62.55,
    "en_cours_fin": 29.41,
}


# =============================================================================
# STYLES REUTILISABLES
# =============================================================================

FONT_TITLE = Font(name="Calibri", size=18, bold=True, color="0E1B2E")
FONT_SUBTITLE = Font(name="Calibri", size=12, bold=True, color="0E1B2E")
FONT_HEADER = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
FONT_TOTAL = Font(name="Calibri", size=12, bold=True)
FONT_BODY = Font(name="Calibri", size=11)
FONT_MUTED = Font(name="Calibri", size=10, italic=True, color="6B7280")

FILL_HEADER = PatternFill("solid", fgColor="0E1B2E")   # marine BPREDICTION
FILL_ACCENT = PatternFill("solid", fgColor="14B8A6")   # teal accent
FILL_ZEBRA = PatternFill("solid", fgColor="F5F7FA")
FILL_ALERT = PatternFill("solid", fgColor="FEE2E2")    # rouge doux
FILL_SUCCESS = PatternFill("solid", fgColor="D1FAE5")  # vert doux
FILL_INFO = PatternFill("solid", fgColor="DBEAFE")     # bleu doux
FILL_GOLD = PatternFill("solid", fgColor="FEF3C7")     # or (KPI)

CENTER = Alignment(horizontal="center", vertical="center")
RIGHT = Alignment(horizontal="right", vertical="center")
LEFT = Alignment(horizontal="left", vertical="center")
WRAP = Alignment(horizontal="left", vertical="top", wrap_text=True)

_thin = Side(style="thin", color="D1D5DB")
BORDER = Border(top=_thin, bottom=_thin, left=_thin, right=_thin)

FMT_CAD = "#,##0.00 \"$\";[Red]-#,##0.00 \"$\""
FMT_CAD_ACCENT = "[Green]+#,##0.00 \"$\";[Red]-#,##0.00 \"$\""
FMT_INT = "#,##0"
FMT_PCT = "0.0%"
FMT_DATE = "yyyy-mm-dd"


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


def kpi_block(ws, start_row, cells):
    """cells = list of (label, value, format, fill) — rendu blocs KPI."""
    for i, (label, value, fmt, fill) in enumerate(cells):
        col = 1 + i * 2
        # Label
        ws.cell(row=start_row, column=col, value=label).font = FONT_MUTED
        ws.merge_cells(start_row=start_row, start_column=col,
                       end_row=start_row, end_column=col + 1)
        # Value
        vcell = ws.cell(row=start_row + 1, column=col, value=value)
        vcell.font = Font(name="Calibri", size=18, bold=True)
        vcell.fill = fill
        if fmt:
            vcell.number_format = fmt
        vcell.alignment = CENTER
        ws.merge_cells(start_row=start_row + 1, start_column=col,
                       end_row=start_row + 1, end_column=col + 1)


# =============================================================================
# SHEET 1 — DASHBOARD
# =============================================================================

def build_dashboard(wb: Workbook) -> None:
    ws = wb.create_sheet("Dashboard", 0)
    ws.sheet_view.showGridLines = False

    # Totaux calcules
    total_couts = sum(row[3] for row in COUTS_FIXES_JUILLET)
    total_depots = sum(r[3] for r in DEPOTS_JUILLET)
    total_retraits = sum(r[3] for r in RETRAITS_JUILLET if r[4] == "Effectue")
    depots_nets = total_depots - total_retraits
    # Trading P&L : approximation
    # On sait qu'en 12 mois : gains-mises = -109.31, mises = 3908
    # L'activite du 28/07 = +24.97
    # On repartit : le P&L trading juillet ~= -109 (car graphe plat avant juillet)
    pnl_trading_juillet_estime = BILAN_12M_BET365["gains_pertes"]

    # Compte cash : depots - retraits + P&L trading = solde en compte
    # Solde bankroll = depots_nets + P&L trading
    solde_bankroll_estime = depots_nets + pnl_trading_juillet_estime

    # Vraie perte reelle en tant qu'entreprise :
    # Couts fixes payes = 224.13$
    # P&L trading = -109.31$
    # Total : depense reelle = -333.44$
    perte_reelle = pnl_trading_juillet_estime - total_couts

    # --- Titre
    ws.merge_cells("A1:H1")
    ws["A1"] = "BPREDICTION — RAPPORT MENSUEL JUILLET 2026"
    ws["A1"].font = FONT_TITLE
    ws["A1"].alignment = CENTER
    ws["A1"].fill = FILL_ACCENT
    ws.row_dimensions[1].height = 36

    ws.merge_cells("A2:H2")
    ws["A2"] = f"Genere le {date.today().isoformat()} · Devise CAD · Source : bet365 + subscriptions"
    ws["A2"].font = FONT_MUTED
    ws["A2"].alignment = CENTER

    # --- KPI principaux
    ws.merge_cells("A4:H4")
    ws["A4"] = "Indicateurs cle juillet 2026"
    ws["A4"].font = FONT_SUBTITLE
    ws["A4"].fill = FILL_ZEBRA

    kpi_block(ws, 5, [
        ("Couts fixes payes",    total_couts,        FMT_CAD, FILL_ALERT),
        ("Depots (bet365)",      total_depots,       FMT_CAD, FILL_INFO),
        ("Retraits (bet365)",    total_retraits,     FMT_CAD, FILL_INFO),
        ("Depots NETS",          depots_nets,        FMT_CAD, FILL_SUCCESS),
    ])

    kpi_block(ws, 8, [
        ("Volume paris (12m)",   BILAN_12M_BET365["montant_mise"],    FMT_CAD, FILL_INFO),
        ("P&L trading (12m)",    pnl_trading_juillet_estime,          FMT_CAD_ACCENT, FILL_ALERT),
        ("Duree jouee (h)",      BILAN_12M_BET365["duree_jouee_heures"], "0.0", FILL_ZEBRA),
        ("Nb transactions",      len(DEPOTS_JUILLET) + len([r for r in RETRAITS_JUILLET if r[4]=="Effectue"]),  FMT_INT, FILL_ZEBRA),
    ])

    # --- Bilan reel
    ws.merge_cells("A12:H12")
    ws["A12"] = "Bilan REEL entreprise juillet 2026"
    ws["A12"].font = FONT_SUBTITLE
    ws["A12"].fill = FILL_ZEBRA

    kpi_block(ws, 13, [
        ("Couts fixes",           -total_couts,               FMT_CAD_ACCENT, FILL_ALERT),
        ("P&L trading",           pnl_trading_juillet_estime, FMT_CAD_ACCENT, FILL_ALERT),
        ("Perte NETTE mois",      perte_reelle,               FMT_CAD_ACCENT, FILL_ALERT),
        ("Cash reste au compte",  solde_bankroll_estime,      FMT_CAD, FILL_INFO),
    ])

    # --- Analyse rapide (verdict)
    ws.merge_cells("A17:H17")
    ws["A17"] = "Verdict"
    ws["A17"].font = FONT_SUBTITLE
    ws["A17"].fill = FILL_ZEBRA

    verdict_lines = [
        f"• PERTE MENSUELLE de {abs(perte_reelle):.2f} $ = couts subscriptions ({total_couts:.2f} $) + perte trading ({abs(pnl_trading_juillet_estime):.2f} $).",
        f"• Volume mise/depots = {BILAN_12M_BET365['montant_mise']/total_depots:.1f}x — bon signe de discipline (compounding).",
        f"• Taux de perte trading = {abs(pnl_trading_juillet_estime)/BILAN_12M_BET365['montant_mise']*100:.2f}% sur volume — proche du 'juice' bookmaker (marge).",
        f"• Sans reduction de couts ou augmentation bankroll, perte annualisee estimee : {perte_reelle*12:.0f} $/an.",
        "• Action prioritaire : couper ChatGPT (-28.73 $/mois) + verifier TheSportsDB free tier.",
    ]
    for i, line in enumerate(verdict_lines):
        cell = ws.cell(row=18 + i, column=1, value=line)
        cell.font = FONT_BODY
        cell.alignment = WRAP
        ws.merge_cells(start_row=18 + i, start_column=1, end_row=18 + i, end_column=8)
        ws.row_dimensions[18 + i].height = 22

    set_col_widths(ws, [22] * 8)


# =============================================================================
# SHEET 2 — COUTS FIXES
# =============================================================================

def build_couts_fixes(wb: Workbook) -> None:
    ws = wb.create_sheet("Couts fixes")
    ws.sheet_view.showGridLines = False

    ws.merge_cells("A1:E1")
    ws["A1"] = "Subscriptions & couts fixes juillet 2026"
    ws["A1"].font = FONT_TITLE
    ws["A1"].alignment = CENTER
    ws["A1"].fill = FILL_ACCENT
    ws.row_dimensions[1].height = 32

    headers = ["Date", "Service", "Categorie", "Montant CAD", "Impact annualise"]
    apply_header(ws, 3, headers)

    total = 0.0
    for i, (dt, service, categorie, montant) in enumerate(COUTS_FIXES_JUILLET, start=4):
        ws.cell(row=i, column=1, value=dt).number_format = FMT_DATE
        ws.cell(row=i, column=2, value=service).font = FONT_BODY
        ws.cell(row=i, column=3, value=categorie).font = FONT_MUTED
        c = ws.cell(row=i, column=4, value=montant)
        c.number_format = FMT_CAD
        c.font = FONT_BODY
        c_an = ws.cell(row=i, column=5, value=montant * 12)
        c_an.number_format = FMT_CAD
        c_an.font = FONT_MUTED
        for col in range(1, 6):
            ws.cell(row=i, column=col).border = BORDER
            if i % 2 == 0:
                ws.cell(row=i, column=col).fill = FILL_ZEBRA
        total += montant

    # Total row
    total_row = len(COUTS_FIXES_JUILLET) + 4
    ws.cell(row=total_row, column=1, value="TOTAL")
    ws.merge_cells(start_row=total_row, start_column=1,
                   end_row=total_row, end_column=3)
    ws.cell(row=total_row, column=1).font = FONT_TOTAL
    ws.cell(row=total_row, column=1).alignment = RIGHT
    ws.cell(row=total_row, column=1).fill = FILL_GOLD
    t = ws.cell(row=total_row, column=4, value=total)
    t.number_format = FMT_CAD
    t.font = FONT_TOTAL
    t.fill = FILL_GOLD
    ta = ws.cell(row=total_row, column=5, value=total * 12)
    ta.number_format = FMT_CAD
    ta.font = FONT_TOTAL
    ta.fill = FILL_GOLD

    # --- Graphique repartition (donut)
    ws.cell(row=total_row + 3, column=1, value="Repartition par service").font = FONT_SUBTITLE

    # data pour pie
    pie_start_row = total_row + 5
    ws.cell(row=pie_start_row, column=1, value="Service").font = FONT_HEADER
    ws.cell(row=pie_start_row, column=1).fill = FILL_HEADER
    ws.cell(row=pie_start_row, column=2, value="Montant").font = FONT_HEADER
    ws.cell(row=pie_start_row, column=2).fill = FILL_HEADER
    for i, (_, service, _, montant) in enumerate(COUTS_FIXES_JUILLET, start=pie_start_row + 1):
        ws.cell(row=i, column=1, value=service)
        c = ws.cell(row=i, column=2, value=montant)
        c.number_format = FMT_CAD

    pie = PieChart()
    labels = Reference(ws, min_col=1,
                       min_row=pie_start_row + 1,
                       max_row=pie_start_row + len(COUTS_FIXES_JUILLET))
    data = Reference(ws, min_col=2,
                     min_row=pie_start_row,
                     max_row=pie_start_row + len(COUTS_FIXES_JUILLET))
    pie.add_data(data, titles_from_data=True)
    pie.set_categories(labels)
    pie.title = "Repartition couts fixes juillet 2026"
    pie.height = 12
    pie.width = 18
    pie.dataLabels = DataLabelList(showPercent=True)
    ws.add_chart(pie, "D" + str(pie_start_row))

    set_col_widths(ws, [14, 30, 28, 16, 20])


# =============================================================================
# SHEET 3 — DEPOTS & RETRAITS
# =============================================================================

def build_depots_retraits(wb: Workbook) -> None:
    ws = wb.create_sheet("Depots & retraits")
    ws.sheet_view.showGridLines = False

    ws.merge_cells("A1:F1")
    ws["A1"] = "Depots et retraits bet365 — juillet 2026"
    ws["A1"].font = FONT_TITLE
    ws["A1"].alignment = CENTER
    ws["A1"].fill = FILL_ACCENT
    ws.row_dimensions[1].height = 32

    # --- Depots
    ws.cell(row=3, column=1, value="DEPOTS").font = FONT_SUBTITLE
    ws.cell(row=3, column=1).fill = FILL_SUCCESS
    ws.merge_cells("A3:F3")

    apply_header(ws, 4, ["Date", "Heure", "Reference", "Canal", "Montant CAD", "Cumul"])
    total_dep = 0.0
    for i, (dt, hr, ref, montant, canal) in enumerate(DEPOTS_JUILLET, start=5):
        total_dep += montant
        ws.cell(row=i, column=1, value=dt).number_format = FMT_DATE
        ws.cell(row=i, column=2, value=hr)
        ws.cell(row=i, column=3, value=ref).font = FONT_MUTED
        ws.cell(row=i, column=4, value=canal)
        c = ws.cell(row=i, column=5, value=montant)
        c.number_format = FMT_CAD
        cu = ws.cell(row=i, column=6, value=total_dep)
        cu.number_format = FMT_CAD
        cu.font = FONT_MUTED
        for col in range(1, 7):
            ws.cell(row=i, column=col).border = BORDER
            if i % 2 == 0:
                ws.cell(row=i, column=col).fill = FILL_ZEBRA

    dep_total_row = len(DEPOTS_JUILLET) + 5
    ws.cell(row=dep_total_row, column=1, value="TOTAL DEPOTS")
    ws.merge_cells(start_row=dep_total_row, start_column=1,
                   end_row=dep_total_row, end_column=4)
    ws.cell(row=dep_total_row, column=1).font = FONT_TOTAL
    ws.cell(row=dep_total_row, column=1).alignment = RIGHT
    ws.cell(row=dep_total_row, column=1).fill = FILL_SUCCESS
    t = ws.cell(row=dep_total_row, column=5, value=total_dep)
    t.number_format = FMT_CAD
    t.font = FONT_TOTAL
    t.fill = FILL_SUCCESS

    # --- Retraits
    start_ret = dep_total_row + 3
    ws.cell(row=start_ret, column=1, value="RETRAITS").font = FONT_SUBTITLE
    ws.cell(row=start_ret, column=1).fill = FILL_INFO
    ws.merge_cells(start_row=start_ret, start_column=1,
                   end_row=start_ret, end_column=6)

    apply_header(ws, start_ret + 1,
                 ["Date", "Heure", "Reference", "Statut", "Montant CAD", "Cumul effectifs"])
    total_ret = 0.0
    for i, (dt, hr, ref, montant, statut) in enumerate(RETRAITS_JUILLET, start=start_ret + 2):
        if statut == "Effectue":
            total_ret += montant
        ws.cell(row=i, column=1, value=dt).number_format = FMT_DATE
        ws.cell(row=i, column=2, value=hr)
        ws.cell(row=i, column=3, value=ref).font = FONT_MUTED
        ws.cell(row=i, column=4, value=statut)
        c = ws.cell(row=i, column=5, value=montant)
        c.number_format = FMT_CAD
        if statut == "Annule":
            for col in range(1, 7):
                ws.cell(row=i, column=col).font = Font(italic=True, color="9CA3AF", strike=True)
        cu = ws.cell(row=i, column=6, value=total_ret if statut == "Effectue" else "-")
        if statut == "Effectue":
            cu.number_format = FMT_CAD
        cu.font = FONT_MUTED
        for col in range(1, 7):
            ws.cell(row=i, column=col).border = BORDER
            if i % 2 == 0:
                ws.cell(row=i, column=col).fill = FILL_ZEBRA

    ret_total_row = start_ret + 1 + len(RETRAITS_JUILLET) + 1
    ws.cell(row=ret_total_row, column=1, value="TOTAL RETRAITS EFFECTUES")
    ws.merge_cells(start_row=ret_total_row, start_column=1,
                   end_row=ret_total_row, end_column=4)
    ws.cell(row=ret_total_row, column=1).font = FONT_TOTAL
    ws.cell(row=ret_total_row, column=1).alignment = RIGHT
    ws.cell(row=ret_total_row, column=1).fill = FILL_INFO
    t = ws.cell(row=ret_total_row, column=5, value=total_ret)
    t.number_format = FMT_CAD
    t.font = FONT_TOTAL
    t.fill = FILL_INFO

    # Net
    net = total_dep - total_ret
    net_row = ret_total_row + 2
    ws.cell(row=net_row, column=1, value="DEPOTS NETS (depots - retraits)")
    ws.merge_cells(start_row=net_row, start_column=1,
                   end_row=net_row, end_column=4)
    ws.cell(row=net_row, column=1).font = FONT_TOTAL
    ws.cell(row=net_row, column=1).alignment = RIGHT
    ws.cell(row=net_row, column=1).fill = FILL_GOLD
    n = ws.cell(row=net_row, column=5, value=net)
    n.number_format = FMT_CAD_ACCENT
    n.font = FONT_TOTAL
    n.fill = FILL_GOLD

    set_col_widths(ws, [14, 10, 20, 14, 16, 18])


# =============================================================================
# SHEET 4 — BILAN & GRAPHIQUES
# =============================================================================

def build_bilan_graphique(wb: Workbook) -> None:
    ws = wb.create_sheet("Bilan mensuel")
    ws.sheet_view.showGridLines = False

    ws.merge_cells("A1:F1")
    ws["A1"] = "Bilan mensuel — juillet 2026"
    ws["A1"].font = FONT_TITLE
    ws["A1"].alignment = CENTER
    ws["A1"].fill = FILL_ACCENT
    ws.row_dimensions[1].height = 32

    # Table synthese
    total_couts = sum(row[3] for row in COUTS_FIXES_JUILLET)
    total_dep = sum(r[3] for r in DEPOTS_JUILLET)
    total_ret = sum(r[3] for r in RETRAITS_JUILLET if r[4] == "Effectue")
    depots_nets = total_dep - total_ret
    pnl_trading = BILAN_12M_BET365["gains_pertes"]

    lignes = [
        ("Depots bet365",       total_dep,      "info",   "Argent injecte dans la bankroll"),
        ("Retraits bet365",     -total_ret,     "info",   "Argent recupere du compte"),
        ("Depots NETS",         depots_nets,    "success", "Injection nette de la periode"),
        ("P&L trading",         pnl_trading,    "alert",  "Gains - mises sur 12 mois (surtout juillet)"),
        ("Couts subscriptions", -total_couts,   "alert",  "Data + infra + outils AI"),
        ("PERTE NETTE mois",    pnl_trading - total_couts, "alert", "P&L trading - couts fixes"),
    ]

    apply_header(ws, 3, ["Poste", "Montant CAD", "Signal", "Commentaire"])
    for i, (label, val, signal, comment) in enumerate(lignes, start=4):
        ws.cell(row=i, column=1, value=label).font = FONT_BODY
        c = ws.cell(row=i, column=2, value=val)
        c.number_format = FMT_CAD_ACCENT
        c.font = FONT_TOTAL if "NET" in label or "PERTE" in label else FONT_BODY
        # Signal color
        signal_cell = ws.cell(row=i, column=3, value="")
        if signal == "success":
            signal_cell.fill = FILL_SUCCESS
        elif signal == "alert":
            signal_cell.fill = FILL_ALERT
        elif signal == "info":
            signal_cell.fill = FILL_INFO
        ws.cell(row=i, column=4, value=comment).font = FONT_MUTED
        ws.cell(row=i, column=4).alignment = WRAP
        for col in range(1, 5):
            ws.cell(row=i, column=col).border = BORDER
        if "PERTE" in label:
            for col in range(1, 5):
                ws.cell(row=i, column=col).fill = FILL_GOLD
                ws.cell(row=i, column=col).font = FONT_TOTAL

    # --- Graphique cumul par date (depots vs retraits)
    ws.cell(row=13, column=1, value="Flux journalier depots vs retraits").font = FONT_SUBTITLE

    # Preparer donnees agregees par date
    from collections import defaultdict
    daily = defaultdict(lambda: {"depot": 0.0, "retrait": 0.0})
    for dt, _, _, m, _ in DEPOTS_JUILLET:
        daily[dt]["depot"] += m
    for dt, _, _, m, s in RETRAITS_JUILLET:
        if s == "Effectue":
            daily[dt]["retrait"] += m
    dates_sorted = sorted(daily.keys())

    chart_data_row = 15
    ws.cell(row=chart_data_row, column=1, value="Date").font = FONT_HEADER
    ws.cell(row=chart_data_row, column=1).fill = FILL_HEADER
    ws.cell(row=chart_data_row, column=2, value="Depots").font = FONT_HEADER
    ws.cell(row=chart_data_row, column=2).fill = FILL_HEADER
    ws.cell(row=chart_data_row, column=3, value="Retraits").font = FONT_HEADER
    ws.cell(row=chart_data_row, column=3).fill = FILL_HEADER

    for i, dt in enumerate(dates_sorted, start=chart_data_row + 1):
        ws.cell(row=i, column=1, value=dt).number_format = FMT_DATE
        c1 = ws.cell(row=i, column=2, value=daily[dt]["depot"])
        c1.number_format = FMT_CAD
        c2 = ws.cell(row=i, column=3, value=daily[dt]["retrait"])
        c2.number_format = FMT_CAD

    bar = BarChart()
    bar.type = "col"
    bar.style = 12
    bar.title = "Depots vs retraits par jour (juillet 2026)"
    bar.y_axis.title = "Montant CAD"
    bar.x_axis.title = "Date"
    data = Reference(ws, min_col=2, min_row=chart_data_row,
                     max_col=3, max_row=chart_data_row + len(dates_sorted))
    cats = Reference(ws, min_col=1,
                     min_row=chart_data_row + 1,
                     max_row=chart_data_row + len(dates_sorted))
    bar.add_data(data, titles_from_data=True)
    bar.set_categories(cats)
    bar.height = 12
    bar.width = 22
    ws.add_chart(bar, "E" + str(chart_data_row))

    # --- Solde cumule apres chaque transaction (line chart)
    solde_start = chart_data_row + len(dates_sorted) + 4
    ws.cell(row=solde_start, column=1, value="Solde cumule (approx.)").font = FONT_SUBTITLE

    events = []  # (date, delta, type)
    for dt, _, _, m, _ in DEPOTS_JUILLET:
        events.append((dt, m, "Depot"))
    for dt, _, _, m, s in RETRAITS_JUILLET:
        if s == "Effectue":
            events.append((dt, -m, "Retrait"))
    events.sort()

    ws.cell(row=solde_start + 2, column=1, value="Date").font = FONT_HEADER
    ws.cell(row=solde_start + 2, column=1).fill = FILL_HEADER
    ws.cell(row=solde_start + 2, column=2, value="Solde cumule").font = FONT_HEADER
    ws.cell(row=solde_start + 2, column=2).fill = FILL_HEADER

    solde = 0.0
    for i, (dt, delta, _) in enumerate(events, start=solde_start + 3):
        solde += delta
        ws.cell(row=i, column=1, value=dt).number_format = FMT_DATE
        c = ws.cell(row=i, column=2, value=solde)
        c.number_format = FMT_CAD

    line = LineChart()
    line.title = "Solde cumule des flux (juillet 2026)"
    line.style = 12
    line.y_axis.title = "Solde cumule CAD"
    line.x_axis.title = "Date"
    data_line = Reference(ws, min_col=2, min_row=solde_start + 2,
                          max_row=solde_start + 2 + len(events))
    cats_line = Reference(ws, min_col=1,
                          min_row=solde_start + 3,
                          max_row=solde_start + 2 + len(events))
    line.add_data(data_line, titles_from_data=True)
    line.set_categories(cats_line)
    line.height = 10
    line.width = 22
    ws.add_chart(line, "E" + str(solde_start + 2))

    set_col_widths(ws, [24, 18, 12, 40])


# =============================================================================
# SHEET 5 — PROJECTION & BREAK-EVEN
# =============================================================================

def build_projection(wb: Workbook) -> None:
    ws = wb.create_sheet("Projection break-even")
    ws.sheet_view.showGridLines = False

    ws.merge_cells("A1:F1")
    ws["A1"] = "Projection break-even & scenarios 12 mois"
    ws["A1"].font = FONT_TITLE
    ws["A1"].alignment = CENTER
    ws["A1"].fill = FILL_ACCENT
    ws.row_dimensions[1].height = 32

    # --- Tableau ROI requis
    total_couts = sum(row[3] for row in COUTS_FIXES_JUILLET)
    ws.cell(row=3, column=1, value=f"Cout fixe mensuel : {total_couts:.2f} $ CAD").font = FONT_SUBTITLE

    ws.cell(row=5, column=1, value="Break-even : ROI mensuel requis par taille de bankroll").font = FONT_SUBTITLE

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
        # colorier selon verdict
        v_cell = ws.cell(row=i, column=3, value=verdict)
        if "Irrealiste" in verdict or "difficile" in verdict:
            v_cell.fill = FILL_ALERT
        elif "Atteignable" in verdict:
            v_cell.fill = FILL_GOLD
        else:
            v_cell.fill = FILL_SUCCESS
        for col in range(1, 4):
            ws.cell(row=i, column=col).border = BORDER

    # --- Scenarios 12 mois (avec/sans injection)
    ws.cell(row=15, column=1,
            value="Scenarios 12 mois — bankroll depart 2 000 $").font = FONT_SUBTITLE

    apply_header(ws, 16, ["Mois", "Sans injection", "Injection 500$/mois", "Injection + coupes"])

    # Scenario A : sans injection, ROI 4%/mois, couts pleins
    # Scenario B : injection 500$/mois, ROI 4%/mois, couts pleins
    # Scenario C : injection + coupes (ChatGPT -28.73 = 195.40$)
    bk_a = bk_b = bk_c = 2000
    for m in range(1, 13):
        # A : sans rien
        bk_a = bk_a * 1.04 - total_couts
        # B : injection 500
        bk_b = bk_b * 1.04 - total_couts + 500
        # C : injection + coupes
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
        # highlight negative
        if bk_a < 0:
            ws.cell(row=row, column=2).fill = FILL_ALERT
        if bk_b < 0:
            ws.cell(row=row, column=3).fill = FILL_ALERT

    # Graphique projection
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

def main() -> None:
    wb = Workbook()
    default = wb.active
    wb.remove(default)

    build_dashboard(wb)
    build_couts_fixes(wb)
    build_depots_retraits(wb)
    build_bilan_graphique(wb)
    build_projection(wb)

    output_path = Path(__file__).parent / "rapport_juillet_2026.xlsx"
    wb.save(output_path)
    print(f"OK : {output_path}")


if __name__ == "__main__":
    main()
