"""Gestion de bankroll — du montant ideal par pari au plan de portefeuille.

Volet "mise ideale par pari" (anti-banqueroute) :
    Kelly fractionnaire calcule sur la probabilite CREDIBILISEE — la proba
    modele retrecie vers celle du marche proportionnellement a la credibilite
    de l'edge. Un edge enorme (donc suspect) mise automatiquement moins.
    Le Kelly plein est optimal en theorie mais suppose des probabilites
    exactes ; les notres portent une erreur de modele, d'ou la fraction et
    les plafonds. Miser en % de la bankroll COURANTE rend la ruine totale
    mathematiquement impossible ; les plafonds bornent les drawdowns.

Volet "plan de portefeuille" :
    Le client donne sa bankroll, une periode et un profil de risque. Le plan
    alloue les deals actifs (plafond d'exposition simultanee, repartition
    safe/modere/risque) et projette la periode par simulation Monte Carlo :
    les paris futurs sont generes au rythme reellement observe, avec le
    profil de deal typique du moment.
"""
from __future__ import annotations

from dataclasses import dataclass
import random
from statistics import median
from typing import Any, Sequence

from spe_prediction.decision import _edge_credibility


@dataclass(frozen=True)
class RiskProfile:
    code: str
    label: str
    kelly_multiplier: float   # fraction du Kelly plein
    stake_cap: float          # mise max par pari (% bankroll)
    exposure_cap: float       # somme des mises simultanees max (% bankroll)
    min_edge: float           # edge modele-marche minimum
    min_credibility: float    # confiance minimum dans l'edge
    min_expected_value: float # EV projetee minimum (p*cote - 1)
    max_positions: int        # nombre max de positions simultanees
    max_odd: float            # evite les cotes trop explosives selon profil
    concentration_power: float # >1 concentre, <1 diversifie
    source_weights: dict[str, float]
    category_caps: dict[str, float]
    parlay_enabled: bool
    parlay_max_tickets: int
    parlay_stake_cap: float
    description: str


RISK_PROFILES: dict[str, RiskProfile] = {
    "prudent": RiskProfile(
        code="prudent", label="Prudent",
        kelly_multiplier=0.10, stake_cap=0.015, exposure_cap=0.06,
        min_edge=0.030, min_credibility=0.45, min_expected_value=0.025,
        max_positions=6, max_odd=3.20, concentration_power=0.85,
        source_weights={"VALUE BET": 1.00, "PRONOSTIC SUR": 0.55, "LONG TERME": 0.20},
        category_caps={"SAFE": 0.055, "MODERE": 0.015, "RISQUE": 0.000},
        parlay_enabled=False, parlay_max_tickets=0, parlay_stake_cap=0.0,
        description="Defense du capital : peu de paris, edges verifies, cotes moderees, aucun combine.",
    ),
    "equilibre": RiskProfile(
        code="equilibre", label="Equilibre",
        kelly_multiplier=0.28, stake_cap=0.045, exposure_cap=0.18,
        min_edge=0.015, min_credibility=0.25, min_expected_value=0.010,
        max_positions=14, max_odd=5.50, concentration_power=1.00,
        source_weights={"VALUE BET": 1.00, "PRONOSTIC SUR": 0.75, "LONG TERME": 0.45},
        category_caps={"SAFE": 0.090, "MODERE": 0.070, "RISQUE": 0.025},
        parlay_enabled=True, parlay_max_tickets=2, parlay_stake_cap=0.0075,
        description="Croissance controlee : value bets solides, diversification, rares combines tres selectifs.",
    ),
    "agressif": RiskProfile(
        code="agressif", label="Agressif",
        kelly_multiplier=0.70, stake_cap=0.12, exposure_cap=0.45,
        min_edge=0.005, min_credibility=0.10, min_expected_value=0.0025,
        max_positions=24, max_odd=12.00, concentration_power=1.35,
        source_weights={"VALUE BET": 1.10, "PRONOSTIC SUR": 0.90, "LONG TERME": 0.80},
        category_caps={"SAFE": 0.160, "MODERE": 0.200, "RISQUE": 0.120},
        parlay_enabled=True, parlay_max_tickets=4, parlay_stake_cap=0.018,
        description="Croissance offensive : plus de positions, concentration sur les meilleurs edges, variance assumee.",
    ),
}
DEFAULT_PROFILE = "equilibre"

# En dessous, la mise Kelly credibilisee est trop faible pour etre une vraie
# recommandation (arrondie a 0$ sur toute bankroll raisonnable) : on ne
# l'affiche pas dans le plan.
_MIN_STAKE_FRACTION = 1e-4

# Deal "typique" si aucun actif (pour projeter une periode sans board).
_FALLBACK_TYPICAL = {"probability": 0.52, "odd": 2.00, "stake_fraction": 0.01}


def credible_probability(
    model_probability: float, implied_probability: float, credibility: float
) -> float:
    """Probabilite de projection : le modele retreci vers le marche.

    credibility=1 -> on croit le modele ; credibility=0 -> on ne croit que
    le marche. C'est la proba honnete pour dimensionner les mises et
    simuler — PAS celle qui a declenche le deal.
    """
    c = min(1.0, max(0.0, credibility))
    return implied_probability + c * (model_probability - implied_probability)


def kelly_fraction(probability: float, odd: float) -> float:
    """Kelly plein : fraction de bankroll qui maximise la croissance log."""
    if odd <= 1.0:
        return 0.0
    b = odd - 1.0
    raw = ((b * probability) - (1.0 - probability)) / b
    return max(0.0, raw)


def stake_fraction(
    model_probability: float,
    implied_probability: float,
    odd: float,
    confidence_score: float,
    profile: RiskProfile,
) -> float:
    """Mise recommandee (% bankroll) : Kelly credibilise, fractionne, plafonne."""
    edge = model_probability - implied_probability
    credibility = _edge_credibility(edge, confidence_score)
    if edge < profile.min_edge or credibility < profile.min_credibility:
        return 0.0
    p = credible_probability(model_probability, implied_probability, credibility)
    if odd > profile.max_odd:
        return 0.0
    if p * odd - 1.0 < profile.min_expected_value:
        return 0.0
    fraction = kelly_fraction(p, odd) * profile.kelly_multiplier
    if profile.concentration_power != 1.0 and profile.stake_cap > 0:
        normalized = min(1.0, fraction / profile.stake_cap)
        fraction = (normalized ** profile.concentration_power) * profile.stake_cap
    return min(fraction, profile.stake_cap)


def _category(p_credible: float) -> str:
    if p_credible >= 0.55:
        return "SAFE"
    if p_credible >= 0.40:
        return "MODERE"
    return "RISQUE"


def _strategy_score(
    p_credible: float,
    odd: float,
    edge: float,
    credibility: float,
    profile: RiskProfile,
) -> float:
    """Score de selection propre au profil, pas seulement a la mise."""
    expected_value = p_credible * odd - 1.0
    if profile.code == "prudent":
        # Priorite : probabilite de toucher + credibilite ; les longues cotes
        # sont penalisees meme si l'EV semble bonne.
        odd_penalty = max(0.25, 1.0 - max(0.0, odd - 2.2) * 0.18)
        return (p_credible * 1.8 + credibility + edge * 4.0 + expected_value) * odd_penalty
    if profile.code == "agressif":
        # Priorite : croissance attendue ; accepte plus de variance.
        return expected_value * 2.0 + edge * 3.0 + credibility * 0.35 + min(odd, 8.0) * 0.025
    # Equilibre : compromis EV / probabilite / credibilite.
    return expected_value + edge * 2.5 + p_credible * 0.65 + credibility * 0.55


def _apply_category_caps(lines: list[dict[str, Any]], profile: RiskProfile) -> None:
    """Controle l'exposition par classe de risque, comme un risk desk."""
    for category, cap in profile.category_caps.items():
        bucket = [line for line in lines if line["category"] == category]
        total = sum(line["stake_fraction"] for line in bucket)
        if total > cap and total > 0:
            scale = cap / total
            for line in bucket:
                line["stake_fraction"] *= scale


def build_portfolio_plan(
    deals: Sequence[dict[str, Any]],
    bankroll: float,
    days: int,
    profile_code: str = DEFAULT_PROFILE,
    bets_per_day: float = 0.0,
    simulations: int = 10_000,
    seed: int = 42,
) -> dict[str, Any]:
    """Plan de mise complet sur les deals actifs + projection de la periode.

    deals : lignes du board (une par match) avec model_probability,
    implied_probability, market_odd, confidence_score et un label.
    bets_per_day : rythme de deals observe (pour projeter au-dela du board).
    """
    profile = RISK_PROFILES.get(profile_code, RISK_PROFILES[DEFAULT_PROFILE])
    bankroll = max(0.0, float(bankroll))
    days = max(1, int(days))

    # --- allocations par deal -------------------------------------------------
    lines: list[dict[str, Any]] = []
    for deal in deals:
        model_p = float(deal["model_probability"])
        implied_p = float(deal["implied_probability"])
        odd = float(deal["market_odd"])
        confidence = float(deal.get("confidence_score") or 0.5)
        edge = model_p - implied_p
        credibility = _edge_credibility(edge, confidence)
        p_credible = credible_probability(model_p, implied_p, credibility)
        fraction = stake_fraction(model_p, implied_p, odd, confidence, profile)
        source = str(deal.get("source") or "VALUE BET")
        fraction *= profile.source_weights.get(source, 1.0)
        # Kelly credibilise = 0 -> "ne mise pas". Une position recommandee a
        # 0$ n'est pas une recommandation : on l'ecarte du plan (elle reste
        # visible comme value bet sur le board, mais la strategie ne joue que
        # ce qui merite une mise).
        if fraction <= _MIN_STAKE_FRACTION:
            continue
        lines.append(
            {
                "label": str(deal.get("label") or "-"),
                "market_code": str(deal.get("market_code") or "1X2"),
                "selection_code": str(deal.get("selection_code") or "-"),
                "bookmaker": str(deal.get("bookmaker") or "-"),
                "kickoff_utc": deal.get("kickoff_utc"),
                "market_odd": odd,
                "line": deal.get("line"),
                "model_probability": model_p,
                "credible_probability": p_credible,
                "stake_fraction": fraction,
                "strategy_score": _strategy_score(p_credible, odd, edge, credibility, profile),
                "expected_value": round(p_credible * odd - 1.0, 4),
                "edge_probability": edge,
                "edge_credibility": credibility,
                "category": _category(p_credible),
                "source": source,
                "fixture_id": deal.get("fixture_id"),
                "position_count": int(deal.get("position_count") or 0),
                "taken_odd": deal.get("taken_odd"),
                "taken_stake_amount": deal.get("stake_amount"),
            }
        )

    # Chaque profil decide aussi QUOI jouer :
    # prudent = shortlist stricte ; equilibre = diversification ; agressif =
    # plus large mais concentre les mises sur les meilleurs scores.
    lines.sort(key=lambda l: l["strategy_score"], reverse=True)
    if profile.max_positions > 0:
        lines = lines[:profile.max_positions]

    _apply_category_caps(lines, profile)

    # Plafond d'exposition simultanee : si la somme depasse, tout est reduit
    # proportionnellement (la hierarchie Kelly entre deals est preservee).
    total_fraction = sum(line["stake_fraction"] for line in lines)
    if total_fraction > profile.exposure_cap and total_fraction > 0:
        scale = profile.exposure_cap / total_fraction
        for line in lines:
            line["stake_fraction"] *= scale
        total_fraction = profile.exposure_cap

    for line in lines:
        line["stake_amount"] = round(bankroll * line["stake_fraction"], 2)
        line["stake_pct"] = round(line["stake_fraction"] * 100.0, 2)
        # Profit encaisse SI le pari gagne (cote - 1, la mise revient en plus).
        line["win_profit"] = round(line["stake_amount"] * (line["market_odd"] - 1.0), 2)
        # Esperance en $ avec la proba credibilisee : moyenne ponderee de
        # +win_profit (proba p) et -mise (proba 1-p). C'est le chiffre qui
        # justifie le pari, PAS le gain potentiel.
        line["expected_profit"] = round(
            line["credible_probability"] * line["win_profit"]
            - (1.0 - line["credible_probability"]) * line["stake_amount"],
            2,
        )

    lines.sort(key=lambda l: l["stake_amount"], reverse=True)

    # Repartition safe/modere/risque de l'exposition.
    exposure_amount = sum(line["stake_amount"] for line in lines)
    repartition = {}
    for category in ("SAFE", "MODERE", "RISQUE"):
        amount = sum(l["stake_amount"] for l in lines if l["category"] == category)
        repartition[category] = round(100.0 * amount / exposure_amount, 1) if exposure_amount else 0.0

    # --- projection Monte Carlo de la periode ----------------------------------
    simulation = _simulate_period(lines, bankroll, days, bets_per_day, simulations, seed, profile)

    return {
        "profile": {
            "code": profile.code,
            "label": profile.label,
            "description": profile.description,
            "kelly_multiplier": profile.kelly_multiplier,
            "stake_cap_pct": round(profile.stake_cap * 100, 1),
            "exposure_cap_pct": round(profile.exposure_cap * 100, 1),
            "min_edge_pct": round(profile.min_edge * 100, 1),
            "min_ev_pct": round(profile.min_expected_value * 100, 1),
            "max_positions": profile.max_positions,
            "max_odd": profile.max_odd,
            "parlay_enabled": profile.parlay_enabled,
            "source_weights": dict(profile.source_weights),
            "category_caps_pct": {
                key: round(value * 100, 1) for key, value in profile.category_caps.items()
            },
        },
        "bankroll": bankroll,
        "days": days,
        "lines": lines,
        "exposure_amount": round(exposure_amount, 2),
        "exposure_pct": round(total_fraction * 100.0, 2),
        "expected_profit_amount": round(sum(l["expected_profit"] for l in lines), 2),
        "repartition": repartition,
        "simulation": simulation,
    }


# Gardes des tickets combines : la variance explose avec chaque jambe.
PARLAY_MIN_COMBINED_PROBABILITY = 0.25
PARLAY_MAX_LEGS = 3
PARLAY_KELLY_HAIRCUT = 0.5      # Kelly du ticket reduit de moitie
PARLAY_STAKE_CAP = 0.01         # jamais plus de 1% de bankroll sur un combine


def build_parlay_suggestions(
    lines: Sequence[dict[str, Any]],
    bankroll: float,
    profile: RiskProfile,
    max_tickets: int = 3,
) -> list[dict[str, Any]]:
    """Tickets combines A PARTIR des positions deja retenues.

    Un combine multiplie cotes ET probabilites — jambes de MATCHS DIFFERENTS
    uniquement (deux paris du meme match sont correles). Un ticket n'est
    propose que s'il reste probable (>= 25%) et que son esperance depasse
    celle de sa meilleure jambe : sinon le combine n'apporte que de la
    variance, pas de la valeur.
    """
    if not profile.parlay_enabled or profile.parlay_max_tickets <= 0:
        return []

    legs = [
        line for line in lines
        if line.get("stake_fraction", 0) > 0
        and line.get("credible_probability", 0) >= (0.55 if profile.code == "equilibre" else 0.42)
    ]
    # Une seule jambe par match.
    seen_labels: set[str] = set()
    unique_legs = []
    for leg in sorted(legs, key=lambda l: l["credible_probability"], reverse=True):
        if leg["label"] in seen_labels:
            continue
        seen_labels.add(leg["label"])
        unique_legs.append(leg)

    tickets: list[dict[str, Any]] = []
    candidates = unique_legs[:6]
    combos: list[tuple[dict, ...]] = []
    for size in (2, min(3, PARLAY_MAX_LEGS)):
        for i in range(len(candidates)):
            for j in range(i + 1, len(candidates)):
                if size == 2:
                    combos.append((candidates[i], candidates[j]))
                else:
                    for k in range(j + 1, len(candidates)):
                        combos.append((candidates[i], candidates[j], candidates[k]))

    for combo in combos:
        probability = 1.0
        combined_odd = 1.0
        for leg in combo:
            probability *= leg["credible_probability"]
            combined_odd *= leg["market_odd"]
        min_probability = PARLAY_MIN_COMBINED_PROBABILITY
        if profile.code == "equilibre":
            min_probability = max(min_probability, 0.30)
        elif profile.code == "agressif":
            min_probability = 0.18
        if probability < min_probability:
            continue
        expected_value = probability * combined_odd - 1.0
        best_leg_ev = max(
            leg["credible_probability"] * leg["market_odd"] - 1.0 for leg in combo
        )
        if expected_value <= max(0.0, best_leg_ev):
            continue
        fraction = min(
            kelly_fraction(probability, combined_odd)
            * profile.kelly_multiplier * PARLAY_KELLY_HAIRCUT,
            profile.parlay_stake_cap,
        )
        if fraction <= 0:
            continue
        stake_amount = round(bankroll * fraction, 2)
        tickets.append(
            {
                "legs": [
                    {
                        "label": leg["label"],
                        "selection_code": leg["selection_code"],
                        "market_odd": leg["market_odd"],
                        "bookmaker": leg["bookmaker"],
                        # Necessaires au reglement du combine (chaque jambe).
                        "fixture_id": leg.get("fixture_id"),
                        "market_code": leg.get("market_code", "1X2"),
                    }
                    for leg in combo
                ],
                "combined_odd": round(combined_odd, 2),
                "combined_probability": round(probability, 4),
                "expected_value": round(expected_value, 4),
                "stake_fraction": fraction,
                "stake_amount": stake_amount,
                "stake_pct": round(fraction * 100.0, 2),
                "win_profit": round(stake_amount * (combined_odd - 1.0), 2),
            }
        )

    tickets.sort(key=lambda t: t["expected_value"], reverse=True)
    return tickets[:min(max_tickets, profile.parlay_max_tickets)]


def _simulate_period(
    lines: list[dict[str, Any]],
    bankroll: float,
    days: int,
    bets_per_day: float,
    simulations: int,
    seed: int,
    profile: RiskProfile,
) -> dict[str, Any]:
    """Monte Carlo : deals actifs puis paris futurs au rythme observe.

    Chaque mise est proportionnelle a la bankroll COURANTE (comme Kelly le
    prescrit) : la trajectoire compose gains et pertes, jamais de ruine
    totale possible, mais les drawdowns sont mesures honnetement.
    """
    if bankroll <= 0:
        return {"error": "bankroll nulle"}

    active = [
        (l["credible_probability"], l["market_odd"], l["stake_fraction"])
        for l in lines
        if l["stake_fraction"] > 0
    ]
    expected_total_bets = max(len(active), int(round(bets_per_day * days)))
    future_count = max(0, expected_total_bets - len(active))

    # Le pari futur "typique" reprend le profil median du board actuel.
    if active:
        typical = (
            median(p for p, _, _ in active),
            median(o for _, o, _ in active),
            median(s for _, _, s in active),
        )
    else:
        fallback_stake = min(
            _FALLBACK_TYPICAL["stake_fraction"] * (profile.kelly_multiplier / 0.25),
            profile.stake_cap,
            max(0.0, profile.exposure_cap / max(1, min(profile.max_positions, 10))),
        )
        typical = (
            _FALLBACK_TYPICAL["probability"],
            _FALLBACK_TYPICAL["odd"],
            max(_MIN_STAKE_FRACTION, fallback_stake),
        )

    bet_plan = active + [typical] * future_count
    if not bet_plan:
        return {
            "final_median": round(bankroll, 2),
            "final_p5": round(bankroll, 2),
            "final_p95": round(bankroll, 2),
            "prob_loss_pct": 0.0,
            "prob_drawdown30_pct": 0.0,
            "expected_bets": 0,
            "simulations": 0,
        }

    rng = random.Random(seed)
    finals: list[float] = []
    losses = 0
    drawdowns30 = 0
    for _ in range(simulations):
        wealth = 1.0
        trough = 1.0
        peak = 1.0
        for probability, odd, fraction in bet_plan:
            if rng.random() < probability:
                wealth *= 1.0 + fraction * (odd - 1.0)
            else:
                wealth *= 1.0 - fraction
            peak = max(peak, wealth)
            trough = min(trough, wealth / peak)
        finals.append(wealth)
        if wealth < 1.0:
            losses += 1
        if trough <= 0.70:
            drawdowns30 += 1

    finals.sort()
    def _pct(q: float) -> float:
        return finals[min(len(finals) - 1, int(q * len(finals)))]

    return {
        "final_median": round(bankroll * _pct(0.50), 2),
        "final_p5": round(bankroll * _pct(0.05), 2),
        "final_p95": round(bankroll * _pct(0.95), 2),
        "prob_loss_pct": round(100.0 * losses / simulations, 1),
        "prob_drawdown30_pct": round(100.0 * drawdowns30 / simulations, 1),
        "expected_bets": len(bet_plan),
        "simulations": simulations,
    }
