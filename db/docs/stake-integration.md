# Stake Integration

## Objet

Ce document cadre l'integration `Stake` pour la partie `odds 1X2`.

## Regle de securite

Le token Stake ne doit jamais :

- etre committe dans le depot ;
- etre stocke en clair dans une migration SQL ;
- etre colle dans la documentation ;
- etre envoye dans une capture d'ecran.

Le token doit etre charge uniquement via des variables d'environnement ou un secret manager.

## Positionnement dans l'architecture

- `TheSportsDB` : referentiel football, equipes, ligues, fixtures, stats
- `Stake` : cotes bookmaker `1X2` si l'acces API est confirme et autorise

## Information confirmee par la documentation odds officielle

La documentation officielle `Stake Sports Data API` confirme :

- base URL `https://odds-data.stake.com`
- endpoint `GET /sports`
- endpoint `GET /sports/{sport}/categories`
- endpoint `GET /sports/{sport}/{category}/tournaments`
- endpoint `GET /sports/{sport}/{category}/{tournament}/fixtures`
- endpoint `GET /fixtures/{fixture}`
- schema de securite documente `X-API-KEY`

## Information confirmee par le fichier OpenAPI fourni par l'utilisateur

Le fichier `dist (1).yaml` fourni par l'utilisateur confirme :

- serveur principal `https://api.stake.com`
- authentification via header `x-access-token`

Ce fichier semble documenter une autre famille d'API Stake, pas la plateforme odds-data exploitee par notre V1.

## Observation reelle du 27 juin 2026

Les appels directs a `https://odds-data.stake.com` ont bien retourne :

- la liste des sports ;
- les categories football ;
- les tournois disponibles selon la zone ;
- les fixtures et payloads de detail.

En revanche, pour plusieurs fixtures football verifiees le 27 juin 2026, les groupes `winner`, `threeway` ou `1x2 UP` pouvaient etre presentes sans outcomes `1X2` remplies.

Conclusion :

- nous pouvons brancher de vraies donnees Stake au logiciel ;
- nous devons stocker les payloads bruts ;
- nous ne devons inserer `core.fixture_odds_1x2` que quand les trois cotes `HOME`, `DRAW`, `AWAY` sont reellement presentes.

## Ce qui est deja prepare

- provider `STAKE` dans `ops.providers`
- endpoints reels `STAKE` dans `ops.provider_endpoints`
- mode d'authentification stocke de facon generique `TOKEN_HEADER`
- tables cibles deja presentes :
  - `core.bookmakers`
  - `core.fixture_odds_1x2`
  - `model.value_bets`
  - `model.deal_rankings`

## Configuration recommandee

Variables d'environnement minimales :

- `STAKE_API_TOKEN`
- `STAKE_API_BASE_URL`
- `STAKE_AUTH_HEADER`
- `STAKE_AUTH_SCHEME`
- `STAKE_SPORTS_PATH`
- `STAKE_CATEGORIES_PATH_TEMPLATE`
- `STAKE_TOURNAMENTS_PATH_TEMPLATE`
- `STAKE_FIXTURES_PATH_TEMPLATE`
- `STAKE_FIXTURE_PATH_TEMPLATE`
- `STAKE_ODDS_PATH_TEMPLATE`
- `STAKE_SPORT_SLUG`
- `STAKE_LOOKAHEAD_DAYS`
- `STAKE_MATCH_WINDOW_MINUTES`
- `STAKE_TIMEOUT_SECONDS`

Note :

- si un `X-API-KEY` officiel est fourni plus tard, le client peut l'utiliser ;
- lors de nos tests du 27 juin 2026, `GET /sports` fonctionnait meme sans cle fournie ;
- le client reste configurable pour absorber une evolution du contrat Stake sans casser le schema base.

## Approche recommandee

1. charger le token depuis l'environnement
2. appeler les endpoints documentes ou confirmes
3. stocker les payloads bruts dans `raw.provider_payloads`
4. normaliser les cotes dans `core.fixture_odds_1x2`
5. lier ensuite ces odds aux predictions `1X2`

## Pattern d'integration recommande

La separation recommandee est :

`Stake API -> client Python -> PostgreSQL -> modele`

Il ne faut pas faire dependre directement le modele de l'API externe au moment de la prediction.

## Precondition importante

Le projet ne doit brancher des appels `Stake` qu'avec :

- un acces autorise ;
- une documentation d'endpoint exploitable ;
- une validation claire des headers et du format de reponse.

## Si la doc officielle est incomplète

Dans ce cas, on garde l'integration en mode configurable :

- base URL configurable
- paths configurables
- headers configurables
- mapping transform configurable cote `home/draw/away`

Cela nous permet d'avancer sans figer de faux endpoints dans le code.
