# Journal des erreurs de développement

Registre des erreurs rencontrées pendant le développement de la V1, avec cause racine, correctif et leçon. À consulter avant de déboguer : plusieurs « bugs » apparents sont des limites de providers déjà documentées ici.

Format : **Symptôme → Cause racine → Correctif → Leçon**

## Statut de résolution (audit du 2026-07-03)

Chaque erreur a été re-vérifiée contre l'état réel du système (93 tests, requêtes SQL sur la base, contenu servi par le dashboard).

| # | Erreur | Statut | Preuve de résolution |
|---|---|---|---|
| E01 | Blend qui invente sans données | ✅ Résolu | Tests `test_market_anchoring_and_calibration` + `test_argentina_stacking_regression` ; board différencié en prod |
| E02 | Confiance saturée à ~50 % | ✅ Résolu | Confiances observées en prod : 53 → 98 |
| E03 | Stacking des deals | ✅ Résolu | Dernier run : 10 deals, 8 matchs distincts (vérifié SQL) |
| E04 | Bonus domicile en terrain neutre | ✅ Résolu | `elo.py` proportionnel à `home_advantage` + tests |
| E05 | Elo aveugle à l'adversaire | ✅ Résolu | `rating.py` rejoue 28 310 matchs ; ratings réels au dashboard (Spain 1892...) |
| E06 | bet365 absent de The Odds API | ✅ Résolu (architecture) | 41 books ingérés ; deals du dernier run = 100 % Pinnacle (vérifié SQL) |
| E07 | Caps de la clé gratuite TheSportsDB | ✅ Résolu | Clé Premium active ; 30 773 fixtures en base |
| E08 | Sync jamais lancée / non journalisée | ✅ Résolu le 2026-07-03 | Journalisation `ops.ingestion_runs` ajoutée (RUNNING→SUCCESS/FAILED avec rollback préalable) ; testée contre la base réelle |
| E09 | Payload premium sans `strCountry` | ✅ Résolu | 33 ligues admises ; test `test_premium_slim_league_payload...` |
| E10 | Intrusions féminines/clubs/jeunes | ✅ Résolu | Listes d'exclusion + tests dédiés |
| E11 | API-Football Free limité à 2022-2024 | ✅ Contourné (limite provider) | Skip gracieux testé en prod ; effectifs OK (1 381 joueurs) ; blessures temps réel = tier payant — **limite documentée, pas un bug** |
| E12 | « England Women vs England Women » | ✅ Résolu | Gardes événement + test reproduisant le cas |
| E13 | Horaire malformé `15:00:00:00` | ✅ Résolu | Parsing tolérant + test reproduisant le cas prod |
| E14 | Commit unique = tout perdre | ✅ Résolu | Ronde 5 complète (33 ligues) sans perte |
| E15 | Fixtures/équipes synthétiques | ✅ Résolu | 0 fixture et 0 équipe à ID négatif en base (vérifié SQL) |
| E16 | Doublon par ponctuation | ✅ Résolu | 0 doublon parmi les fixtures à venir (vérifié SQL) |
| E17 | Mauvais rôle Postgres | ✅ Résolu | Ingestion API-Football exécutée avec succès (90 requêtes) |
| E18 | BOM PowerShell → Python | ✅ Résolu (pratique) | Fichiers inter-outils écrits côté Python |
| E19 | Dérive `.env` / `.env.example` | ✅ Résolu | Parité vérifiée par diff des noms de variables |
| E20 | Serveur dashboard obsolète | ✅ Résolu | Serveur relancé ; contenu vérifié par curl : 0 ligne 2018, tri chronologique |
| E21 | Double instance sur le port 8501 | ✅ Résolu le 2026-07-03 | `allow_reuse_address=False` + message d'erreur clair ; double lancement testé (code retour 1) ; warnings Pylance réglés (`.vscode/settings.json`) |
| E22 | `btts` refusé par le bulk `/odds` (422) | ✅ Résolu le 2026-07-04 | Bulk `h2h,totals` + endpoint par événement pour le btts (≤72 h) ; 118 cotes O/U + 78 BTTS en base |
| E23 | Compteurs du résumé d'ingestion écrasés | ✅ Résolu le 2026-07-04 | Incréments via `dataclasses.replace` ; résumé re-vérifié : `totals_odds_written: 118, btts_odds_written: 78` |

---

## 1. Moteur de prédiction

### E01 — Toutes les prédictions World Cup identiques (Jordan 18.7 % contre l'Argentine)
- **Symptôme** : probabilités quasi identiques sur tous les matchs, faux edges énormes (+14.9 %), les mêmes xG partout.
- **Cause racine** : sans historique d'équipe (`data_quality=0`), le blend gardait 57 % du poids sur Poisson/Elo qui tournaient sur des valeurs par défaut. Le modèle « inventait » des probabilités.
- **Correctif** : poids de marché adaptatif (plafond 0.95 quand consensus solide + zéro historique), `engine.py:_adaptive_market_weight`.
- **Leçon** : un modèle sans données doit s'écraser devant le marché, pas improviser.

### E02 — Confiance collée à ~50 % sur tous les matchs
- **Symptôme** : colonne Confiance entre 44.9 et 51.8 partout.
- **Cause racine** : formule de confiance saturée — les termes se compensaient autour de 0.5 quel que soit le match.
- **Correctif** : réécriture avec `dominance_gap` (écart 1er/2e choix) + plancher épistémique dépendant du signal (`engine.py:_estimate_confidence`).
- **Leçon** : tester la DISPERSION d'un score, pas seulement sa valeur moyenne.

### E03 — Un seul match monopolisait le classement des deals (8× Jordan-HOME)
- **Symptôme** : le top deals = le même pari décliné sur 8 bookmakers.
- **Cause racine** : chaque bookmaker génère un deal ; un faux edge se duplique par le nombre de books. Aucune pénalité sur les edges invraisemblables.
- **Correctif** : plafond 2 deals par (match, sélection) ; crédibilité d'edge (décroissance au-dessus de 8 %, rejet > 18 %) ; failsafe longshot (implied < 12 % ET edge > 4 % = refus) ; `DENSE_RANK` par fixture dans le dashboard.
- **Leçon** : un edge « trop beau » est presque toujours une erreur de NOTRE modèle, pas du bookmaker.

### E04 — Bonus domicile Elo appliqué en Coupe du Monde (terrain neutre)
- **Symptôme** : Elo donnait 44.8 % à Jordan-HOME uniquement grâce au bonus domicile fixe de 55 pts.
- **Correctif** : bonus proportionnel à `fixture.home_advantage` (WC = 0.04 → ~22 % du bonus normal), `elo.py`.
- **Leçon** : tout paramètre « constant » doit être questionné par contexte de compétition.

### E05 — Elo aveugle à la force de l'adversaire
- **Symptôme** : la Jordanie (points engrangés contre des équipes faibles) notée comme la France.
- **Correctif** : Elo itératif rejouant tout l'historique chronologiquement (`rating.py`) — battre un fort rapporte plus. K pondéré par compétition et marge.
- **Leçon** : une moyenne de points sans référence à l'adversaire n'est pas un rating.

## 2. Providers de données

### E06 — bet365 introuvable via The Odds API (`odds_written: 0`)
- **Cause racine** (double) : `THEODDS_API_REGIONS=eu` alors que bet365 vit dans `uk`/`au` ; ET bet365 a été retiré de The Odds API (bloque les agrégateurs). Vérifié : 41 bookmakers reçus, zéro bet365.
- **Correctif** : régions `uk,eu` ; ingestion multi-books (le consensus l'exige) ; ciblage bookmaker déplacé au niveau deals (`SPE_TARGET_BOOKMAKER=pinnacle`).
- **Leçon** : vérifier la couverture réelle d'un provider AVANT de verrouiller l'architecture dessus.

### E07 — Clé gratuite TheSportsDB : données tronquées silencieusement
- **Symptôme** : « on est supposé avoir des données en masse » — mais 5 ligues, 44 fixtures.
- **Cause racine** : la clé `123` tronque TOUT : `all_leagues` → 10 ligues, `search_all_seasons` → les 5 saisons les plus ANCIENNES (WC : 1930-1954 !), `eventsseason` → 15 événements/saison.
- **Correctif** : clé Premium + contournement `THESPORTSDB_EXTRA_LEAGUE_IDS` (IDs vérifiés un à un via `lookupleague.php`).
- **Leçon** : les tiers gratuits tronquent sans le dire — sonder les VOLUMES retournés, pas juste le code HTTP 200.

### E08 — La sync TheSportsDB n'avait jamais tourné (et ne se journalisait pas)
- **Symptôme** : équipes à Elo 1500, « peu d'historique pour Portugal ».
- **Cause racine** : `ops.ingestion_runs` ne contenait que des runs The Odds API — le code d'ingestion internationale existait mais n'avait jamais été exécuté. Aggravant : l'ingesteur TheSportsDB n'écrivait AUCUN run dans le journal, rendant l'oubli invisible (carte « Pilotage » vide).
- **Correctif** : sync exécutée (5 rondes) ET journalisation ajoutée à l'ingesteur (`_start_run`/`_finish_run` : RUNNING → SUCCESS/FAILED avec `error_message`, rollback préalable en cas de crash pour pouvoir écrire le statut). La carte « Pilotage » du dashboard affichera désormais chaque sync.
- **Leçon** : vérifier les JOURNAUX D'EXÉCUTION avant de déboguer le code — et s'assurer que chaque ingesteur en écrit.

### E09 — Liste premium `all_leagues.php` : payload allégé sans `strCountry`
- **Symptôme** : ronde 2 de sync identique à la ronde 1 — zéro ligue internationale admise malgré 661 visibles.
- **Cause racine** : la liste premium n'inclut ni pays ni année de création ; notre filtre exigeait un pays international pour tester les mots-clés.
- **Correctif** : quand le pays est absent, les mots-clés décident seuls.
- **Leçon** : ne jamais supposer qu'un champ présent en tier gratuit l'est aussi en premium (et inversement).

### E10 — Compétitions féminines / clubs / jeunes aspirées par les mots-clés
- **Symptôme** : « Womens World Cup Qualifying », « CAF Confederation Cup » (clubs), « FIFA U-17 World Cup » admises — elles contiennent « world cup ».
- **Correctif** : listes d'exclusion (femmes, beach, futsal, esport, clubs, < U-20) ; retrait des préfixes de confédération trop larges (« afc  », « uefa  ») qui attrapaient des ligues de clubs.
- **Leçon** : un filtre par inclusion doit toujours être doublé d'exclusions explicites.

### E11 — API-Football plan Free : saisons 2022-2024 uniquement
- **Symptôme** : `RuntimeError: Free plans do not have access to this season, try from 2022 to 2024` sur les blessures 2026.
- **Correctif** : skip gracieux des appels hors plan (les effectifs, sans paramètre saison, passent). Blessures temps réel = tier payant.
- **Leçon** : coder chaque appel de provider comme optionnel — le plan de l'utilisateur fait partie de l'environnement.

## 3. Qualité des données

### E12 — Crash : « England Women vs England Women » (équipe contre elle-même)
- **Symptôme** : `CheckViolation: fixtures_check` — toute la ronde 3 de sync perdue.
- **Cause racine** : donnée corrompue TheSportsDB (même équipe des deux côtés) + match féminin caché dans la ligue MASCULINE International Friendlies.
- **Correctif** : garde `home_team_id == away_team_id` → skip ; filtre au niveau ÉVÉNEMENT (`_is_target_event`) pour les ligues mixtes.
- **Leçon** : le filtre de ligue ne suffit pas — les données sales se cachent au niveau ligne.

### E13 — Crash : horaire malformé `15:00:00:00`
- **Symptôme** : `ValueError: Invalid isoformat string` — ronde 4 tuée en pleine ligue.
- **Correctif** : `_normalize_time_text` (garde HH:MM:SS, rejette le reste) + try/except → None.
- **Leçon** : ne JAMAIS parser une date/heure externe sans tolérance aux formats sales.

### E14 — Un seul commit en fin de sync = tout perdre au premier crash
- **Symptôme** : ~40 min de sync annulées par une seule ligne corrompue.
- **Correctif** : commit par ligue + SAVEPOINT par événement (une ligne sale est sautée, rien d'autre n'est perdu).
- **Leçon** : la granularité transactionnelle doit être proportionnelle au coût de re-exécution.

### E15 — Fixtures et équipes « synthétiques » en double
- **Symptôme** : Spain vs Austria en double au dashboard — un avec cotes, un avec historique.
- **Cause racine** : l'ingesteur de cotes créait des fixtures/équipes avec IDs négatifs quand les vrais événements TheSportsDB manquaient. La sync premium a ensuite créé les vrais → doublons.
- **Correctif** : `scripts/dedupe_synthetic_fixtures.sql` — fusion par nom d'équipe normalisé + kickoff, migration des cotes, purge des fantômes.
- **Leçon** : toute création d'entité « de secours » doit prévoir sa réconciliation future.

### E16 — Doublon résiduel : « Bosnia & Herzegovina » vs « Bosnia-Herzegovina »
- **Cause racine** : la fusion comparait les noms en minuscules mais la ponctuation différait.
- **Correctif** : normalisation agressive `regexp_replace(lower(name), '[^a-z0-9]', '', 'g')`.
- **Leçon** : normaliser = enlever TOUT ce qui n'est pas alphanumérique, pas juste la casse.

## 4. Infrastructure et configuration

### E17 — `permission denied for schema raw` (API-Football)
- **Cause racine** : le runner se connectait avec le rôle applicatif (`spe_app_rw`) au lieu du rôle d'ingestion — seul `spe_ingest_rw` écrit dans `raw`.
- **Correctif** : `DatabaseSettings.from_env(prefix="POSTGRES_INGEST")` dans le runner.
- **Leçon** : chaque runner doit expliciter SON rôle ; le défaut silencieux est un piège.

### E18 — BOM UTF-8 de PowerShell 5.1 cassant `json.loads` en Python
- **Symptôme** : `Unexpected UTF-8 BOM` en relisant un fichier écrit par `Out-File -Encoding utf8`.
- **Correctif** : faire écrire les fichiers par Python (`Path.write_text(encoding='utf-8')`), pas par PowerShell.
- **Leçon** : PowerShell 5.1 ajoute un BOM en « utf8 » ; en pipeline mixte PS/Python, écrire côté Python.

### E19 — Dérive entre `.env` et `.env.example`
- **Symptôme** : `APIFOOTBALL_KEY` absent du `.env` réel, bloc inséré au milieu de la section The Odds API, `HISTORY_START_YEAR` désynchronisé (2010 vs 2015).
- **Correctif** : réécriture complète des deux fichiers en sections nettes + test de parité (`diff` des noms de variables).
- **Leçon** : `.env` et `.env.example` doivent être modifiés ENSEMBLE, et l'ordre des sections compte pour l'humain qui édite.

### E21 — Deux dashboards liés au même port 8501 (crash + ERR_CONNECTION_REFUSED)
- **Symptôme** : deuxième lancement de `py apps\dashboard\app.py` → traceback dans `serve_forever()` ; le navigateur affiche `ERR_CONNECTION_REFUSED` ; état incohérent (les requêtes partent vers l'une ou l'autre instance).
- **Cause racine** : sur Windows, `wsgiref` active `allow_reuse_address` par défaut → DEUX processus peuvent se lier au même port au lieu que le second échoue. Une instance détachée (session précédente) coexistait avec celle lancée par l'utilisateur dans VS Code.
- **Correctif** : `SingleInstanceServer(WSGIServer)` avec `allow_reuse_address = False` + capture `OSError` → message clair (« le port 8501 est déjà occupé... ») et code retour 1. Testé : le double lancement est refusé proprement, l'instance existante n'est pas perturbée. Bonus : `Ctrl+C` affiche « Arrêt du dashboard » au lieu d'un traceback.
- **Warnings Pylance associés** : les imports `spe_prediction.*`/`spe_ingestion.*` étaient soulignés dans VS Code (chemins `src/` injectés au runtime, invisibles à l'analyse statique) → `.vscode/settings.json` avec `python.analysis.extraPaths`.
- **Leçon** : un serveur local doit refuser de démarrer si son port est pris — le comportement par défaut de Windows rend le conflit silencieux et déroutant.

### E20 — Correctifs « sans effet » : le serveur dashboard servait l'ancien code
- **Symptôme** : le tableau Prédictions affichait toujours les matchs 2018 sans prédiction après le correctif.
- **Cause racine** : `apps/dashboard/app.py` est un serveur WSGI de longue durée — lancé AVANT les correctifs, jamais redémarré. Python ne recharge pas le code à chaud.
- **Correctif** : tuer le processus (port 8501) et relancer. Vérification : `curl` de la page et grep du contenu attendu.
- **Leçon** : après tout changement du dashboard, REDÉMARRER le serveur. En cas de « correctif sans effet », vérifier d'abord l'âge du processus (`Get-Process` / port 8501).

### E22 — `422 Unprocessable Entity` : le marché `btts` refusé par le bulk `/odds` de The Odds API
- **Symptôme** : `markets=h2h,totals,btts` sur `/sports/{sport}/odds` → 422 immédiat ; aucun marché ingéré.
- **Cause racine** : le endpoint bulk ne sert que les marchés « featured » (`h2h`, `spreads`, `totals`). Les marchés additionnels (`btts`, `draw_no_bet`...) ne sont servis que par `/sports/{sport}/events/{id}/odds` — facturé par événement (1 × régions × marchés).
- **Correctif** : bulk en `h2h,totals` + appel par événement pour le `btts`, restreint aux matchs à ≤72 h du kickoff (`THEODDS_API_BTTS_LOOKAHEAD_HOURS`, 0 = off) et tolérant aux 404/422 (marché optionnel, ne fait pas échouer le run de cotes).
- **Leçon** : chez The Odds API, « même clé » ne veut pas dire « même endpoint » : vérifier la disponibilité de chaque marché par endpoint AVANT de promettre le coût quota.

### E23 — Compteurs du résumé d'ingestion remis à zéro par reconstruction partielle du dataclass
- **Symptôme** : `totals_odds_written: 0` dans le résumé du run alors que 118 lignes étaient bel et bien en base.
- **Cause racine** : le dataclass gelé `TheOddsApiIngestionSummary` était reconstruit champ par champ à chaque incrément ; les NOUVEAUX champs, absents de ces constructions historiques, retombaient à leur défaut 0 à chaque événement traité.
- **Correctif** : toutes les incrémentations passées à `dataclasses.replace(summary, champ=champ+1)` — les champs non cités sont préservés.
- **Leçon** : pour incrémenter un dataclass gelé, TOUJOURS `replace()` ; reconstruire à la main est une bombe à retardement pour chaque champ futur. Et vérifier en BASE avant de conclure qu'une écriture a échoué : un compteur peut mentir.

### E24 — « Port 8501 déjà occupé » alors qu'AUCUN serveur ne tourne
- **Symptôme** : `py apps\dashboard\app.py` refuse de démarrer (« port déjà occupé ») ; `netstat` ne montre aucun LISTEN, seulement des connexions `TIME_WAIT` résiduelles ; `curl` ne répond pas.
- **Cause racine** : le correctif E21 (`allow_reuse_address = False`) est trop strict sur Windows — sans `SO_REUSEADDR`, le bind échoue aussi sur les sockets `TIME_WAIT` laissés par l'arrêt précédent (~2 minutes), pas seulement sur un vrai listener concurrent.
- **Correctif** : `SO_EXCLUSIVEADDRUSE` dans `server_bind()` — le flag Windows fait exactement pour ça : interdit le double-bind (l'objectif de E21) mais ignore les `TIME_WAIT`. Testé : démarrage immédiat malgré TIME_WAIT ✓, redémarrage immédiat après kill ✓, double lancement toujours refusé proprement ✓.
- **Leçon** : sur Windows, l'exclusivité d'un port se fait avec `SO_EXCLUSIVEADDRUSE`, pas en désactivant `SO_REUSEADDR` — les deux flags ne protègent pas contre la même chose.

### E25 — Règlement 1X2 sur le score FINAL au lieu des 90 minutes
- **Symptôme** : un pari NUL sur un match de Coupe du monde à élimination directe (0-0 à la 90e, 1-0 après prolongation) aurait été marqué PERDU alors que le pari se règle sur le temps réglementaire — donc GAGNÉ.
- **Cause racine** : `settle_result` utilisait `core.fixture_scores` (score final, prolongation incluse). « Gagner » un pari 1X2/O-U/BTTS se juge à 90 minutes ; « se qualifier » (prolongation, tirs au but) est un autre marché.
- **Correctif** : `regulation_score()` reconstruit le score des 90 min depuis `core.fixture_timeline` quand il y a un but en prolongation ET que la timeline est cohérente avec le score officiel (sinon fallback prudent au score final). Convention TheSportsDB vérifiée : temps additionnel (90+X) stocké à la minute 90, prolongation aux minutes 91-120 → seuil `minute <= 90` exact. Spot-check réel : Algeria–DR Congo (final 1-0, réglementaire 0-0) réglé en NUL.
- **Leçon** : au foot, le score officiel d'un provider inclut la prolongation ; tout pari « temps réglementaire » doit reconstruire les 90 min. La qualification (tout le reste) est un marché distinct, pas encore offert.

---

## Réflexes de débogage tirés de ce journal

1. **« Pas de données » ?** → vérifier `ops.ingestion_runs` (E08), puis les caps du provider (E07, E11).
2. **« Correctif sans effet » ?** → âge du processus serveur (E20), puis cache/duplication de données (E15).
3. **Crash de sync ?** → c'est une donnée sale (E12, E13) ; le blindage (E14) limite la perte, corriger la garde et relancer.
4. **Edge irréaliste ?** → c'est notre modèle qui se trompe (E01, E03), pas le bookmaker.
5. **Doublons au dashboard ?** → `scripts/dedupe_synthetic_fixtures.sql` (E15, E16).
