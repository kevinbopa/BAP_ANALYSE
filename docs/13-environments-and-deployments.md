# Environnements et deploiements

## Objectif

Definir une structure simple pour `dev`, `staging` et `prod`, afin de faire
evoluer le produit sans confusion sur les secrets, les roles, les donnees et
les procedures de mise en ligne.

## Environnements cibles

### Dev

Usage:

- developpement local ;
- tests fonctionnels rapides ;
- migrations en construction ;
- essais de connecteurs API.

Contraintes:

- base locale Docker ou instance jetable ;
- secrets locaux seulement ;
- donnees rejouables ;
- aucune exigence de haute disponibilite.

### Staging

Usage:

- validation avant production ;
- test des migrations ;
- verification des taches planifiees ;
- recette des parcours critiques.

Contraintes:

- environnement proche de la production ;
- variables separees de `prod` ;
- jeux de donnees controles ;
- acces plus restreint que `dev`.

### Prod

Usage:

- service reel ;
- donnees clients ;
- historique d'exploitation ;
- monitoring et audit.

Contraintes:

- secrets isoles ;
- base sauvegardee ;
- changements via PR mergee uniquement ;
- migration versionnee obligatoire ;
- rollback applicatif prepare.

## Composants a separer

Le produit doit etre pense comme quatre briques distinctes:

1. `web` : dashboard et parcours utilisateur.
2. `db` : PostgreSQL, source de verite.
3. `workers` : ingestion, prediction, validation, settlement.
4. `scheduler` : declenchement des cycles et backfills.

Le dashboard ne doit pas porter seul toute l'exploitation a long terme.

## Topologie recommandee

### Dev

- `web` sur poste local ;
- `db` via Docker local ;
- `workers` lances manuellement ;
- `scheduler` manuel.

### Staging

- `web` deploye sur une cible dediee ;
- `db` PostgreSQL dedie ou base managée dediee ;
- `workers` dedies ;
- `scheduler` separe ;
- secrets stockes dans GitHub Environments ou l'infra cible.

### Prod

- `web` deploye separement ;
- `db` PostgreSQL managé ou serveur dedie sauvegarde ;
- `workers` hors processus web ;
- `scheduler` hors processus web ;
- secrets en coffre ou variables d'environnement protegees.

## Strategie de donnees par environnement

### Dev

- donnees locales ;
- reset acceptable ;
- tests de migration libres.

### Staging

- donnees de recette ou sous-ensemble controle ;
- pas de donnees client reelles si possible ;
- backfills testes avant prod.

### Prod

- donnees client reelles ;
- journalisation des runs ;
- aucune correction manuelle sans trace ;
- sauvegarde avant changement sensible.

## Variables et secrets

Les variables doivent etre decoupees ainsi:

- variables communes applicatives ;
- variables DB application ;
- variables DB ingestion ;
- secrets d'auth ;
- cles fournisseurs ;
- variables de quotas et de fenetres.

Les templates de reference sont:

- [infra/env/staging.env.example](../infra/env/staging.env.example)
- [infra/env/production.env.example](../infra/env/production.env.example)

## GitHub Environments recommandes

Configurer au minimum:

- `staging`
- `production`

Pour chaque environnement:

- secrets distincts ;
- approbation manuelle pour `production` ;
- variables de deploiement specifiques ;
- historique des runs conserve.

## Checks minimum avant deploiement

Avant `staging`:

- `npm run check`
- migration relue
- template d'environnement a jour

Avant `prod`:

- CI verte ;
- staging valide ;
- sauvegarde DB disponible ;
- plan de rollout documente ;
- plan de rollback documente ;
- personne responsable identifiee.

## Politique de deploiement

- `dev` : libre
- `staging` : a chaque PR importante ou regroupement de features
- `prod` : depuis `main` uniquement

## Ce que nous n'acceptons pas

- meme secret partage entre `staging` et `prod` ;
- migration lancee depuis le processus web au demarrage ;
- workers critiques executes depuis l'interface utilisateur ;
- deploiement prod sans verification staging pour un changement base ;
- correctif prod non reporte dans le repo.
