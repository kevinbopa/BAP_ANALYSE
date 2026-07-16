# Architecture cible — parcours client et marchés long terme

## Parcours

```
Sport (Football, V1 seul actif)
 └─ Compétition (sélecteur, + 2 entrées spéciales)
     ├─ [une compétition]         → prédictions + deals sur la période choisie
     │                              (défaut : 1 semaine à partir d'aujourd'hui)
     ├─ Toutes compétitions       → meilleurs deals de la semaine, tous championnats
     └─ Faits divers (long terme) → marchés outright : vainqueur de championnat,
                                    vainqueur de tournoi, meilleur buteur, indice Ballon d'Or
 └─ Stratégie de paris (bouton)
     → sport + compétition + période (jour/semaine/mois/3 mois/an) + budget + profil de risque
     → plan de mise optimal : positions simples (value bets + pronostics sûrs),
       tickets combinés quand l'espérance le justifie,
       et positions LONG TERME (faits divers) intégrées automatiquement
       dès que la période couvre leur échéance.
     → chaque position pointe vers l'analyse complète du match (/match/{id}).
```

## Faits divers — la vision

**Ce que c'est** : les marchés à échéance longue ("outrights"), prédits par la même
philosophie que les matchs — nos probabilités contre les prix du marché — mais avec
des horizons de semaines ou de mois.

| Marché | Méthode de prédiction | Deal possible ? |
|---|---|---|
| Vainqueur de championnat | Simulation Monte Carlo de la saison restante : chaque match restant (déjà en base avec son calendrier) reçoit ses probabilités par le moteur, on simule N saisons, on compte les titres | Oui si cotes outright disponibles |
| Vainqueur de tournoi (Coupe du monde) | Simulation des matchs restants connus + appariements des tours suivants | Oui — The Odds API cote `soccer_fifa_world_cup_winner` |
| Meilleur buteur d'un championnat | Timeline des buts (core.fixture_timeline) → rythme par joueur → projection sur les matchs restants de son équipe, Monte Carlo Poisson | Rare (cotes rarement exposées) — prédiction affichée quand même |
| Indice Ballon d'Or | Score de performance (buts + passes pondérés par compétition, 12 mois glissants) | NON — aucun marché coté chez nos providers, pas d'edge mesurable : affiché à titre indicatif, jamais vendu comme deal |

**Principe d'honnêteté** : un fait divers sans cote n'est jamais un "deal", c'est une
prédiction. Un fait divers avec cote suit exactement la méthode value : edge = P(modèle)
− P(implicite), gardes de crédibilité, Kelly.

## Données (migration 0014)

- `core.outright_markets` — un marché : code (LEAGUE_WINNER, TOURNAMENT_WINNER,
  TOP_SCORER, BALLON_DOR_INDEX), compétition, échéance, statut.
- `core.outright_selections` — les candidats (équipe ou joueur).
- `core.outright_odds` — cotes par bookmaker/candidat (The Odds API, marché `outrights`).
- `model.outright_predictions` — nos probabilités par candidat (méthode + généré le).
- `model.outright_deals` — les value bets long terme (edge, cote, statut, règlement).

## Combinés (tickets)

Un combiné multiplie cotes ET probabilités (jambes de matchs différents uniquement —
jamais deux paris du même match, corrélation). Le moteur propose un ticket seulement si :
proba combinée ≥ 25 %, chaque jambe a une espérance positive, et l'EV du ticket dépasse
celle de la meilleure jambe seule. Mise : Kelly du ticket × 0.5 (la variance d'un combiné
est très supérieure), plafond 1 % de bankroll.

## Périodes

Horizon des prédictions/deals par compétition : aujourd'hui / 48 h / 1 semaine (défaut) /
2 semaines / 1 mois. Stratégie : jour / semaine / mois / 3 mois / an — les faits divers
dont l'échéance tombe dans la période sont intégrés d'office au plan.
