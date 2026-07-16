# Roadmap d'implémentation

## Stratégie

Construire d'abord un noyau fiable et testable avant d'ajouter la sophistication temps réel.

## Phase 0. Cadrage

Livrables :

- documentation produit et technique ;
- cadrage V1 sur le football uniquement ;
- définition de la zone d'analyse utilisateur ;
- définition du marché unique `1X2` ;
- décision sur le sport cible V1 ;
- choix de la première API sportive ;
- conventions de nommage et de versionnement.

## Phase 1. Fondation de données

Objectif :

poser une base PostgreSQL saine.

Travaux :

- créer `analysis_scopes` ;
- créer le schéma `leagues`, `teams`, `fixtures`, `fixture_scores`, `team_statistics`, `odds`, `predictions`, `value_bets` ;
- créer `deal_rankings` ;
- mettre en place les migrations ;
- préparer des index et contraintes ;
- définir une stratégie d'upsert.

## Phase 2. Ingestion initiale

Objectif :

alimenter la base automatiquement.

Travaux :

- connecter une API sportive ;
- ingérer ligues et équipes ;
- ingérer les matchs historiques et à venir ;
- stocker les réponses brutes ;
- journaliser les erreurs d'ingestion.

## Phase 3. Statistiques et cotes

Objectif :

obtenir les données nécessaires au premier modèle.

Travaux :

- importer les statistiques de match ;
- importer les cotes pré-match `1X2` ;
- intégrer une première couche de signaux contextuels ;
- historiser les snapshots de cotes ;
- vérifier la couverture par compétition.

## Phase 4. Premier moteur de prédiction

Objectif :

produire des probabilités crédibles.

Travaux :

- construire le dataset d'entraînement ;
- calculer les features de base ;
- implémenter un modèle `Poisson` ;
- ajouter un `Elo` ou une baseline logistique ;
- stocker les sorties `home/draw/away` dans `predictions`.

## Phase 5. Moteur d'opportunités

Objectif :

passer de la probabilité brute à la décision.

Travaux :

- transformer les cotes en probabilités implicites ;
- mesurer l'edge ;
- enregistrer les `value_bets` ;
- définir un seuil minimal de signal ;
- classer les meilleurs deals `1X2` dans `deal_rankings`.

## Phase 6. Backtesting

Objectif :

mesurer la qualité réelle du système.

Travaux :

- comparer les prédictions aux résultats réels ;
- produire les métriques probabilistes ;
- calculer le ROI simulé ;
- analyser la performance par ligue et par marché.

## Phase 7. Backend et dashboard

Objectif :

rendre le moteur exploitable visuellement.

Travaux :

- exposer des endpoints `FastAPI` ;
- afficher les matchs du jour dans une zone choisie ;
- afficher probabilités, cotes, value bets et ranking final ;
- présenter l'historique du modèle.

## Phase 8. Industrialisation

Objectif :

préparer l'échelle future.

Travaux :

- conteneurisation `Docker` ;
- observabilité ;
- tâches planifiées robustes ;
- tests d'intégration ;
- préparation à `Redis`, `ClickHouse` et `Rust`.

## Priorité recommandée des prochains travaux

1. Initialiser le dépôt et la structure de dossiers.
2. Formaliser les règles du ranking sur le marché `1X2`.
3. Créer les migrations PostgreSQL V1.
4. Choisir le fournisseur d'API pour le football.
5. Développer le module d'ingestion `leagues -> teams -> fixtures`.
6. Ajouter ensuite `team_statistics`, `odds` et les signaux contextuels.
