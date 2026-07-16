# Sports Prediction Engine

Plateforme d'analyse sportive conçue pour collecter des données, les stocker proprement, produire des probabilités d'événements sportifs et classer les meilleures opportunités parmi les marchés proposés par les bookmakers.

Ce dépôt démarre par une base documentaire volontairement forte afin de cadrer le projet avant l'implémentation.

## Vision

Construire un moteur capable de :

- analyser tous les matchs d'une zone définie par l'utilisateur ;
- collecter des données sportives historiques et live depuis plusieurs API ;
- intégrer des signaux de dernière minute et des facteurs extra-sportifs ;
- normaliser et stocker ces données dans une base relationnelle fiable ;
- générer des probabilités de matchs et de marchés dérivés ;
- comparer ces probabilités aux cotes bookmakers ;
- détecter, scorer et classer les meilleurs deals bookmaker ;
- suivre la performance du modèle et des recommandations.

## Objectifs V1

La V1 reste volontairement simple et pragmatique :

- `Football` uniquement ;
- `Python` pour l'ingestion, le nettoyage et les premiers modèles ;
- `PostgreSQL` comme base principale ;
- `FastAPI` pour exposer les données et les prédictions ;
- `Streamlit` ou `Next.js` pour un premier dashboard ;
- modèle initial de type `Poisson` et/ou `Elo`.

Livrables V1 :

- définition par l'utilisateur d'une zone d'analyse ;
- analyse de tous les matchs entrant dans cette zone ;
- ingestion des équipes, compétitions, matchs et statistiques ;
- couverture des compétitions internationales et des matchs amicaux ;
- historique international exploitable depuis `2010` ;
- stockage des cotes pré-match ;
- génération de probabilités `1X2` uniquement ;
- calcul de value bets et scoring d'opportunité ;
- classement des meilleurs deals par match, sélection et bookmaker ;
- backtesting simple et suivi de performance.

## Architecture cible

Le projet suit la chaîne suivante :

```text
APIs sportives
    -> Collecte
    -> Normalisation
    -> Stockage SQL
    -> Enrichissement contextuel
    -> Feature engineering
    -> Modèles statistiques / ML
    -> Moteur de prédiction
    -> Détection d'opportunités
    -> Classement des deals
    -> API backend
    -> Dashboard utilisateur
```

## Documentation

- [Dossier Version 1](docs/v1/00-index-v1.md)
- [Base de donnees V1](db/README.md)
- [Dashboard V1](apps/dashboard/README.md)
- [Integration Stake](db/docs/stake-integration.md)
- [Vision et portée](docs/01-vision-et-portee.md)
- [Cadrage fonctionnel V1](docs/01b-cadrage-fonctionnel-v1.md)
- [Architecture système](docs/02-architecture-systeme.md)
- [Modèle de données V1](docs/03-modele-de-donnees-v1.md)
- [Pipeline de prédiction](docs/04-pipeline-prediction.md)
- [Roadmap d'implémentation](docs/05-roadmap-implementation.md)
- [Journal des erreurs de développement](docs/06-journal-des-erreurs.md)
- [Moteur algorithmique V1](docs/v1/08-moteur-algorithmique-v1.md)

## Référence officielle V1

Le dossier [Version 1](docs/v1/00-index-v1.md) est désormais la référence principale pour :

- la vision business ;
- les attentes produit ;
- l'architecture cible ;
- la stratégie data et modèle ;
- les exigences de qualité ;
- le plan d'exécution.

## Base locale

Tu n'as pas besoin d'un compte PostgreSQL externe pour travailler sur ce projet.

Le setup local recommandé est PostgreSQL via Docker :

- démarrage : [scripts/setup_local_postgres.ps1](scripts/setup_local_postgres.ps1)
- arrêt : [scripts/teardown_local_postgres.ps1](scripts/teardown_local_postgres.ps1)
- compose : [infra/docker/postgres-compose.yml](infra/docker/postgres-compose.yml)

Rôles locaux principaux :

- `spe_app_rw` : backend / application
- `spe_ingest_rw` : ingestion des fournisseurs
- `spe_readonly` : lecture dashboard / reporting

## Interface web V1

Une console web locale simple est disponible pour piloter la V1 sans passer uniquement par les logs.

Lancement :

```powershell
npm run dev
```

`npm run dev` lance le dashboard en mode developpement avec redemarrage automatique
du serveur et rafraichissement de la page ouverte quand le code change.

Commandes produit utiles :

```powershell
npm start
npm run db:up
npm run cycle
npm run validate:back
npm test
```

Le lancement Python direct reste possible pour debug bas niveau, mais il n'est plus l'entree produit recommandee :

```powershell
py apps/dashboard/app.py
```

Puis ouvrir :

`http://127.0.0.1:8501`

## Stack recommandée

### V1

- `Python`
- `PostgreSQL`
- `FastAPI`
- `Streamlit` ou `Next.js`
- `Docker`

### V2

- `ClickHouse` pour l'analytique massif ;
- `Redis` pour les états live et le cache ;
- `Rust` pour le recalcul temps réel et les simulations rapides ;
- modèles `XGBoost`, `LightGBM`, bayésiens ou hybrides ;
- pipeline de monitoring de modèle et alerting.

## Principes d'architecture

- partir simple, mais garder une structure extensible ;
- séparer ingestion, stockage, modélisation et exposition API ;
- séparer prédiction probabiliste et recommandation de paris ;
- rendre les données auditables à chaque étape ;
- versionner les modèles et les jeux de features ;
- privilégier la reproductibilité avant l'optimisation.

## Structure cible du dépôt

```text
sports-prediction-engine/
|-- apps/
|   |-- api/                    # FastAPI
|   `-- dashboard/              # Streamlit ou Next.js
|-- services/
|   |-- ingestion/              # Connecteurs API sportives
|   |-- normalization/          # Nettoyage et transformation
|   |-- prediction/             # Modeles, features, inference
|   `-- opportunity-engine/     # Value bets, alertes
|-- data/
|   |-- raw/
|   |-- staging/
|   `-- curated/
|-- db/
|   |-- migrations/
|   `-- seeds/
|-- docs/
|-- tests/
`-- infra/
    |-- docker/
    `-- monitoring/
```

## Priorité immédiate

Le meilleur point de départ est :

1. finaliser le schéma PostgreSQL V1 ;
2. formaliser la notion de zone utilisateur et des marchés à analyser ;
3. choisir une première API sportive ;
4. construire le pipeline d'ingestion des équipes, ligues et matchs ;
5. ajouter les statistiques d'équipe, les cotes et les signaux contextuels ;
6. produire un premier modèle Poisson/Elo testable.
