"""Marche buteur : probabilite qu'un joueur marque au moins une fois.

Approche "part de buts x xG de l'equipe", enrichie au niveau JOUEUR :

    lambda = xG_equipe_ce_match x part_ponderee x forme x disponibilite
    P(marque >= 1) = 1 - exp(-lambda)   [Poisson]

Ce qui entre dans le calcul :
  - xG_equipe_ce_match : vient du MOTEUR COMPLET (Poisson+Elo+XGBoost+marche
    + contexte : adversaire, meteo, fatigue, niveau du championnat). Tout le
    comportement d'equipe et de competition transite par la.
  - part_ponderee : buts du joueur / buts de l'equipe, PONDERES PAR RECENCE
    (demi-vie 1 an, comme le reste du moteur) -> la forme recente pese plus.
  - forme : rythme des 90 derniers jours vs rythme de base -> chaud/froid.
  - disponibilite : presence dans les dernieres compos de l'equipe (proxy
    blessure / rotation / banc) quand les lineups existent ; neutre sinon.

Le value betting reste LA methode : P(modele) confrontee a la cote reelle
"anytime scorer" (Yes), avec seuils renforces (marge enorme du marche) +
failsafe longshot (buteur d'elite cote outsider = info d'equipe ignoree).
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Sequence

# Bornes des ajustements joueur (evitent qu'un signal bruite explose lambda).
FORM_MIN, FORM_MAX = 0.70, 1.45
AVAILABILITY_FLOOR = 0.15  # jamais 0 pur : on ne connait pas toujours la compo

# Regularisation par poste : la part de buts SEULE surestime les rares
# buteurs atypiques (defenseur qui marque de la tete, gardien). Le poste
# corrige ce que la part capte mal — surtout en DAMPANT les cas trompeurs.
# Doux et neutre par defaut (poste inconnu / milieu -> 1.0) pour ne PAS
# double-compter avec la part (un attaquant a deja une grosse part).
POSITION_FACTORS = {
    "G": 0.15,  # gardien : ne marque quasi jamais dans le jeu
    "D": 0.80,  # defenseur : buts surtout sur coups de pied arretes
    "L": 0.90, "R": 0.90,  # lateraux (ou ailiers ambigus) : leger malus
    "F": 1.05, "S": 1.05, "A": 1.05, "W": 1.05,  # attaquants : leger bonus
}


def position_factor(position_code: str | None) -> float:
    """Facteur multiplicatif doux du poste (1.0 = neutre / inconnu / milieu)."""
    if not position_code:
        return 1.0
    return POSITION_FACTORS.get(position_code.strip().upper()[:1], 1.0)

# Gardes des deals buteur : marche a forte marge et plus incertain que le
# 1X2 -> seuils plus stricts.
SCORER_MIN_EDGE = 0.04
SCORER_MAX_EDGE = 0.18
SCORER_MIN_PROBABILITY = 0.05
SCORER_MAX_PROBABILITY = 0.90
# Failsafe longshot : un buteur d'elite cote comme un outsider (implicite
# basse) qui degage un gros edge = le book connait une info d'equipe (repos,
# blessure, banc) que le modele ignore. On rejette ces faux edges — c'est le
# risque #1 du marche buteur (le modele suppose que le joueur joue).
SCORER_LONGSHOT_IMPLIED = 0.12
SCORER_LONGSHOT_EDGE = 0.06
# Fiabilite minimale de la part : sans assez de buts d'equipe / de joueur,
# la part est du bruit.
MIN_TEAM_GOALS = 8
MIN_PLAYER_GOALS = 2


@dataclass(frozen=True)
class PlayerGoalRecord:
    player_id: int
    player_name: str
    goals: int                       # buts bruts (garde d'echantillon)
    matches: int
    weighted_goals: float = 0.0      # buts ponderes recence
    form_factor: float = 1.0         # rythme recent / rythme de base
    availability: float = 1.0        # presence recente (proxy blessure/banc)
    position_code: str | None = None  # poste (regularise la part de buts)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def scorer_probability(
    team_expected_goals: float,
    player_goals: int,
    team_total_goals: int,
    weighted_player_goals: float | None = None,
    weighted_team_goals: float | None = None,
    form_factor: float = 1.0,
    availability: float = 1.0,
    position_code: str | None = None,
) -> float:
    """P(le joueur marque >= 1) : part ponderee x xG equipe x forme x dispo x poste."""
    # Garde d'echantillon sur les comptes BRUTS (le pondere est plus petit).
    if team_total_goals < MIN_TEAM_GOALS or player_goals < MIN_PLAYER_GOALS:
        return 0.0
    wp = weighted_player_goals if weighted_player_goals is not None else float(player_goals)
    wt = weighted_team_goals if weighted_team_goals is not None else float(team_total_goals)
    if wt <= 0:
        return 0.0
    share = wp / wt
    lam = (
        max(0.0, float(team_expected_goals))
        * share
        * _clamp(form_factor, FORM_MIN, FORM_MAX)
        * _clamp(availability, AVAILABILITY_FLOOR, 1.0)
        * position_factor(position_code)
    )
    if lam <= 0:
        return 0.0
    probability = 1.0 - math.exp(-lam)
    return min(SCORER_MAX_PROBABILITY, max(0.0, probability))


def team_scorer_probabilities(
    team_expected_goals: float,
    players: Sequence[PlayerGoalRecord],
    team_total_goals: int,
    weighted_team_goals: float | None = None,
) -> dict[int, float]:
    """P(marque) par joueur d'une equipe (avec forme/dispo par joueur)."""
    result: dict[int, float] = {}
    for player in players:
        p = scorer_probability(
            team_expected_goals,
            player.goals,
            team_total_goals,
            weighted_player_goals=player.weighted_goals or None,
            weighted_team_goals=weighted_team_goals,
            form_factor=player.form_factor,
            availability=player.availability,
            position_code=player.position_code,
        )
        if p >= SCORER_MIN_PROBABILITY:
            result[player.player_id] = round(p, 6)
    return result


def scorer_deal_candidates(
    model_probabilities: dict[int, float],
    market_odds: dict[int, float],
) -> list[dict[str, float]]:
    """Value bets buteur : edge = P(modele) - proba implicite (1/cote).

    Le marche "anytime scorer" ne cote que le "Yes" : la marge du book est
    deja dans la cote, on ne peut pas la normaliser. On exige donc un edge
    net (SCORER_MIN_EDGE) qui couvre franchement le vig.
    """
    candidates = []
    for player_id, odd in market_odds.items():
        if odd <= 1.0:
            continue
        model_p = float(model_probabilities.get(player_id, 0.0))
        implied = 1.0 / odd
        edge = model_p - implied
        longshot_failsafe = implied < SCORER_LONGSHOT_IMPLIED and edge > SCORER_LONGSHOT_EDGE
        if (
            SCORER_MIN_PROBABILITY <= model_p <= SCORER_MAX_PROBABILITY
            and SCORER_MIN_EDGE <= edge < SCORER_MAX_EDGE
            and model_p * odd > 1.0
            and not longshot_failsafe
        ):
            candidates.append(
                {
                    "player_id": player_id,
                    "model_probability": round(model_p, 6),
                    "implied_probability": round(implied, 6),
                    "edge_probability": round(edge, 6),
                    "market_odd": odd,
                }
            )
    candidates.sort(key=lambda c: c["edge_probability"], reverse=True)
    return candidates
