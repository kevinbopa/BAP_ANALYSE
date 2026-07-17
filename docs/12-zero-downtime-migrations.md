# Politique de migrations sans downtime

## Objectif

Faire evoluer la base en ligne sans casser le produit, sans bloquer les
clients et sans imposer de coupure evitables.

## Regle d'or

Toute evolution de schema suit le cycle:

1. `expand`
2. `migrate`
3. `contract`

## Phase 1: expand

On ajoute ce qui manque sans retirer l'existant:

- nouvelles tables ;
- nouvelles colonnes nullable ou avec valeur par defaut sure ;
- nouvelles vues ou index ;
- nouvelles relations non bloquantes.

Interdit en phase `expand`:

- suppression de colonne ;
- renommage brutal d'un champ deja consomme ;
- contrainte immediate qui invalide les anciennes ecritures ;
- changement de type destructif sur une colonne chaude.

## Phase 2: migrate

On rend le code compatible ancien + nouveau schema, puis on deplace la donnee:

- l'application lit l'ancien et le nouveau si besoin ;
- les ecritures commencent a peupler la nouvelle structure ;
- un backfill dedie remplit l'historique ;
- les jobs de verification comparent ancien et nouveau resultat.

Le backfill ne doit pas vivre dans une requete web.

## Phase 3: contract

Une fois la production stable:

- on retire les lectures legacy ;
- on verrouille les nouvelles ecritures ;
- on supprime ensuite l'ancien schema dans une migration separee.

La suppression arrive toujours apres au moins une release stable ayant prouve
que le nouveau chemin fonctionne.

## Deploiement recommande

### Cas standard

1. merger la migration `expand` ;
2. deployer l'application compatible ;
3. lancer le backfill ;
4. verifier les volumes, erreurs et lectures ;
5. deployer la phase `contract` plus tard.

### Cas avec index lourd

- utiliser `CREATE INDEX CONCURRENTLY` en production ;
- eviter les verrous longs sur les tables chaudes ;
- lancer les gros recalculs hors heures de pointe si possible.

## Changements consideres risques

Les operations suivantes exigent un plan explicite:

- `DROP COLUMN`
- `DROP TABLE`
- renommage d'une colonne lue par le dashboard ;
- changement de type non trivial ;
- contrainte `NOT NULL` sur donnees historiques ;
- migration touchant des tables tres sollicitees.

## Rollback

Le rollback standard se fait ainsi:

1. rollback applicatif vers la version precedente ;
2. conservation du schema `expand` si backward-compatible ;
3. suspension du backfill si necessaire ;
4. investigation avant toute suppression de donnees.

On evite autant que possible les rollbacks SQL destructifs en urgence.

## Regles de disponibilite

- l'application web ne doit pas faire de migration au demarrage ;
- les workers d'ingestion et de prediction doivent tolerer une version de
  schema de transition ;
- les vues `reporting` doivent etre stabilisees avant exposition produit ;
- les corrections manuelles en base doivent etre versionnees ensuite.

## Checklist PR pour changement de schema

Une PR base/schema doit repondre a ces questions:

- quelle table ou vue change ?
- quelle etape `expand/migrate/contract` est en cours ?
- le code est-il backward-compatible ?
- un backfill est-il necessaire ?
- quels tests ou smoke checks ont ete lances ?
- quel est le plan de retour arriere ?

## Checklist avant prod

- sauvegarde ou snapshot recent disponible ;
- migration relue ;
- temps de lock estime acceptable ;
- monitoring prevu sur erreurs applicatives ;
- personne responsable du rollout identifiee ;
- plan de communication en cas d'incident.
