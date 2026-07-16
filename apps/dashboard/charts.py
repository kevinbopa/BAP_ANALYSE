"""Courbes SVG server-rendered — la couche "terminal de trading" du dashboard.

Discipline du systeme Swiss : fond transparent, filets 1px, numeraux
tabulaires, series en noir/gris, rouge #E4002B reserve aux marqueurs de
signal. Zero dependance JS — chaque courbe est un <svg> inline.
"""
from __future__ import annotations

from datetime import datetime
from html import escape
import math
from typing import Sequence

INK = "#111114"
MUTED = "#9A9AA0"
LIGHT = "#C9C9CE"
RULE = "rgba(17,17,20,0.16)"
RED = "#E4002B"


def _scale(values: Sequence[float], out_min: float, out_max: float) -> tuple[float, float]:
    """(min, max) du domaine avec un petit padding — évite les courbes plates
    collées aux bords."""
    lo, hi = min(values), max(values)
    if hi - lo < 1e-9:
        lo -= 1.0
        hi += 1.0
    pad = (hi - lo) * 0.08
    return lo - pad, hi + pad


def svg_line_chart(
    series: Sequence[tuple[str, str, Sequence[tuple[float, float]]]],
    width: int = 900,
    height: int = 240,
    y_format: str = "{:.0f}",
    x_labels: tuple[str, str] | None = None,
) -> str:
    """Courbe multi-series.

    ``series``: liste de (label, couleur, [(x, y), ...]) — x numerique
    (timestamp epoch), points tries par x croissant.
    """
    all_points = [p for _label, _color, pts in series for p in pts]
    if len(all_points) < 2:
        return "<p class='empty'>Pas encore assez de points pour tracer une courbe.</p>"

    pad_l, pad_r, pad_t, pad_b = 54, 14, 12, 24
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b

    xs = [p[0] for p in all_points]
    ys = [p[1] for p in all_points]
    x_lo, x_hi = min(xs), max(xs)
    if x_hi - x_lo < 1e-9:
        x_hi = x_lo + 1.0
    y_lo, y_hi = _scale(ys, 0, 1)

    def px(x: float) -> float:
        return pad_l + (x - x_lo) / (x_hi - x_lo) * plot_w

    def py(y: float) -> float:
        return pad_t + (1.0 - (y - y_lo) / (y_hi - y_lo)) * plot_h

    parts: list[str] = [
        f"<svg viewBox='0 0 {width} {height}' width='100%' role='img' "
        f"style='display:block' xmlns='http://www.w3.org/2000/svg'>"
    ]

    # Grille horizontale (4 filets) + etiquettes de valeur
    for i in range(4):
        y_val = y_lo + (y_hi - y_lo) * i / 3
        y_pix = py(y_val)
        parts.append(
            f"<line x1='{pad_l}' y1='{y_pix:.1f}' x2='{width - pad_r}' y2='{y_pix:.1f}' "
            f"stroke='{RULE}' stroke-width='1'/>"
        )
        parts.append(
            f"<text x='{pad_l - 8}' y='{y_pix + 3.5:.1f}' text-anchor='end' "
            f"font-size='11' fill='{MUTED}' font-family='Helvetica Neue, Helvetica, Arial, sans-serif'>"
            f"{escape(y_format.format(y_val))}</text>"
        )

    # Series
    for label, color, pts in series:
        if len(pts) < 2:
            continue
        coords = " ".join(f"{px(x):.1f},{py(y):.1f}" for x, y in pts)
        parts.append(
            f"<polyline points='{coords}' fill='none' stroke='{color}' "
            f"stroke-width='1.8' stroke-linejoin='round'/>"
        )
        # Marqueur + valeur du dernier point (le "prix courant")
        last_x, last_y = pts[-1]
        parts.append(
            f"<circle cx='{px(last_x):.1f}' cy='{py(last_y):.1f}' r='3' fill='{color}'/>"
        )

    # Etiquettes temporelles debut/fin
    if x_labels:
        parts.append(
            f"<text x='{pad_l}' y='{height - 6}' font-size='11' fill='{MUTED}' "
            f"font-family='Helvetica Neue, Helvetica, Arial, sans-serif'>{escape(x_labels[0])}</text>"
        )
        parts.append(
            f"<text x='{width - pad_r}' y='{height - 6}' text-anchor='end' font-size='11' fill='{MUTED}' "
            f"font-family='Helvetica Neue, Helvetica, Arial, sans-serif'>{escape(x_labels[1])}</text>"
        )

    parts.append("</svg>")

    legend = "".join(
        f"<span class='chart-key'><span class='chart-swatch' style='background:{color}'></span>"
        f"{escape(label)} <strong>{y_format.format(pts[-1][1])}</strong></span>"
        for label, color, pts in series if pts
    )
    return f"<div class='chart'>{''.join(parts)}<div class='chart-legend'>{legend}</div></div>"


def svg_sparkline(values: Sequence[float], width: int = 120, height: int = 28, color: str = INK) -> str:
    """Mini-courbe sans axes (forme recente, tendances)."""
    if len(values) < 2:
        return ""
    y_lo, y_hi = _scale(values, 0, 1)
    step = width / (len(values) - 1)
    coords = " ".join(
        f"{i * step:.1f},{(1.0 - (v - y_lo) / (y_hi - y_lo)) * (height - 4) + 2:.1f}"
        for i, v in enumerate(values)
    )
    last_y = (1.0 - (values[-1] - y_lo) / (y_hi - y_lo)) * (height - 4) + 2
    return (
        f"<svg viewBox='0 0 {width} {height}' width='{width}' height='{height}' "
        f"xmlns='http://www.w3.org/2000/svg'>"
        f"<polyline points='{coords}' fill='none' stroke='{color}' stroke-width='1.5'/>"
        f"<circle cx='{width - 0.5:.1f}' cy='{last_y:.1f}' r='2.2' fill='{RED}'/>"
        "</svg>"
    )


def cumulative_series(points: Sequence[tuple[datetime, float]]) -> list[tuple[float, float]]:
    """(timestamp, delta) -> courbe cumulative (equity curve)."""
    total = 0.0
    out: list[tuple[float, float]] = []
    for ts, delta in points:
        total += delta
        out.append((ts.timestamp(), total))
    return out
