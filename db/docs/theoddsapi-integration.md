# The Odds API Integration

## Objet

Ce document cadre l'integration `The Odds API` comme source principale des cotes V1.

## Positionnement V1

- `TheSportsDB` : referentiel football, equipes, ligues, fixtures
- `The Odds API` : cotes bookmaker `1X2`
- `Stake` : connecteur secondaire conserve pour exploration

## Contrat officiel confirme

Documentation officielle verifiee le 27 juin 2026 :

- host `https://api.the-odds-api.com/v4`
- `GET /sports`
- `GET /sports/{sport}/odds`
- marche `h2h`
- parametres principaux :
  - `apiKey`
  - `regions`
  - `markets`
  - `bookmakers`
  - `commenceTimeFrom`
  - `commenceTimeTo`
  - `oddsFormat`
  - `dateFormat`

Source officielle :

- [The Odds API v4 docs](https://the-odds-api.com/liveapi/guides/v4/)

## Mapping V1 retenu

Bookmakers ingeres :

- TOUS les bookmakers retournes par l'API (necessaire au consensus du modele :
  mediane, spreads, source_count, price_outlier).
- Attention : `bet365` n'est pas couvert par The Odds API (verifie le 2026-07-02,
  regions `uk,eu`, 41 bookmakers retournes, zero bet365). bet365 bloque les
  agregateurs de cotes.
- Le bookmaker cible business (celui ou l'utilisateur parie) se configure via
  `SPE_TARGET_BOOKMAKER` et s'applique au niveau des deals, pas de l'ingestion.

Ligues football cibles par defaut :

- `soccer_epl`
- `soccer_spain_la_liga`
- `soccer_germany_bundesliga`
- `soccer_italy_serie_a`
- `soccer_france_ligue_one`

Marche V1 :

- `h2h`

Format retenu :

- `decimal`

## Flux de donnees

`The Odds API -> client Python -> raw.provider_payloads -> core.fixture_odds_1x2 -> modele`

## Regles de normalisation

- un event The Odds API est rapproche d'un fixture interne par :
  - ligue / sport key
  - home team
  - away team
  - proximite de kickoff
- la V1 ne retient que `bet365` comme bookmaker de reference
- chaque snapshot `bet365` devient une ligne dans `core.fixture_odds_1x2`
- seules les reponses avec trois outcomes `HOME / DRAW / AWAY` sont ecrites pour la V1
- les marches deux issues sont ignores pour ne pas polluer le modele `1X2`

## Variables d'environnement

- `THEODDS_API_KEY`
- `THEODDS_API_BASE_URL`
- `THEODDS_API_SPORT_KEYS`
- `THEODDS_API_REGIONS`
- `THEODDS_API_MARKETS`
- `THEODDS_API_ODDS_FORMAT`
- `THEODDS_API_DATE_FORMAT`
- `THEODDS_API_BOOKMAKERS`
- `THEODDS_API_LOOKBACK_HOURS`
- `THEODDS_API_LOOKAHEAD_DAYS`
- `THEODDS_API_MATCH_WINDOW_MINUTES`
- `THEODDS_API_TIMEOUT_SECONDS`

## Politique de securite

- la cle API ne doit jamais etre committee
- la cle API doit etre chargee uniquement depuis `.env` ou un secret manager
- les payloads bruts stockes en base ne doivent pas contenir la cle
