"""Marches long terme ("faits divers") — les previsions a echeance de semaines.

Meme philosophie que les matchs : NOS probabilites d'abord, le marche ensuite.

  - Vainqueur de championnat : la saison restante est simulee match par match
    (chaque match restant recoit ses probabilites 1X2 du moteur), N fois ;
    la part de titres = la probabilite. Zero formule magique : c'est le
    calendrier reel qui parle.
  - Vainqueur de tournoi a elimination : les matchs connus sont simules,
    puis les survivants sont apparies dans l'ordre du tableau (approximation
    par ordre chronologique des kickoffs quand le bracket exact n'est pas
    structure en base — documente dans method details).
  - Meilleur buteur : rythme de buts observe (timeline reelle) projete sur
    les matchs restants de l'equipe du joueur, Monte Carlo Poisson.
  - Indice Ballon d'Or : score de performance descriptif (buts + passes,
    ponderes par competition) — PAS une probabilite de gagner, AUCUN deal :
    il n'existe pas de marche cote ni d'edge mesurable.

Un fait divers avec cote devient un deal par la meme methode value que les
matchs : edge = P(modele) - P(implicite normalisee), gardes, Kelly.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import random
from typing import Mapping, Sequence

# Gardes des deals outright : les probabilites long terme sont plus
# incertaines que celles d'un match — seuils plus stricts que le 1X2.
OUTRIGHT_MIN_EDGE = 0.03
OUTRIGHT_MAX_EDGE = 0.25
OUTRIGHT_MIN_PROBABILITY = 0.02


@dataclass(frozen=True)
class RemainingMatch:
    home_team_id: int
    away_team_id: int
    p_home: float
    p_draw: float
    p_away: float


def simulate_league_title(
    remaining: Sequence[RemainingMatch],
    current_points: Mapping[int, float],
    simulations: int = 10_000,
    seed: int = 42,
) -> dict[int, float]:
    """P(titre) par equipe : Monte Carlo de la saison restante.

    current_points : points deja acquis cette saison (0 partout si la
    saison n'a pas commence). Egalite de points : depart au hasard —
    les criteres fins (difference de buts) sont du bruit a cet horizon.
    """
    if simulations <= 0:
        return {}
    rng = random.Random(seed)
    teams: set[int] = set(current_points)
    for match in remaining:
        teams.add(match.home_team_id)
        teams.add(match.away_team_id)
    if not teams:
        return {}

    titles: dict[int, int] = {team: 0 for team in teams}
    base_points = {team: float(current_points.get(team, 0.0)) for team in teams}

    for _ in range(simulations):
        points = dict(base_points)
        for match in remaining:
            draw_threshold = match.p_home + match.p_draw
            roll = rng.random()
            if roll < match.p_home:
                points[match.home_team_id] += 3.0
            elif roll < draw_threshold:
                points[match.home_team_id] += 1.0
                points[match.away_team_id] += 1.0
            else:
                points[match.away_team_id] += 3.0
        best = max(points.values())
        leaders = [team for team, pts in points.items() if pts == best]
        titles[rng.choice(leaders)] += 1

    return {team: count / simulations for team, count in titles.items() if count > 0}


def simulate_knockout(
    rounds: Sequence[Sequence[RemainingMatch]],
    simulations: int = 10_000,
    seed: int = 42,
    win_probability_fn=None,
) -> dict[int, float]:
    """P(vainqueur du tournoi) : simulation des tours restants.

    rounds[0] = matchs connus du prochain tour. Les tours suivants sont
    apparies dans l'ordre des vainqueurs (bracket implicite : le vainqueur
    du match i rencontre celui du match i+1). Les probabilites des matchs
    futurs inconnus viennent de win_probability_fn(team_a, team_b) ->
    p(team_a gagne, nuls resolus), fournie par l'appelant (typiquement un
    ecart Elo). Sans fn : 50/50 (documente, prudent).
    """
    if not rounds or simulations <= 0:
        return {}
    rng = random.Random(seed)
    wins: dict[int, int] = {}

    def _match_winner(match: RemainingMatch) -> int:
        # Nul -> qualification tranchee au prorata des forces hors nul.
        total = match.p_home + match.p_away
        p_home_through = (match.p_home / total) if total > 0 else 0.5
        return match.home_team_id if rng.random() < p_home_through else match.away_team_id

    def _future_winner(team_a: int, team_b: int) -> int:
        p_a = win_probability_fn(team_a, team_b) if win_probability_fn else 0.5
        return team_a if rng.random() < p_a else team_b

    for _ in range(simulations):
        survivors = [_match_winner(match) for match in rounds[0]]
        while len(survivors) > 1:
            next_round: list[int] = []
            for i in range(0, len(survivors) - 1, 2):
                next_round.append(_future_winner(survivors[i], survivors[i + 1]))
            if len(survivors) % 2 == 1:
                next_round.append(survivors[-1])
            survivors = next_round
        if survivors:
            wins[survivors[0]] = wins.get(survivors[0], 0) + 1

    return {team: count / simulations for team, count in wins.items()}


@dataclass(frozen=True)
class ScorerState:
    player_id: int
    player_name: str
    team_id: int
    goals: int
    matches_played: int


def top_scorer_probabilities(
    scorers: Sequence[ScorerState],
    remaining_by_team: Mapping[int, int],
    simulations: int = 10_000,
    seed: int = 42,
) -> dict[int, float]:
    """P(meilleur buteur) : rythme observe projete en Poisson sur les
    matchs restants de l'equipe. Egalite : co-vainqueurs comptes chacun
    (convention des trophees partages)."""
    if not scorers or simulations <= 0:
        return {}
    rng = random.Random(seed)
    rates = []
    for scorer in scorers:
        matches = max(1, scorer.matches_played)
        rate_per_match = scorer.goals / matches
        remaining = max(0, int(remaining_by_team.get(scorer.team_id, 0)))
        rates.append((scorer.player_id, scorer.goals, rate_per_match * remaining))

    wins: dict[int, int] = {player_id: 0 for player_id, _, _ in rates}
    for _ in range(simulations):
        best_total = -1
        best_players: list[int] = []
        for player_id, goals, expected_future in rates:
            future = _poisson_draw(rng, expected_future)
            total = goals + future
            if total > best_total:
                best_total = total
                best_players = [player_id]
            elif total == best_total:
                best_players.append(player_id)
        for player_id in best_players:
            wins[player_id] += 1

    total_awards = sum(wins.values())
    if total_awards == 0:
        return {}
    # Normalise pour que la somme fasse 1 malgre les co-vainqueurs.
    return {pid: count / total_awards for pid, count in wins.items() if count > 0}


def _poisson_draw(rng: random.Random, lam: float) -> int:
    """Tirage de Poisson (Knuth) — lam modere (< 40 buts restants)."""
    if lam <= 0:
        return 0
    threshold = math.exp(-lam)
    k = 0
    p = 1.0
    while True:
        p *= rng.random()
        if p <= threshold:
            return k
        k += 1


def performance_index(
    rows: Sequence[tuple[int, str, float, float, float]],
) -> list[dict[str, object]]:
    """Indice Ballon d'Or : (player_id, nom, buts ponderes, passes ponderees,
    poids competition moyen) -> score descriptif normalise 0..1.

    Ce N'EST PAS une probabilite de gagner le trophee (vote humain, aucun
    marche cote) : c'est un classement par performance mesuree.
    """
    scored = []
    for player_id, name, weighted_goals, weighted_assists, avg_weight in rows:
        score = (weighted_goals + 0.7 * weighted_assists) * max(0.8, avg_weight)
        scored.append((player_id, name, score))
    if not scored:
        return []
    top_score = max(score for _, _, score in scored) or 1.0
    scored.sort(key=lambda item: item[2], reverse=True)
    return [
        {
            "player_id": player_id,
            "player_name": name,
            "index": round(score / top_score, 4),
            "raw_score": round(score, 2),
        }
        for player_id, name, score in scored
    ]


def outright_deal_candidates(
    model_probabilities: Mapping[int, float],
    market_odds: Mapping[int, float],
) -> list[dict[str, float]]:
    """Value bets long terme : edge vs probabilites implicites NORMALISEES
    du marche outright (la marge d'un marche a 20+ candidats est enorme —
    la normalisation est indispensable)."""
    inverse_sum = sum(1.0 / odd for odd in market_odds.values() if odd > 1.0)
    if inverse_sum <= 0:
        return []
    candidates = []
    for selection_id, odd in market_odds.items():
        if odd <= 1.0:
            continue
        model_p = float(model_probabilities.get(selection_id, 0.0))
        implied = (1.0 / odd) / inverse_sum
        edge = model_p - implied
        if (
            model_p >= OUTRIGHT_MIN_PROBABILITY
            and OUTRIGHT_MIN_EDGE <= edge < OUTRIGHT_MAX_EDGE
            and model_p * odd > 1.0
        ):
            candidates.append(
                {
                    "selection_id": selection_id,
                    "model_probability": round(model_p, 6),
                    "implied_probability": round(implied, 6),
                    "edge_probability": round(edge, 6),
                    "market_odd": odd,
                }
            )
    candidates.sort(key=lambda c: c["edge_probability"], reverse=True)
    return candidates
