# Stratégie données et modèles

## 1. Rôle stratégique de la donnée

La donnée est l'actif principal du produit. La qualité du moteur dépend directement :

- de la couverture des compétitions ;
- de la cohérence des identifiants ;
- de la fraîcheur des mises à jour ;
- de la qualité des historiques ;
- de la capacité à tracer chaque prédiction à ses entrées.

## 2. Périmètre data V1

### Référentiels

- ligues ;
- équipes ;
- saisons ;
- bookmakers.

### Données match

- date et heure ;
- équipes ;
- compétition et type de match ;
- statut ;
- résultat final ;
- scores intermédiaires si disponibles.

### Statistiques pré-match et historiques

- buts marqués ;
- buts encaissés ;
- forme récente ;
- historique domicile / extérieur ;
- séries récentes ;
- historique international depuis `2010` ;
- matchs amicaux internationaux ;
- confrontation historique si utile.

### Données marché

- cote `home`
- cote `draw`
- cote `away`
- bookmaker
- timestamp de capture

### Données contextuelles

- absences disponibles ;
- charge de calendrier ;
- délai depuis le dernier match ;
- signaux de volatilité de cotes.

## 3. Pipeline de qualité de données

Chaque donnée ingérée doit passer par quatre niveaux :

1. `raw`
2. `validated`
3. `normalized`
4. `consumable`

Objectif :

- pouvoir auditer la source ;
- identifier les pertes de qualité ;
- éviter les transformations opaques.

## 4. Tables clés V1

- `analysis_scopes`
- `leagues`
- `teams`
- `fixtures`
- `fixture_scores`
- `team_statistics`
- `odds`
- `predictions`
- `value_bets`
- `deal_rankings`
- `model_runs`

## 5. Philosophie de modélisation V1

La V1 doit être sérieuse et lisible avant d'être complexe.

L'approche recommandée est :

1. baseline statistique robuste
2. comparaison avec une seconde baseline
3. calibration
4. évaluation historique
5. montée en sophistication seulement si le gain est démontré

## 6. Modèles recommandés

### Modèle Poisson

Pourquoi :

- bon point de départ pour le football ;
- interprétable ;
- rapide ;
- bien adapté à la modélisation des buts.

Usage :

- estimer `expected_home_goals` et `expected_away_goals`
- dériver `home/draw/away`

### Modèle Elo

Pourquoi :

- simple à maintenir ;
- bon signal de force relative ;
- utile comme feature et comme benchmark.

### Régression logistique

Pourquoi :

- baseline supplémentaire claire ;
- exploitable directement sur les variables construites ;
- intéressante pour tester la valeur des features non linéaires simples.

## 7. Features minimales V1

- rating Elo des deux équipes ;
- avantage domicile ;
- forme sur les 5 derniers matchs ;
- forme pondérée sur 3 couches temporelles ;
- buts moyens marqués ;
- buts moyens encaissés ;
- puissance offensive relative ;
- fragilité défensive relative ;
- jours de repos ;
- poids de la compétition ;
- signal de variation de cote ;
- qualité de couverture des données.

## 8. Règle de pondération historique V1

Pour le football `1X2`, la V1 applique une lecture en trois couches afin de donner plus d'importance au présent qu'au passé lointain.

Couche 1. `recent`

- environ les `5` derniers matchs ;
- poids principal dans l'analyse ;
- priorise la dynamique immédiate.

Couche 2. `intermediate`

- environ les matchs `6` à `15` ;
- stabilise le signal ;
- évite qu'un pic court domine tout le modèle.

Couche 3. `long_tail`

- historique plus ancien, jusqu'à environ `40` matchs ;
- sert d'ancrage structurel ;
- empêche les sur-réactions.

Règles complémentaires :

- les matchs récents portent plus de poids que les anciens ;
- les grandes compétitions internationales portent plus de poids que les amicaux ;
- les amicaux restent utilisés mais avec un impact réduit ;
- la qualité du snapshot baisse si trop peu de matchs sont disponibles.

## 9. Logique de décision

La chaîne de décision V1 est :

1. estimer une probabilité `1X2`
2. convertir la cote en probabilité implicite
3. calculer l'edge
4. évaluer la confiance
5. produire un ranking

## 10. Métriques d'évaluation

### Qualité probabiliste

- `log loss`
- `Brier score`
- calibration par buckets

### Qualité business

- ROI simulé
- hit rate
- performance du top 5
- performance du top 10
- closing line value si disponible

### Qualité produit

- couverture de matchs analysés
- taux de matchs sans recommandation
- délai moyen de mise à jour

## 11. Règle clé

La V1 ne doit jamais présenter un classement comme une vérité absolue.

Le système doit toujours distinguer :

- `probabilité estimée`
- `opportunité détectée`
- `deal priorisé`

Cette séparation protège à la fois la crédibilité technique et la crédibilité commerciale.
