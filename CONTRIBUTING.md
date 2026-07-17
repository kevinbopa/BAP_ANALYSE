# Contribution Guide

Ce projet doit fonctionner comme un produit d'entreprise: changements traces,
revues propres, migrations maitrisees et `main` toujours deployable.

## Regles de base

- pas de push direct sur `main` ;
- tout changement part d'une branche dediee ;
- toute modification visible passe par Pull Request ;
- une PR doit rester ciblee: un sujet principal, un objectif clair ;
- une PR non verte en CI ne part pas en merge ;
- tout changement schema/base doit inclure sa migration et son plan de rollout.

## Convention de branches

Utiliser l'un des prefixes suivants:

- `feature/<sujet>`
- `fix/<sujet>`
- `hotfix/<sujet>`
- `chore/<sujet>`
- `docs/<sujet>`

Exemples:

- `feature/client-ledger-v1`
- `fix/golf-matchup-render`
- `docs/zero-downtime-policy`

## Flux de travail

1. Partir de `main` a jour.
2. Ouvrir une branche de travail.
3. Developper le changement avec tests et migration si necessaire.
4. Executer les verifications locales.
5. Ouvrir une Pull Request vers `main`.
6. Faire reviewer la PR.
7. Merger en `Squash and merge` une fois la CI verte.
8. Supprimer la branche apres merge.

## Verification locale minimale

Avant toute PR:

```powershell
npm run check
```

Si la PR touche la base ou les scripts d'exploitation:

```powershell
npm run db:up
```

## Regles de schema et de donnees

- PostgreSQL est la source de verite.
- Les payloads fournisseurs vivent en `raw`.
- Les donnees normalisees vivent en `core`.
- Les predictions, bankrolls et positions vivent en `model`.
- Les lectures front/API passent par `reporting`.
- Les comptes, sessions et traces sensibles vivent en `app_auth`.

Ne jamais:

- melanger donnees client et payloads API dans une meme table ;
- supprimer une colonne ou table dans le meme deploiement que son remplacement ;
- renommer brutalement un champ consomme en production ;
- ecrire un script manuel non versionne pour corriger la prod.

## Regles de Pull Request

Chaque PR doit expliciter:

- l'objectif produit ou technique ;
- l'impact sur la base ;
- l'impact sur le front ou les clients ;
- la strategie de deploiement ;
- le plan de retour arriere ;
- les tests executes.

Utiliser le template de PR du depot.

## Politique de merge

- merge autorise uniquement apres CI verte ;
- merge en `Squash and merge` ;
- pas de merge de PR en brouillon ;
- pas de bypass manuel des checks sauf incident prod documente.

## Branch protection a activer sur GitHub

Configurer `main` avec:

- Pull Request obligatoire ;
- au moins 1 approbation ;
- status check obligatoire: `Smoke and unit tests` ;
- interdiction du force push ;
- interdiction de suppression de la branche ;
- historique lineaire ou squash obligatoire.

## Incidents et hotfix

Pour un incident production:

1. ouvrir `hotfix/<sujet>` depuis `main` ;
2. faire le correctif minimal ;
3. lancer les checks locaux ;
4. ouvrir une PR prioritaire ;
5. merger puis tagger la release ;
6. documenter l'incident dans `docs/06-journal-des-erreurs.md`.

## Documents de reference

- [docs/11-operating-model.md](docs/11-operating-model.md)
- [docs/12-zero-downtime-migrations.md](docs/12-zero-downtime-migrations.md)
- [db/README.md](db/README.md)
