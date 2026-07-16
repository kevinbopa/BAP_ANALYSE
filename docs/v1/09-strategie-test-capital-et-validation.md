# Stratégie de test, capital et validation V1

## 1. Objectif

La V1 ne doit pas être testée comme un simple modèle de prédiction.

Elle doit être testée comme un système de décision avec :

- une logique d'entrée en position ;
- une gestion de capital ;
- des garde-fous de risque ;
- un protocole de validation ;
- un cadre de lecture business.

L'objectif n'est pas de "prouver qu'on gagne tout de suite".
L'objectif est de démontrer que le produit sait :

- sélectionner des opportunités plus propres que le marché moyen ;
- limiter les faux signaux ;
- survivre à la variance ;
- produire une performance traçable et vendable.

## 2. Capital test recommandé

### Capital de départ V1

Capital test recommandé :

- `2 000 CAD`

Structure recommandée :

- `100 unités`
- `1 unité = 20 CAD`

Pourquoi ce niveau :

- assez grand pour absorber une vraie variance football ;
- assez petit pour rester acceptable en phase d'expérimentation ;
- simple à lire en reporting ;
- compatible avec une montée progressive ensuite vers `5 000 CAD`, `10 000 CAD` puis plus.

### Règle fondamentale

Toutes les analyses de performance doivent être exprimées en :

- `unités`
- `% de bankroll`
- `ROI`
- `drawdown`

Le montant en CAD ne doit servir qu'à l'exploitation réelle.
La comparaison de performance doit rester en unités.

## 3. Stratégie test recommandée

## Périmètre

La stratégie V1 testée doit rester volontairement étroite :

- sport : football
- marché : `1X2`
- bookmaker principal : `bet365`
- timing : `pre-match` uniquement
- une seule sélection finale par match

## Segments à privilégier

Le rapport actuel de backtest analytique disponible dans [backtest_latest.json](C:/Users/kev/OneDrive%20-%20Universit%C3%A9%20Laval/Desktop/bp_logiciel/services/prediction/reports/backtest_latest.json:1) montre :

- `10 400` matchs évalués
- `51.47 %` d'accuracy globale
- `54.3 %` en `QUALIFIER`
- `53.1 %` en `CONTINENTAL`
- `50.8 %` en `DOMESTIC`
- `50.6 %` en `FRIENDLY`
- `48.8 %` en `NATIONS_LEAGUE`

En conséquence, la stratégie test V1 doit être segmentée ainsi :

### Phase prioritaire

- qualifications internationales
- grandes compétitions continentales
- Coupe du Monde
- top ligues domestiques si les données pré-match sont complètes

### Phase secondaire

- autres ligues domestiques correctement alimentées

### Phase exclue au début

- amicaux
- segments à faible couverture de données
- matchs avec signaux incomplets ou contradictoires

Cette discipline est importante : le moteur n'a pas besoin de jouer "tout".
Il doit d'abord apprendre où il est réellement fort.

## 4. Filtres d'entrée de la stratégie

Un pari réel ne doit être pris que si les conditions suivantes sont réunies.

### Filtres minimums

- le match est bien couvert par la donnée
- le pipeline a produit une recommandation finale
- `bet365` propose les trois cotes `HOME / DRAW / AWAY`
- une seule sélection est retenue sur le match

### Filtres quantitatifs recommandés

Pour la phase de test réel, appliquer les seuils suivants :

- `confidence_score >= 0.70`
- `surety >= 58 %`
- `edge_probability >= 0.04`
- `expected_value >= 0.02`
- `market_odd >= fair_odd`

### Filtres qualité

Écarter temporairement :

- les longs outsiders déjà filtrés par le failsafe du moteur
- les matchs où le signal dépend d'un énorme edge isolé
- les matchs dont le contexte est instable à quelques minutes du coup d'envoi
- les matchs amicaux tant que leur segment n'est pas validé séparément

### Filtres opérationnels

- maximum `1` bet par match
- maximum `2` bets par compétition et par jour
- maximum `6` unités exposées dans une même journée

## 5. Plan de mise

## Principe

La V1 ne doit pas utiliser une mise agressive.

Le moteur calcule déjà une mise de type `Fractional Kelly`.
Pour une phase test, il faut être plus conservateur que le moteur.

## Règle recommandée

Utiliser :

- `50 %` de la mise Kelly affichée par le système
- avec un plancher à `0.5 unité`
- et un plafond à `2.0 unités`

Exemple :

- Kelly système = `1.6 %` de bankroll
- bankroll = `100 unités`
- mise système = `1.6 unités`
- mise test retenue = `0.8 unité`

### Traduction pratique

Avec `1 unité = 20 CAD` :

- `0.5 unité = 10 CAD`
- `1.0 unité = 20 CAD`
- `2.0 unités = 40 CAD`

## Pourquoi ce plan de mise est bon

- il respecte la qualité relative du signal ;
- il empêche un match isolé de dégrader fortement le capital ;
- il laisse respirer la stratégie pendant la variance naturelle ;
- il produit une courbe de capital crédible pour une démonstration business.

## 6. Règles de risque

### Stop de sécurité

- stop soft à `-12 unités`
- stop dur à `-20 unités`

### Stop comportemental

Mettre la stratégie en pause si l'un des cas suivants arrive :

- `12` pertes consécutives
- `3` semaines consécutives de ROI négatif
- CLV négative persistante
- baisse nette du volume de bons signaux

### Règle de reprise

Après pause :

- relancer en demi-mise
- réévaluer le segment
- vérifier si la contre-performance vient du modèle, du marché ou de la qualité de données

## 7. Protocole de test de la stratégie

La bonne méthode n'est pas de passer directement en argent réel plein régime.

Il faut un protocole en `4` étapes.

## Étape 0. Validation analytique historique

But :

- vérifier la qualité intrinsèque du moteur sans fuite temporelle

Commande actuelle :

```powershell
py services/prediction/run_backtest.py --from 2024-01-01
```

Lecture attendue :

- `accuracy`
- `log_loss`
- `brier`
- calibration par bucket
- performance par famille de compétition

Important :

ce backtest mesure la qualité probabiliste du moteur, pas encore le ROI réel bookmaker, car les cotes historiques complètes ne sont pas disponibles.

## Étape 1. Paper trading

Durée recommandée :

- `30 jours`

Règles :

- aucun argent réel
- exécution quotidienne du pipeline
- archivage des recommandations
- règlement des résultats une fois les matchs terminés

Commandes utiles :

```powershell
py services/prediction/run_prediction_pipeline.py
py services/prediction/run_settle_deals.py
```

Objectif :

- mesurer le comportement réel du ranking
- vérifier le volume de signaux
- vérifier que la sélection finale reste disciplinée

## Étape 2. Test live micro-capital

Durée recommandée :

- `6 à 8 semaines`

Règles :

- capital complet de `100 unités`
- mais mise plafonnée à `0.5 unité` fixe
- uniquement les segments prioritaires

Objectif :

- observer la variance réelle
- valider l'opérabilité
- vérifier la cohérence entre recommandation, exécution et règlement

## Étape 3. Test live contrôlé

Durée recommandée :

- `8 à 12 semaines`

Règles :

- passage au plan `50 % Kelly` capé
- plafond `2 unités`
- maintien des filtres forts

Critères pour entrer dans cette phase :

- paper trading positif ou neutre avec bonne calibration
- micro-capital sans incident opérationnel
- drawdown contenu

## Étape 4. Validation de montée

La stratégie peut être considérée comme prête pour un usage plus ambitieux si elle montre simultanément :

- `>= 150` bets réels réglés
- `ROI > 3 %`
- CLV positive ou au moins neutre
- drawdown compatible avec le mandat de risque
- stabilité sur plusieurs familles de compétitions

## 8. KPI à suivre chaque semaine

La direction produit ou investissement doit recevoir un tableau simple :

- nombre de bets
- nombre de wins / losses / void
- `ROI`
- `yield`
- `hit rate`
- `closing line value`
- profit en unités
- drawdown max
- profit factor
- répartition par compétition
- répartition par type de sélection `HOME / DRAW / AWAY`

## 9. Pourquoi cette stratégie est bonne pour ta boîte

## 1. Elle protège le capital

Une boîte sérieuse ne se juge pas seulement sur sa capacité à détecter des edges.
Elle se juge sur sa capacité à ne pas se détruire pendant la phase d'apprentissage.

Cette stratégie protège la V1 contre :

- la surconfiance ;
- les faux positives ;
- la variance courte ;
- les trous de données.

## 2. Elle transforme le produit en actif vendable

Avec ce protocole, tu ne vends pas une promesse abstraite.
Tu vends :

- une méthode ;
- une discipline ;
- un historique ;
- une logique de risque ;
- une preuve de sérieux.

Pour un client, partenaire ou investisseur, c'est beaucoup plus crédible qu'un simple "notre IA prédit les matchs".

## 3. Elle aligne technique et business

La stratégie proposée s'appuie directement sur ce que le logiciel sait déjà faire :

- calcul de probabilité
- scoring des deals
- filtrage des faux edges
- mise Kelly conservative
- règlement et suivi de performance

Autrement dit, elle ne demande pas un produit différent.
Elle exploite correctement le produit actuel.

## 4. Elle produit des preuves commerciales

Cette approche permet de construire progressivement :

- un track record réel ;
- des courbes de bankroll ;
- des statistiques de segment ;
- des cas d'usage montrables ;
- une story claire pour une vente B2C ou B2B.

## 5. Elle prépare la V2 intelligemment

Une fois cette V1 validée, tu pourras faire évoluer le produit avec beaucoup plus de sécurité :

- nouveaux marchés
- multi-bookmakers
- live betting
- moteur score exact monétisable séparément
- scoring premium par ligue

## 10. Position recommandée

Si je devais fixer une politique officielle V1 pour ta boîte, ce serait :

1. capital test `2 000 CAD`
2. `100 unités` de `20 CAD`
3. paris uniquement sur les meilleurs deals `bet365`
4. priorité aux compétitions internationales et segments les plus propres
5. `30 jours` de paper trading puis montée en `micro live`
6. mise `50 % Kelly`, minimum `0.5u`, maximum `2u`
7. arrêt automatique si drawdown `-20u`

## 11. Conclusion

Cette stratégie est bonne pour ta boîte parce qu'elle cherche d'abord à construire une performance défendable.

Elle ne confond pas :

- précision du modèle ;
- rentabilité des paris ;
- qualité opérationnelle ;
- valeur commerciale.

Et c'est exactement ce qui permet ensuite de vendre le produit plus cher, plus proprement et avec plus de crédibilité.
