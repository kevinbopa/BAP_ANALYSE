# Database Layer

Cette couche contient la base PostgreSQL V1 du projet `Sports Prediction Engine`.

## Objectif

Fournir une base :

- proprement normalisee pour le football ;
- prete pour l'ingestion depuis `TheSportsDB` ;
- extensible vers les predictions `1X2` ;
- securisee avec une separation claire des schemas et des roles ;
- exploitable pour l'API, le backtesting et le dashboard.

## Architecture des schemas

- `ops` : configuration technique, fournisseurs, endpoints, journal d'ingestion.
- `raw` : payloads JSON bruts recus depuis TheSportsDB.
- `core` : entites metier normalisees.
- `model` : predictions, value bets, rankings, traces de runs.
- `reporting` : vues de lecture pour l'API et le dashboard.

## Vues de lecture incluses

- `reporting.v_latest_predictions_1x2`
- `reporting.v_match_board_1x2`
- `reporting.v_latest_odds_1x2`
- `reporting.v_ranked_deals_1x2`
- `reporting.v_track_record`
- `reporting.v_parlay_board`
- `reporting.v_scorer_board`
- `reporting.v_golf_board`

## Fichiers

- [db/migrations/0001_v1_baseline.sql](./migrations/0001_v1_baseline.sql) : schema complet V1.
- [db/migrations/0002_v1_security.sql](./migrations/0002_v1_security.sql) : roles, revokes et grants.
- [db/migrations/0003_v1_stake_provider.sql](./migrations/0003_v1_stake_provider.sql) : enregistrement du provider Stake.
- [db/migrations/0004_v1_hardening.sql](./migrations/0004_v1_hardening.sql) : hardening operationnel, triggers et contraintes avancees.
- [db/migrations/0005_v1_prediction_security.sql](./migrations/0005_v1_prediction_security.sql) : ajustement minimal des droits pour le pipeline de prediction.
- [db/migrations/0006_v1_stake_odds_contract.sql](./migrations/0006_v1_stake_odds_contract.sql) : contrat officiel Stake odds-data et endpoints reels.
- [db/migrations/0007_v1_theoddsapi_provider.sql](./migrations/0007_v1_theoddsapi_provider.sql) : provider principal The Odds API v4.
- [db/migrations/0017_v1_annotation_kind_scope.sql](./migrations/0017_v1_annotation_kind_scope.sql) : separation des annotations utilisateur par type de recommandation.
- [db/migrations/0018_v1_taken_price_and_real_profit.sql](./migrations/0018_v1_taken_price_and_real_profit.sql) : cote prise, mise reelle et profit reel des positions utilisateur.
- [db/migrations/0019_v1_position_tickets_and_pronostic_markets.sql](./migrations/0019_v1_position_tickets_and_pronostic_markets.sql) : carnet de prises multiples et marches pronostic libres.
- [db/migrations/0020_v1_parlay_positions.sql](./migrations/0020_v1_parlay_positions.sql) : tickets combines pris, jambes et vue de suivi.
- [db/migrations/0021_v1_scorer_market.sql](./migrations/0021_v1_scorer_market.sql) : cotes/pronostics/deals du marche buteur anytime.
- [db/migrations/0022_v1_golf_markets.sql](./migrations/0022_v1_golf_markets.sql) : tournois, joueurs, cotes, predictions et deals golf.
- [db/migrations/0023_v1_golf_context.sql](./migrations/0023_v1_golf_context.sql) : lieux, meteo et facteurs de contexte des tournois golf.
- [db/docs/thesportsdb-mapping.md](./docs/thesportsdb-mapping.md) : mapping TheSportsDB -> tables PostgreSQL.
- [db/docs/theoddsapi-integration.md](./docs/theoddsapi-integration.md) : cadrage integration The Odds API.
- [db/docs/stake-integration.md](./docs/stake-integration.md) : cadrage integration Stake.

## Portee V1

- sport : `Soccer` et premiere extension `Golf`
- marches : `1X2`, `OU25`, `BTTS`, `EXACT_SCORE`, combines, buteur anytime,
  golf `TOURNAMENT_WINNER`, `TOP_3`, `TOP_5`, `TOP_10`, `TOP_20`, `ROUND_WINNER`
- source primaire : `TheSportsDB`

## Point important

TheSportsDB documente les ligues, equipes, matchs, statistiques, lineups et classements, mais pas un flux bookmaker suffisant. Les cotes pre-match, derivees et buteur sont donc alimentees principalement par `The Odds API`.

## Ordre recommande d'execution

1. Executer `0001_v1_baseline.sql`
2. Executer `0002_v1_security.sql` avec un compte admin PostgreSQL
3. Executer `0003_v1_stake_provider.sql`
4. Executer `0004_v1_hardening.sql`
5. Executer `0005_v1_prediction_security.sql`
6. Executer `0006_v1_stake_odds_contract.sql`
7. Executer `0007_v1_theoddsapi_provider.sql`
8. Creer les secrets applicatifs hors du code
9. Brancher ensuite le service d'ingestion TheSportsDB et The Odds API

## Demarrage local sans compte PostgreSQL

Tu n'as pas besoin d'un compte PostgreSQL externe.

Le projet peut tourner avec PostgreSQL en local via Docker :

1. lancer `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/setup_local_postgres.ps1`
2. laisser `/.env` tel qu'il est pour l'utilisateur applicatif `spe_app_rw`
3. utiliser Docker comme hote PostgreSQL local

Le script :

- demarre PostgreSQL local
- applique toutes les migrations
- configure les mots de passe des roles applicatifs

Roles recommandes :

- `spe_ingest_rw` pour l'ingestion
- `spe_app_rw` pour l'application
- `spe_readonly` pour les lectures

## Principes de conception

- conserver les payloads bruts en `raw`
- normaliser les donnees en `core`
- ne jamais melanger donnees sources et donnees modelisees
- versionner les predictions et les runs
- exposer l'application via des vues `reporting`

## Hardening ajoute

- triggers automatiques `updated_at`
- contraintes de coherence temporelle
- unicite d'une seule saison courante par ligue
- unicite d'une seule `closing line` par fixture et bookmaker
- revocation explicite des privileges `PUBLIC` sur tables et sequences
