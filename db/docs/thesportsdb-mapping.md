# TheSportsDB Mapping

## Source officielle utilisee

Base URL v1 documentee :

- `https://www.thesportsdb.com/api/v1/json`

## Endpoints V1 retenus pour la base

### Catalogue et referentiels

- `all_leagues.php`
- `search_all_leagues.php?c={country}&s=Soccer`
- `search_all_teams.php?l={league}`
- `lookupteam.php?id={idTeam}`
- `lookupvenue.php?id={idVenue}`
- `listseasons.php?id={idLeague}` ou equivalent documente `List Seasons`

### Matchs et calendrier

- `eventsseason.php?id={idLeague}&s={season}`
- `eventsnextleague.php?id={idLeague}`
- `eventspastleague.php?id={idLeague}`
- `eventsday.php?d={date}&s=Soccer`
- `lookupevent.php?id={idEvent}`

### Enrichissements match

- `lookupeventstats.php?id={idEvent}`
- `lookuplineup.php?id={idEvent}`
- `lookuptimeline.php?id={idEvent}`
- `lookuptable.php?l={idLeague}&s={season}`

## Mapping principal vers la base

### `all_leagues.php` / `search_all_leagues.php`

Tables cibles :

- `raw.provider_payloads`
- `core.leagues`

### `List Seasons`

Tables cibles :

- `raw.provider_payloads`
- `core.seasons`

### `search_all_teams.php` / `lookupteam.php`

Tables cibles :

- `raw.provider_payloads`
- `core.teams`
- `core.team_season_memberships`

### `lookupvenue.php`

Tables cibles :

- `raw.provider_payloads`
- `core.venues`

### `eventsseason.php` / `eventsnextleague.php` / `eventspastleague.php` / `eventsday.php`

Tables cibles :

- `raw.provider_payloads`
- `core.fixtures`
- `core.fixture_scores`

### `lookupevent.php`

Tables cibles :

- `raw.provider_payloads`
- `core.fixtures`
- `core.fixture_scores`

### `lookupeventstats.php`

Tables cibles :

- `raw.provider_payloads`
- `core.fixture_team_statistics`

### `lookuplineup.php`

Tables cibles :

- `raw.provider_payloads`
- `core.fixture_lineups`

### `lookuptimeline.php`

Tables cibles :

- `raw.provider_payloads`
- `core.fixture_timeline_events`

### `lookuptable.php`

Tables cibles :

- `raw.provider_payloads`
- `core.league_table_snapshots`
- `core.league_table_rows`

## Exemples de champs utiles documentes

Exemple `lookupevent.php` :

- `idEvent`
- `strTimestamp`
- `strSport`
- `idLeague`
- `strLeague`
- `strSeason`
- `strHomeTeam`
- `strAwayTeam`
- `idHomeTeam`
- `idAwayTeam`
- `intHomeScore`
- `intAwayScore`
- `intRound`
- `dateEvent`
- `dateEventLocal`
- `strTime`
- `strTimeLocal`
- `idVenue`
- `strVenue`
- `strStatus`
- `strPostponed`
- `strLocked`

Exemple `lookupteam.php` :

- `idTeam`
- `idAPIfootball`
- `strTeam`
- `strTeamShort`
- `strSport`
- `strLeague`
- `idLeague`
- `idVenue`
- `strStadium`
- `strLocation`
- `intStadiumCapacity`
- `strWebsite`
- `strFacebook`
- `strTwitter`
- `strInstagram`

## Limite officielle importante pour la V1

La documentation officielle TheSportsDB consultee ne documente pas de endpoint `bookmaker odds` ou `1X2 odds` dans l'API v1.

Consequence :

- la base V1 contient bien les tables `core.bookmakers` et `core.fixture_odds_1x2`
- ces tables ne seront pas peuplees par TheSportsDB seul
- le produit V1 pourra ingerer les donnees football depuis TheSportsDB
- une source de cotes devra etre ajoutee ensuite pour activer completement la couche `value bets`

## Implication produit

Pour demarrer correctement :

- TheSportsDB couvre tres bien le referentiel football, les matchs, les scores, les stats et les lineups
- la prediction `1X2` peut etre construite sur cette base
- la comparaison aux bookmakers necessite une deuxieme source de cotes
