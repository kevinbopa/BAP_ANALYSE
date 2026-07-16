# Prediction Service

## Objet

Ce module porte la pile algorithmique V1 du projet.

## Philosophie V1

La V1 ne repose pas sur "un algorithme magique". Elle repose sur une pile simple et serieuse :

- `prediction model` : `Dixon-Coles / Poisson` + `Elo` + `XGBoost` (optionnel)
- `forecast` : selection prediction + score de surete (`forecaster.py`)
- `pricing model` : probabilites implicites et retrait de marge
- `decision model` : `Expected Value` + credibilite anti-faux-edge
- `risk model` : `Fractional Kelly`
- `bankroll strategy` : profils prudent/equilibre/agressif avec caps par risque,
  EV minimum, cote max, exposition et Monte Carlo

### Mix d'algorithmes 1X2

| Algo | Status | Role |
|---|---|---|
| Poisson + Dixon-Coles | actif | distribution `1X2` derivee des xG |
| Elo (rating + draw decay) | actif | force globale + home field contextuel |
| XGBoost (multi-class) | optionnel | active des qu'un modele entraine existe a `models/xgboost_1x2.joblib` |
| Market consensus | anchor | ancre adaptative en fonction de la qualite des donnees |

Sans modele XGBoost entraine, le moteur tourne en 3-way blend (Poisson + Elo + Market). Apres entrainement, le moteur passe automatiquement en 4-way blend (50/35/20/anchor).

## Portee

- sport : football
- marche : `1X2`
- usage : pre-match

## Modules

- `domain.py` : objets metier du moteur (incl. `PredictionVerdict`)
- `elo.py` : prior de force d'equipe et probabilites `1X2`
- `rating.py` : Elo ajuste adversaire (replay chronologique de l'historique)
- `poisson.py` : moteur de buts attendus et distribution `1X2`
- `gboost.py` : predictor XGBoost optionnel avec fallback gracieux
- `market.py` : cotes -> probabilites implicites sans marge
- `forecaster.py` : selection predite + score de surete (sans value-bet)
- `correlations.py` : moteur de detection de correlations (presence joueur,
  synergies buteur-passeur, impact repos) avec correction FDR — seuls les
  insights VALIDES influencent les probabilites (`run_correlation_scan.py`)
- `player_signals.py` : disponibilite d'effectif ponderee par les notes
- `context.py` : facteurs extra-sportifs -> ajustements xG plafonnes
- `explain.py` : justification texte de chaque prediction (incl. correlations)
- `decision.py` : scoring de selection, EV, credibilite, recommendation
- `engine.py` : orchestration complete (Poisson + Elo + XGBoost + Market)
- `exact_score.py` : agent score exact coherent avec les xG et le bloc `1X2`
- `scorer.py` : probabilite buteur anytime via part de buts ponderee x xG equipe
- `golf.py` : probabilites golf par consensus de marche devige
- `bankroll.py` : strategie de mise, profils de risque et combines

## Pattern de calcul

`Donnees normalisees -> Poisson/Dixon-Coles -> Elo -> Ensemble -> Pricing -> Decision -> Ranking`

## Sortie attendue

Pour un match, le moteur retourne :

- probabilites `home/draw/away`
- buts attendus
- grille de scores exacts probable via l'agent dedie
- probabilites derivees `OU25` et `BTTS`
- probabilites buteur anytime quand les donnees joueur/cotes existent
- fair odds
- lecture du marche si disponible
- selection recommandee
- edge
- expected value
- mise Fractional Kelly
- score de classement

## Pipeline applicatif

Le module contient aussi une couche pipeline simple :

- chargement des fixtures candidates
- analyse avec le moteur
- serialisation vers les structures SQL de `predictions`, `value_bets` et `deal_rankings`

Fichiers utiles :

- `repository.py`
- `pipeline.py`
- `serialization.py`

## Execution reelle

Pour lancer le pipeline sur PostgreSQL local :

`py services/prediction/run_prediction_pipeline.py`

Le pipeline : 

- charge les fixtures sans prediction
- construit des features minimales a partir de l'historique
- calcule les probabilites `1X2`
- persiste `model.predictions`
- persiste `model.deal_rankings`
- persiste `model.value_bets` seulement si des odds existent en base

## Agent score exact

Le projet contient maintenant un agent dedie aux scores exacts :

- classe : `ExactScoreAgent`
- fichier : `src/spe_prediction/exact_score.py`
- principe : conserver la forme du scoreline Poisson / Dixon-Coles puis
  realigner chaque bucket `HOME/DRAW/AWAY` sur la probabilite `1X2` finale du
  moteur principal

Commande de demonstration :

`py services/prediction/run_exact_score_agent.py`

Sortie :

- `expected_home_goals`
- `expected_away_goals`
- top scores exacts tries
- fair odds associees

## Marche buteur anytime

Le module `scorer.py` estime `P(joueur marque >= 1)` avec :

- xG attendu de l'equipe sur le match ;
- part de buts du joueur dans son equipe, ponderee par recence ;
- forme recente ;
- disponibilite/rotation via les lineups quand disponibles ;
- regularisation par poste.

Commandes utiles :

```powershell
py services/ingestion/run_ingest_scorer_odds.py
py services/prediction/run_scorer_predictions.py
```

Les resultats alimentent `model.scorer_predictions`, `model.scorer_deals` et
`reporting.v_scorer_board`.

## Golf V1.1

Le module `golf.py` fournit un socle golf adapte aux marches outright :

- de-vig des cotes par bookmaker et marche ;
- aggregation mediane multi-book si plusieurs books sont ingeres ;
- shrinkage vers la probabilite de champ pour reduire le biais longshot/favori ;
- profondeur de marche, dispersion entre books et score de confiance ;
- prise en compte des facteurs tournoi (`WEATHER`, `COURSE_FIT`, etc.) quand ils existent ;
- probabilite modele, cote juste et detection d'edge contre les bookmakers cibles.

Commande :

```powershell
py services/prediction/run_golf_predictions.py
```

Limite volontaire : sans flux statistique joueur golf dedie, le modele ne
pretend pas surperformer le marche par magie. Il cree une base propre pour
ajouter ensuite rankings, forme recente, strokes gained, historique parcours
et profils joueur meteo/parcours.

## Strategie bankroll

Le module `bankroll.py` fournit trois mandats :

- `Prudent` : preservation du capital, cotes longues filtrees, aucun combine.
- `Equilibre` : strategie par defaut, diversification, EV positive, combines rares.
- `Agressif` : croissance offensive, exposition plus forte, variance assumee.

Chaque profil controle Kelly fractionnaire, edge minimum, EV minimum, cote max,
nombre de positions, caps par classe `SAFE/MODERE/RISQUE`, poids par source et
simulation Monte Carlo de drawdown.

## Entrainement XGBoost (optionnel)

Une fois la base peuplee avec assez de fixtures completes (`Cycle complet V1`), lancer :

```powershell
pip install -r services/prediction/requirements.txt
py services/prediction/train_xgboost.py
```

Le script :

- lit les fixtures completes depuis 2015 (par defaut, `--cutoff-year` configurable)
- calcule des features point-in-time (pas de leakage)
- entraine un `XGBClassifier` multi-classe (objectif `multi:softprob`)
- sauve le modele a `services/prediction/models/xgboost_1x2.joblib`

A la prochaine execution du pipeline, le moteur detecte automatiquement le fichier et passe en 4-way blend. Aucun changement de code requis.
