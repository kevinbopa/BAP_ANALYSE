# Vision et portée

## But du projet

Le `Sports Prediction Engine` est un système d'aide à la décision orienté analyse sportive et détection d'opportunités de pari. Son rôle n'est pas uniquement de "prédire un score", mais de transformer des données sportives, contextuelles et de marché en recommandations exploitables, mesurables et améliorables.

## Problèmes que le système doit résoudre

- agréger des données issues de plusieurs fournisseurs, souvent hétérogènes ;
- analyser un univers de matchs défini par l'utilisateur ;
- conserver un historique fiable des matchs, statistiques et cotes ;
- produire des probabilités cohérentes pour plusieurs marchés ;
- intégrer des informations de dernière minute ;
- intégrer des facteurs extra-sportifs quand une source structurée existe ;
- recalculer rapidement les probabilités quand le contexte change ;
- classer les meilleurs deals parmi les offres bookmakers ;
- mesurer objectivement la qualité des prédictions et recommandations ;
- exposer les résultats dans une interface simple à utiliser.

## Valeur métier

Le système doit permettre de :

- identifier des value bets ;
- hiérarchiser les opportunités selon leur qualité attendue ;
- suivre la performance d'un modèle sur la durée ;
- comparer plusieurs approches de modélisation ;
- préparer une future extension vers le live trading / live betting ;
- industrialiser la recherche quantitative sportive.

## Portée V1

La V1 couvre :

- une seule discipline sportive : le football ;
- des données pré-match et historiques ;
- une première couche de signaux de dernière minute ;
- une seule base de données principale : `PostgreSQL` ;
- un backend `FastAPI` ;
- un premier dashboard ;
- un ou deux modèles simples et interprétables ;
- un premier moteur de scoring et classement des deals.

## Hors périmètre V1

Les éléments suivants sont volontairement reportés :

- arbitrage multi-bookmakers en temps réel ;
- ingestion live à forte fréquence ;
- moteur `Rust` en production ;
- architecture distribuée complexe ;
- trading automatisé ou exécution d'ordres ;
- optimisation cloud avancée.

## Critères de réussite

Le projet sera sur de bonnes bases si la V1 permet :

- d'ingérer automatiquement les matchs et statistiques ;
- de filtrer un périmètre d'analyse défini par l'utilisateur ;
- de stocker proprement les cotes et résultats ;
- de recalculer des prédictions de manière reproductible ;
- de produire des probabilités fiables sur le marché `1X2` ;
- de classer les opportunités par qualité attendue ;
- d'évaluer les modèles avec des métriques fiables ;
- d'afficher les signaux utiles dans un dashboard lisible.

## Indicateurs clés

- taux de couverture des données par compétition ;
- fraîcheur des données ;
- temps d'ingestion ;
- précision probabiliste (`log loss`, `Brier score`) ;
- qualité du ranking des deals ;
- rentabilité simulée des value bets ;
- latence de calcul des prédictions ;
- disponibilité de l'API backend.
