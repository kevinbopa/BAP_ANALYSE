# Mise en production GitHub

Ce document sert de checklist avant publication du projet Sports Prediction Engine sur GitHub.

## Objectif

Publier le code applicatif sans exposer les secrets, avec une CI minimale qui valide le dashboard, les migrations et les tests prediction/ingestion a chaque push.

## Fichiers a ne jamais publier

- `.env`
- `.env.local`
- fichiers `*.log`
- exports de base de donnees `*.dump`, `*.backup`, `*.db`, `*.sqlite`
- cles privees `*.pem`, `*.key`, `*.p12`
- dossiers locaux `logs/`, `data/`, `.venv/`, `node_modules/`, `graphify-out/`

Le fichier `.env.example` reste publiable parce qu'il ne contient que des placeholders.

## Secrets a configurer dans GitHub plus tard

Dans GitHub, aller dans `Settings -> Secrets and variables -> Actions`, puis ajouter selon l'environnement :

- `SESSION_SECRET`
- `BOOTSTRAP_ADMIN_PASSWORD`
- `ADMIN_SIGNUP_KEY`
- `THESPORTSDB_API_KEY`
- `THEODDS_API_KEY`
- `APIFOOTBALL_KEY`
- `DATAGOLF_API_KEY`
- variables Postgres de production si un serveur externe est utilise

Ne jamais mettre ces valeurs dans le code, le README ou les issues.

## Commandes locales avant push

```powershell
npm run check
```

```powershell
npm run db:up
```

```powershell
npm run cycle
```

## Commandes GitHub initiales

Si le depot Git local est sain :

```powershell
git status
git add .
git commit -m "Prepare GitHub production baseline"
git branch -M main
git remote add origin https://github.com/OWNER/REPO.git
git push -u origin main
```

Dans l'etat actuel du poste local, le dossier `.git` est un reparse point OneDrive non reconnu par Git. Il faut donc soit recuperer l'ancien depot, soit reinitialiser Git proprement avant le premier push.

## CI GitHub

Le workflow `.github/workflows/ci.yml` execute :

- installation Python;
- installation Node;
- dependances ingestion/prediction;
- `npm run check`.

La CI ne lance pas les cycles API pour eviter de consommer les quotas et de dependre de secrets externes.

## Prochaine etape apres GitHub

Pour un vrai deploiement web, il faudra separer :

- le dashboard web deployable;
- les workers ingestion/prediction;
- la base Postgres de production;
- les taches planifiees;
- la gestion des secrets;
- les logs/monitoring.
