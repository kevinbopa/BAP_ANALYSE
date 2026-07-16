# Moteur algorithmique V1

## Objet

Ce document formalise la logique d'analyse V1 du projet.

## Principe

La V1 ne repose pas sur un seul algorithme. Elle repose sur une pile de decision simple et defendable :

1. modele de prediction
2. modele de pricing
3. modele de decision
4. modele de gestion du risque

## Pile retenue pour la V1

### 1. Prediction model

Le coeur V1 combine :

- `Dixon-Coles / Poisson`
- `Elo`
- pondération temporelle en `3 couches`
- ajustement du poids selon le type de compétition

Pourquoi :

- le football se prete tres bien a une modelisation de buts
- Elo apporte une lecture robuste de la force relative
- les matchs recents doivent peser davantage
- les grandes competitions internationales doivent compter plus que les amicaux
- l'ensemble est interpretable
- le cout de calcul reste faible

### 2. Pricing model

Le moteur transforme les cotes `1X2` en probabilites implicites puis retire la marge bookmaker.

Objectif :

- comparer modele vs marche
- identifier la vraie valeur relative d'une selection

### 3. Decision model

Le moteur calcule :

- edge probabiliste
- fair odd
- expected value

Puis il decide :

- `bet`
- `no bet`

selon des seuils de confiance et de rentabilite attendue.

### 4. Risk model

La V1 utilise une version prudente de `Fractional Kelly`.

Objectif :

- eviter les sur-mises
- prioriser les meilleurs signaux
- encadrer le risque si les probabilites sont imparfaites

## Architecture de calcul

```text
Donnees normalisees
    -> Filtrage competitions internationales et amicaux
    -> Historique international depuis 2010
    -> Features de match
    -> Ponderation temporelle 3 couches
    -> Ajustement par importance de competition
    -> Poisson / Dixon-Coles
    -> Elo
    -> Ensemble probabiliste
    -> Probabilites implicites du marche
    -> Expected Value
    -> Fractional Kelly
    -> Ranking final
```

## Composants implementes

Le service de prediction se trouve dans :

- [services/prediction/README.md](../../services/prediction/README.md)
- [engine.py](../../services/prediction/src/spe_prediction/engine.py)
- [poisson.py](../../services/prediction/src/spe_prediction/poisson.py)
- [elo.py](../../services/prediction/src/spe_prediction/elo.py)
- [market.py](../../services/prediction/src/spe_prediction/market.py)
- [decision.py](../../services/prediction/src/spe_prediction/decision.py)

## Sorties V1

Pour chaque match, le moteur peut produire :

- `probabilite home`
- `probabilite draw`
- `probabilite away`
- `expected_home_goals`
- `expected_away_goals`
- `fair odds`
- `implied probabilities`
- `expected value`
- `fractional kelly`
- `ranking score`

## Regles de priorisation historique

Le moteur V1 lit l'historique en trois bandes :

1. court terme : dynamique immediate
2. moyen terme : stabilisation du niveau
3. long terme : ancrage de reference

Principes :

- les `5` derniers matchs ont le poids le plus fort ;
- les matchs `6` a `15` servent de couche d'equilibre ;
- le reste de la fenetre historique agit comme socle ;
- les competitions majeures internationales recoivent un bonus de poids ;
- les amicaux internationaux restent utiles mais sont penalises par rapport aux tournois competitifs.

## Ce qui est volontairement reporte

- `XGBoost / LightGBM`
- modeles bayesiens hierarchiques
- simulation Monte Carlo massive
- inference live minute par minute
- calibration avancee multicouches

## Trajectoire recommandee

### V1.0

- Poisson / Dixon-Coles
- Elo
- implied odds
- EV
- Fractional Kelly

### V1.1

- calibration isotonic ou Platt
- plus de features de contexte
- meilleure logique de score de confiance

### V2

- XGBoost ou LightGBM
- ensemble learning
- composant bayesien
- simulation de scenarios
