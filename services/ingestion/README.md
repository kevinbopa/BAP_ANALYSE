# Ingestion Service

## Objet

Ce module porte les connecteurs externes du projet.

## Connecteurs cibles V1

- `TheSportsDB` pour les donnees football
- `The Odds API` pour les cotes `1X2`, derivees football et golf outrights
- `Stake` comme connecteur secondaire / exploratoire

## Securite

- les tokens ne sont jamais codés en dur
- l'environnement est la seule source locale de secrets
- les payloads recus peuvent etre journalises en `raw`
- les erreurs reseau doivent masquer les secrets

## Pattern d'architecture

Le pattern recommande pour les odds est :

`Odds Provider API -> client -> PostgreSQL -> modele`

Le modele doit lire la base, pas interroger Stake directement pendant l'inference.

## Modules

- `src/spe_ingestion/config.py` : lecture de configuration
- `src/spe_ingestion/db.py` : connexion PostgreSQL
- `src/spe_ingestion/clients/thesportsdb.py` : client TheSportsDB
- `src/spe_ingestion/clients/theoddsapi.py` : client The Odds API v4
- `src/spe_ingestion/clients/stake.py` : client Stake odds-data
- `src/spe_ingestion/thesportsdb_ingestor.py` : ingestion TheSportsDB vers PostgreSQL
- `src/spe_ingestion/theoddsapi_odds_ingestor.py` : ingestion The Odds API vers PostgreSQL
- `src/spe_ingestion/stake_odds_ingestor.py` : ingestion Stake vers PostgreSQL
- `run_ingest_golf_odds.py` : ingestion des tournois et cotes golf

## Etat actuel

L'integration odds principale est maintenant alignee sur la documentation officielle The Odds API v4 :

- base URL `https://api.the-odds-api.com/v4`
- endpoint `GET /sports`
- endpoint `GET /sports/{sport}/odds`
- query params `apiKey`, `regions`, `markets`, `bookmakers`, `commenceTimeFrom`, `commenceTimeTo`
- marche V1 cible `h2h` en format decimal

Le pipeline `TheSportsDB -> PostgreSQL` peut ingerer :

- ligues football
- saisons
- equipes
- fixtures de saison
- scores consolides

Le pipeline `The Odds API -> PostgreSQL` peut :

- appeler les sports / competitions football cibles ;
- recuperer les evenements football avec tous les bookmakers disponibles
  (le consensus du modele exige plusieurs books : mediane, spreads, source_count) ;
- filtrer optionnellement via `THEODDS_API_BOOKMAKERS` (vide = tous, recommande) ;
- stocker les payloads bruts dans `raw.provider_payloads` ;
- normaliser les cotes `1X2` dans `core.fixture_odds_1x2`.

Le pipeline `Stake -> PostgreSQL` peut encore :

- resoudre les tournois disponibles par pays / ligue ;
- recuperer les fixtures live Stake ;
- stocker les payloads bruts dans `raw.provider_payloads` ;
- normaliser les cotes `1X2` dans `core.fixture_odds_1x2` quand Stake retourne vraiment le marche.

Observation importante :

- The Odds API est la source prioritaire V1 pour les odds ;
- `bet365` N'EST PAS couvert par The Odds API (bet365 bloque les agregateurs de cotes) —
  verifie empiriquement le 2026-07-02 : 41 bookmakers retournes en regions `uk,eu`, zero bet365 ;
- le ciblage bookmaker business se fait cote prediction via `SPE_TARGET_BOOKMAKER`
  (les deals affiches viennent du book cible s'il price le fixture, sinon fallback tous books) ;
- Stake reste disponible mais n'est plus la source recommandee pour la production V1.

## Execution

Commande :

`py services/ingestion/run_ingest_thesportsdb.py`

Commande Stake :

`py services/ingestion/run_ingest_stake_odds.py`

Commande The Odds API :

`py services/ingestion/run_ingest_theoddsapi_odds.py`

Commande Golf The Odds API :

`py services/ingestion/run_ingest_golf_odds.py`

Commande meteo Golf :

`py services/ingestion/run_ingest_golf_weather.py`

Variables golf utiles :

- `THEODDS_API_GOLF_SPORT_KEYS` : optionnel, sinon le script decouvre les sports golf avec outrights via `/sports`.
- `THEODDS_API_GOLF_MARKETS` : defaut `outrights`; accepte aussi les marches top/round si exposes par l'API.
- `THEODDS_API_GOLF_BOOKMAKERS` : optionnel; si vide, le script ingere tous les bookmakers disponibles pour garder un consensus robuste.
- `THEODDS_API_GOLF_FETCH_PARTICIPANTS` : defaut `true`; charge les participants golf quand l'endpoint est disponible.
- `THEODDS_API_GOLF_TARGET_BOOKMAKER(S)` : bookmaker(s) cible(s) pour les deals golf, defaut `SPE_TARGET_BOOKMAKER`, puis tous si aucune cible.
- `GOLF_TOURNAMENT_VENUES_JSON` : mapping manuel tournoi -> ville/pays/coordonnees quand l'API odds ne donne pas le parcours, utilise par la meteo.

Le service d'ingestion doit utiliser les variables :

- `POSTGRES_INGEST_HOST`
- `POSTGRES_INGEST_PORT`
- `POSTGRES_INGEST_DB`
- `POSTGRES_INGEST_USER`
- `POSTGRES_INGEST_PASSWORD`
- `THEODDS_API_KEY`
- `THEODDS_API_SPORT_KEYS`
- `THEODDS_API_BOOKMAKERS`
- `THEODDS_API_REGIONS`
- `THEODDS_API_MARKETS`
- `THEODDS_API_LOOKAHEAD_DAYS`
- `THEODDS_API_MATCH_WINDOW_MINUTES`
- `THEODDS_API_GOLF_SPORT_KEYS`
- `THEODDS_API_GOLF_MARKETS`
- `THEODDS_API_GOLF_BOOKMAKERS`
- `THEODDS_API_GOLF_TARGET_BOOKMAKER`
- `STAKE_API_BASE_URL`
- `STAKE_SPORT_SLUG`
- `STAKE_LOOKAHEAD_DAYS`
- `STAKE_MATCH_WINDOW_MINUTES`
