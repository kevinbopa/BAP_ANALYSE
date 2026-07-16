# Authentification, rôles et bankroll - V1

## Objectif

La V1 passe d'un poste local unique à un poste multi-utilisateur contrôlé.
Chaque client possède son propre espace de lecture, ses prises, ses notes,
son historique de back et sa bankroll. Les administrateurs gardent les droits
d'exploitation : relancer les cycles, superviser les clients et consulter
l'audit.

## Rôles

### Client

Un client peut :

- consulter les prédictions football et golf ;
- consulter les deals et stratégies ;
- prendre une position avec cote réelle et mise ;
- supprimer une position qu'il a prise ;
- consulter son Back/performance personnel ;
- consulter et modifier sa bankroll ;
- voir l'historique de ses mouvements bankroll.

Un client ne peut pas :

- relancer le cycle complet de collecte/prédiction ;
- voir les prises ou la bankroll d'un autre client ;
- accéder à l'administration.

### Admin

Un administrateur peut :

- faire tout ce qu'un client peut faire ;
- relancer les cycles opérationnels ;
- voir la liste des clients ;
- rechercher un client ;
- créer ou mettre à jour un accès client/admin ;
- consulter les positions, mises, bankrolls et derniers événements ;
- ouvrir le Back d'un client précis ;
- consulter l'audit récent.

## Modèle de données

Schema `app_auth` :

- `app_auth.users` : utilisateurs, rôles, statut, dernier login.
- `app_auth.sessions` : sessions serveur hachées, expiration, révocation.
- `app_auth.audit_log` : actions sensibles, acteur, cible, route, metadata.

Tables métier étendues :

- `model.user_bet_annotations.user_id` : notes et état de prise par client.
- `model.user_bet_positions.user_id` : tickets de prise par client.
- `model.user_bet_positions.deleted_at` : suppression logique traçable.
- `model.parlay_tickets.user_id` : combinés par client.
- `model.parlay_tickets.deleted_at` : suppression logique traçable.
- `model.user_bankrolls` : bankroll active par client.
- `model.user_bankroll_events` : ledger bankroll historisé.

## Sécurité V1

- Les mots de passe sont stockés en PBKDF2-SHA256 avec sel unique.
- Les cookies de session sont `HttpOnly` et `SameSite=Lax`.
- Les tokens de session ne sont jamais stockés en clair, seulement hachés.
- Les clients sont isolés par `user_id` dans les lectures de prises et Back.
- Les suppressions sont des soft-deletes pour conserver la traçabilité.
- Le mode test peut désactiver l'auth via `SPE_AUTH_DISABLED=1`.

## Bankroll

La bankroll active est utilisée comme capital de référence dans les stratégies.
Quand un client prend une position, la mise est retirée du solde disponible et
un événement `STAKE_PLACED` est écrit. Quand il supprime une position non
réglée, la mise est réintégrée via `STAKE_VOIDED`.

Ce fonctionnement permet :

- de personnaliser les mises par client ;
- de calculer l'exposition réelle ;
- d'auditer les changements de capital ;
- d'éviter qu'un client voie une stratégie basée sur le budget d'un autre.

## Démarrage local

1. Vérifier `.env` :

```env
SESSION_SECRET=...
BOOTSTRAP_ADMIN_EMAIL=admin@bp-edge.local
BOOTSTRAP_ADMIN_PASSWORD=mot_de_passe_fort
```

2. Appliquer la base :

```powershell
npm run db:up
```

3. Créer ou mettre à jour l'admin :

```powershell
npm run bootstrap:admin
```

4. Lancer l'application :

```powershell
npm run dev
```

## Limites connues V1

- Le règlement bankroll automatique sur pari gagné/perdu doit être branché au
  settlement final pour transformer `STAKE_PLACED` en résultat comptable.
- Le système actuel est une auth applicative locale ; pour une vraie mise en
  production publique, prévoir rotation des secrets, rate limiting login et
  éventuellement MFA admin.
