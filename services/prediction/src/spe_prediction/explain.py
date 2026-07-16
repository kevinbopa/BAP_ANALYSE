"""Human-readable explanation of a prediction (French).

Turns the engine's internal signals — opponent-adjusted Elo gap, recent form,
fatigue, expected goals, market view, data quality — into a short paragraph a
bettor can read to understand WHY the model landed where it did.
"""
from __future__ import annotations

from spe_prediction.domain import (
    EloOutput,
    FixtureFeatures,
    ImpliedProbabilities,
    MarketOdds,
    OutcomeProbabilities,
    PoissonOutput,
    PredictionVerdict,
)


def _form_label(recent_form: float) -> str:
    if recent_form >= 0.35:
        return "excellente forme"
    if recent_form >= 0.12:
        return "bonne forme"
    if recent_form > -0.12:
        return "forme neutre"
    if recent_form > -0.35:
        return "forme fragile"
    return "mauvaise passe"


def _fatigue_label(fatigue: float) -> str:
    if fatigue >= 0.60:
        return "calendrier tres charge"
    if fatigue >= 0.30:
        return "fraicheur reduite"
    return "fraicheur normale"


def _data_quality_label(quality: float) -> str:
    if quality >= 0.60:
        return "historique riche"
    if quality >= 0.25:
        return "historique partiel"
    return "peu d'historique"


def build_explanation(
    fixture: FixtureFeatures,
    poisson_output: PoissonOutput,
    elo_output: EloOutput,
    implied: ImpliedProbabilities | None,
    blended: OutcomeProbabilities,
    verdict: PredictionVerdict,
    market_odds: MarketOdds | None,
) -> str:
    home = fixture.home_team
    away = fixture.away_team
    parts: list[str] = []

    # 1. Strength gap (opponent-adjusted Elo)
    gap = home.elo_rating - away.elo_rating
    if abs(gap) < 40:
        strength = (
            f"Forces proches ({home.team_name} {home.elo_rating:.0f} vs "
            f"{away.team_name} {away.elo_rating:.0f} Elo ajuste adversaire)."
        )
    else:
        stronger = home.team_name if gap > 0 else away.team_name
        strength = (
            f"{stronger} domine au rating ajuste adversaire "
            f"({home.team_name} {home.elo_rating:.0f} vs {away.team_name} {away.elo_rating:.0f}, "
            f"ecart {abs(gap):.0f} pts)."
        )
    parts.append(strength)

    # 2. Form + fatigue, only when informative
    form_bits: list[str] = []
    if abs(home.recent_form) >= 0.12:
        form_bits.append(f"{home.team_name}: {_form_label(home.recent_form)}")
    if abs(away.recent_form) >= 0.12:
        form_bits.append(f"{away.team_name}: {_form_label(away.recent_form)}")
    if home.fatigue_penalty >= 0.30:
        form_bits.append(f"{home.team_name}: {_fatigue_label(home.fatigue_penalty)}")
    if away.fatigue_penalty >= 0.30:
        form_bits.append(f"{away.team_name}: {_fatigue_label(away.fatigue_penalty)}")
    if form_bits:
        parts.append(" ; ".join(form_bits) + ".")

    # 2b. Squad availability (player layer)
    for team in (home, away):
        if team.availability_index < 0.92:
            absences = ", ".join(team.key_absences[:4]) if team.key_absences else "joueurs cles"
            parts.append(
                f"Effectif reduit pour {team.team_name} "
                f"({team.availability_index:.0%} disponible - absents: {absences})."
            )

    # 3. Expected goals
    parts.append(
        f"Buts attendus {poisson_output.expected_home_goals:.2f} - "
        f"{poisson_output.expected_away_goals:.2f}."
    )

    # 4. Market agreement / dissent
    if implied is not None:
        model_top = {
            "HOME": blended.home,
            "DRAW": blended.draw,
            "AWAY": blended.away,
        }[verdict.selection_code]
        market_top = {
            "HOME": implied.home,
            "DRAW": implied.draw,
            "AWAY": implied.away,
        }[verdict.selection_code]
        dissent = model_top - market_top
        source_count = market_odds.source_count if market_odds else 0
        if abs(dissent) < 0.03:
            parts.append(
                f"Le marche ({source_count} bookmakers) est d'accord "
                f"({market_top:.0%} vs modele {model_top:.0%})."
            )
        elif dissent > 0:
            parts.append(
                f"Le modele est plus optimiste que le marche sur cette issue "
                f"({model_top:.0%} vs {market_top:.0%} implicite) - source potentielle de value."
            )
        else:
            parts.append(
                f"Le modele est plus prudent que le marche "
                f"({model_top:.0%} vs {market_top:.0%} implicite)."
            )
    else:
        parts.append("Aucune cote disponible - prediction purement statistique.")

    # 4b. Correlations detectees (moteur de correlations)
    if fixture.insights:
        parts.append("Correlations: " + " | ".join(fixture.insights) + ".")

    # 5. Data quality caveat
    min_quality = min(home.data_quality, away.data_quality)
    if min_quality < 0.25:
        weakest = home.team_name if home.data_quality <= away.data_quality else away.team_name
        parts.append(
            f"Attention: {_data_quality_label(min_quality)} pour {weakest} - "
            f"la prediction s'appuie davantage sur le consensus du marche."
        )

    # 6. Verdict recap
    parts.append(
        f"Pronostic: {verdict.label} ({verdict.probability:.0%}, "
        f"surete {verdict.surety_score:.0%})."
    )
    if verdict.draw_competitive:
        parts.append(
            f"Le match nul ({blended.draw:.0%}) talonne ce pronostic - "
            "match tres serre, le nul est le scenario alternatif le plus credible."
        )

    return " ".join(parts)
