# Runbook de release

## Objectif

Donner une procedure simple, repetable et propre pour passer une version en
`staging` puis en `prod`, avec le moins de risque et de downtime possible.

## Prerequis

- branche feature mergee vers `main`
- CI verte
- migration SQL versionnee si la base change
- notes de rollout et rollback presentes dans la PR

## Release vers staging

1. Verifier que `main` est a jour.
2. Verifier les variables `staging`.
3. Deployer le code web.
4. Appliquer les migrations compatibles.
5. Lancer les workers ou jobs necessaires.
6. Verifier les parcours critiques:
   - login
   - chargement dashboard
   - lecture reporting
   - prise de position
   - pipeline de prediction
7. Confirmer que les logs et erreurs sont propres.

## Go / no-go prod

Le passage en `prod` est autorise seulement si:

- staging est valide ;
- aucun incident ouvert ne bloque la release ;
- la sauvegarde DB est confirmee ;
- le plan de retour arriere est compris.

## Release vers production

1. Annoncer la release.
2. Verifier le commit exact a deployer.
3. Verifier les secrets `production`.
4. Prendre ou confirmer un backup recent.
5. Deployer le code compatible avec le schema vise.
6. Appliquer la migration `expand` si necessaire.
7. Verifier sante web + DB + workers.
8. Lancer le backfill ou les jobs de transition si prevu.
9. Verifier les parcours critiques.
10. Cloturer la release avec horodatage et responsable.

## Parcours critiques a verifier

- ouverture de session
- acces dashboard principal
- affichage predictions
- affichage deals
- page bankroll
- creation d'un compte admin si la release touche l'auth
- lancement d'un cycle d'ingestion si la release touche les workers

## Rollback applicatif

Si le code pose probleme mais que la base reste compatible:

1. redeployer la version applicative precedente ;
2. arreter le backfill en cours si besoin ;
3. verifier les erreurs ;
4. ouvrir l'incident et documenter la cause.

## Rollback schema

On evite le rollback SQL destructif en urgence.

Si une migration a deja ete appliquee:

- preferer un rollback applicatif ;
- garder le schema `expand` si possible ;
- corriger ensuite via nouvelle migration.

## Post-release

Apres chaque release:

- verifier les logs
- verifier les jobs planifies
- verifier les erreurs DB
- verifier les indicateurs metier de base
- documenter toute anomalie constatee

## Checklist courte

Avant staging:

- [ ] CI verte
- [ ] variables `staging` a jour
- [ ] migration relue

Avant prod:

- [ ] staging valide
- [ ] backup confirme
- [ ] rollback compris
- [ ] responsable de release identifie

Apres prod:

- [ ] smoke checks faits
- [ ] monitoring surveille
- [ ] incident logue si necessaire
