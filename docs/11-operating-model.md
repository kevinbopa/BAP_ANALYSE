# Modele operatoire

## Objectif

Poser un cadre simple et strict pour faire evoluer le produit sans
debordement, avec une base de donnees fiable, un historique propre et une
production disponible.

## Principes directeurs

- `main` reste deployable a tout moment.
- PostgreSQL est la source de verite unique.
- un changement de schema est un changement produit, pas un detail technique.
- l'application web, l'ingestion et la prediction doivent pouvoir evoluer sans
  se bloquer mutuellement.
- la production ne doit pas dependre d'actions manuelles invisibles.

## Separation des responsabilites

### Base de donnees

- `ops` : fournisseurs, runs, statuts, erreurs, audit technique.
- `raw` : payloads bruts des API pour replay et investigation.
- `core` : donnees metier normalisees.
- `model` : predictions, deals, bankrolls, prises, runs de modeles.
- `reporting` : vues stables consommees par le dashboard et plus tard l'API.
- `app_auth` : utilisateurs, sessions, audit sensible.

### Services

- `services/ingestion` : collecte et normalisation source.
- `services/prediction` : calculs, classements, settlement.
- `apps/dashboard` : interface operateur et client.
- `db/migrations` : evolution versionnee du contrat de donnees.

## Regles de modelisation

Une nouvelle donnee doit suivre cet ordre de decision:

1. Si elle vient d'une API et doit rester auditable, elle va en `raw`.
2. Si elle sert au metier, elle va en `core`.
3. Si elle est calculee, elle va en `model`.
4. Si elle est lue par le produit, elle doit idealement sortir via `reporting`.
5. Si elle appartient a un client, elle doit etre reliee a `user_id`.

## Environnements

### Dev

- experimentation locale ;
- migrations et features en construction ;
- donnees jetables ou seed de travail.

### Staging

- environnement le plus proche possible de la production ;
- verification des migrations ;
- test du deploiement et du rollback applicatif ;
- test des flux ingestion/prediction sur donnees controlees.

### Prod

- environnement protegee ;
- acces limites ;
- changements uniquement via PR mergee ;
- aucune intervention schema hors migration versionnee sauf incident majeur.

## Types de changements

### Changement applicatif

Exemple: nouveau composant dashboard, nouveau filtre, nouveau endpoint.

Attendus:

- tests ou smoke checks adaptes ;
- pas de casse des vues `reporting`.

### Changement data

Exemple: nouvelle source API, nouvelle table metier, nouvelle vue.

Attendus:

- migration SQL ;
- documentation de schema ;
- plan de remplissage si donnees historiques.

### Changement modele

Exemple: nouveau score, nouvelle calibration, nouvelle logique de deal.

Attendus:

- version de run identifiable ;
- impact explicite sur les tables `model` ou `reporting` ;
- tests de regression.

## Regles de release

- une release mergee sur `main` doit etre installable sans patch manuel ;
- une release ne doit pas supposer un downtime complet pour une simple
  evolution de schema ;
- toute release sensible doit indiquer son plan de retour arriere.

## Gouvernance GitHub

- protection de `main` ;
- PR obligatoire ;
- CI obligatoire ;
- merge en `Squash and merge` ;
- suppression des branches mergees ;
- historique des decisions dans le repo, pas dans des messages epars.

## Definition d'une PR propre

Une PR est consideree propre si:

- le perimetre est limite ;
- le message explique le pourquoi ;
- la base est traitee explicitement ;
- les tests ont ete lances ;
- le rollout et le rollback sont compris ;
- la CI passe sans bypass.

## Definition du succes

Nous sommes alignes si:

- un nouveau dev peut comprendre comment contribuer en 10 minutes ;
- une migration n'impose pas une coupure brutale ;
- une PR ne surprend ni la base, ni le front, ni l'operateur ;
- `main` reste une branche de confiance.
