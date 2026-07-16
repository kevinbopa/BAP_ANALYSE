# Dashboard V1

Cette interface web sert de console d'exploitation simple pour la version 1.

## Ce qu'elle fait

- lance la synchronisation `TheSportsDB`
- lance la synchronisation `The Odds API`
- lance le pipeline de prediction
- affiche les metriques de la base
- affiche les matchs a venir
- affiche les meilleurs deals classes
- affiche les probabilites `1X2`, scores exacts, `BTTS` et `+/-2,5 buts`
- permet de prendre une position avec cote reelle et mise
- permet de prendre plusieurs tickets sur une meme position
- propose et enregistre des combines depuis la page strategie
- suit les positions et combines dans `/back`
- affiche le marche buteur anytime sur les pages match quand les cotes existent
- expose `/bankroll` pour les profils `Prudent`, `Equilibre`, `Agressif`
- expose le mode Golf via `/?sport=golf` avec tournois, outrights et deals golf

## Lancement

Depuis la racine du projet :

```powershell
npm run dev
```

Commandes utiles :

```powershell
npm run db:up
npm run cycle
npm run validate:back
npm test
```

Puis ouvrir :

`http://127.0.0.1:8501`

Le lancement Python direct reste disponible pour debug interne :

```powershell
py apps/dashboard/app.py
```

## Prerequis

- Docker actif pour PostgreSQL local
- fichier [`.env`](C:/Users/kev/OneDrive%20-%20Universit%C3%A9%20Laval/Desktop/bp_logiciel/.env) correctement rempli
- base initialisee via :

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/setup_local_postgres.ps1
```

## Parcours V1 recommande

1. cliquer `Sync football`
2. cliquer `Sync cotes 1X2`
3. cliquer `Lancer predictions`
4. lire les `Predictions` et `Deals classes`
5. ouvrir une `Analyse` match pour voir les scores exacts, value bets et buteurs
6. enregistrer les prises reelles via le bouton `Prise`
7. suivre la performance dans `/back`
8. construire une strategie bankroll dans `/bankroll`

## Parcours Golf V1

Depuis le dashboard, cliquer `Golf`, puis :

1. cliquer `Sync cotes golf`
2. cliquer `Predictions golf`
3. filtrer par tournoi
4. lire les probabilites, cotes justes et deals golf

Le cycle complet est aussi disponible via `Cycle golf complet`.

## Note

Le dashboard reste local, mais il couvre maintenant le cycle produit V1 avance :
ingestion, prediction, prise de position, back/performance, strategie de mise,
combines et marche buteur.
