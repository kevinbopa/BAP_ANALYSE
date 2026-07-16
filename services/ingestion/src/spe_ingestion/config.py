from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path


_ENV_LOADED = False


def _load_dotenv() -> None:
    global _ENV_LOADED
    if _ENV_LOADED:
        return

    current = Path(__file__).resolve()
    for parent in current.parents:
        candidate = parent / ".env"
        if candidate.exists():
            for line in candidate.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("#") or "=" not in stripped:
                    continue
                key, value = stripped.split("=", 1)
                key = key.strip()
                value = value.strip()
                if key and key not in os.environ:
                    os.environ[key] = value
            break

    _ENV_LOADED = True


def _required(name: str) -> str:
    _load_dotenv()
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"Missing required environment variable: {name}")
    return value


def _optional(name: str, default: str) -> str:
    _load_dotenv()
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return value.strip()


def _optional_float(name: str, default: float) -> float:
    _load_dotenv()
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return float(value)


def _optional_int(name: str, default: int) -> int:
    _load_dotenv()
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return int(value)


def _optional_csv(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    _load_dotenv()
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return tuple(item.strip() for item in value.split(",") if item.strip())


@dataclass(frozen=True)
class TheSportsDBSettings:
    api_key: str
    base_url: str
    timeout_seconds: float
    min_interval_seconds: float
    max_retries: int
    history_start_year: int
    extra_league_ids: tuple[int, ...]

    @classmethod
    def from_env(cls) -> "TheSportsDBSettings":
        # La cle gratuite '123' n'expose que ~10 ligues via all_leagues.php.
        # Ces IDs internationaux (verifies par lookupleague.php) sont injectes
        # directement pour contourner la liste tronquee. Extensible par env.
        default_extra = "4429,4490,4499,4502,4498"  # WC, Nations League, Copa America, Euro, Confederations Cup
        raw_ids = _optional("THESPORTSDB_EXTRA_LEAGUE_IDS", default_extra)
        extra_ids = tuple(
            int(item.strip()) for item in raw_ids.split(",") if item.strip().isdigit()
        )
        return cls(
            api_key=_required("THESPORTSDB_API_KEY"),
            base_url=_optional("THESPORTSDB_BASE_URL", "https://www.thesportsdb.com/api/v1/json"),
            timeout_seconds=_optional_float("THESPORTSDB_TIMEOUT_SECONDS", 20.0),
            min_interval_seconds=_optional_float("THESPORTSDB_MIN_INTERVAL_SECONDS", 2.2),
            max_retries=int(_optional("THESPORTSDB_MAX_RETRIES", "4")),
            history_start_year=_optional_int("THESPORTSDB_HISTORY_START_YEAR", 2015),
            extra_league_ids=extra_ids,
        )


@dataclass(frozen=True)
class ApiFootballSettings:
    api_key: str
    base_url: str
    timeout_seconds: float
    min_interval_seconds: float
    season_year: int
    daily_request_budget: int

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    @classmethod
    def from_env(cls) -> "ApiFootballSettings":
        return cls(
            api_key=_optional("APIFOOTBALL_KEY", ""),
            base_url=_optional("APIFOOTBALL_BASE_URL", "https://v3.football.api-sports.io"),
            timeout_seconds=_optional_float("APIFOOTBALL_TIMEOUT_SECONDS", 20.0),
            # Free tier ~10 req/min → 6.5s d'intervalle par prudence.
            min_interval_seconds=_optional_float("APIFOOTBALL_MIN_INTERVAL_SECONDS", 6.5),
            season_year=_optional_int("APIFOOTBALL_SEASON_YEAR", 2026),
            # Budget par run pour ne jamais griller le quota journalier (100).
            daily_request_budget=_optional_int("APIFOOTBALL_REQUEST_BUDGET", 90),
        )


@dataclass(frozen=True)
class DataGolfSettings:
    api_key: str
    base_url: str
    file_format: str
    odds_format: str
    tours: tuple[str, ...]
    catalog_tours: tuple[str, ...]
    timeout_seconds: float
    min_interval_seconds: float
    max_retries: int

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    @classmethod
    def from_env(cls) -> "DataGolfSettings":
        return cls(
            api_key=_optional("DATAGOLF_API_KEY", ""),
            base_url=_optional("DATAGOLF_BASE_URL", "https://feeds.datagolf.com"),
            file_format=_optional("DATAGOLF_FILE_FORMAT", "json"),
            odds_format=_optional("DATAGOLF_ODDS_FORMAT", "decimal"),
            # Circuits masculins couverts par le modele DataGolf.
            # opp = opposite-field (ISCO...), alt = events alternatifs.
            tours=_optional_csv("DATAGOLF_TOURS", ("pga", "euro", "kft", "opp", "alt", "liv")),
            catalog_tours=_optional_csv("DATAGOLF_CATALOG_TOURS", ("all",)),
            timeout_seconds=_optional_float("DATAGOLF_TIMEOUT_SECONDS", 20.0),
            # Limite API : 45 req/min -> 1.5s d'intervalle par prudence.
            min_interval_seconds=_optional_float("DATAGOLF_MIN_INTERVAL_SECONDS", 1.5),
            max_retries=_optional_int("DATAGOLF_MAX_RETRIES", 3),
        )


@dataclass(frozen=True)
class StakeSettings:
    api_token: str
    base_url: str
    auth_header: str
    auth_scheme: str
    sports_path: str
    categories_path_template: str
    tournaments_path_template: str
    fixtures_path_template: str
    fixture_path_template: str
    odds_path_template: str
    sport_slug: str
    lookahead_days: int
    match_window_minutes: int
    timeout_seconds: float

    @classmethod
    def from_env(cls) -> "StakeSettings":
        return cls(
            api_token=_optional("STAKE_API_TOKEN", ""),
            base_url=_optional("STAKE_API_BASE_URL", "https://odds-data.stake.com"),
            auth_header=_optional("STAKE_AUTH_HEADER", "X-API-KEY"),
            auth_scheme=_optional("STAKE_AUTH_SCHEME", ""),
            sports_path=_optional("STAKE_SPORTS_PATH", "/sports"),
            categories_path_template=_optional(
                "STAKE_CATEGORIES_PATH_TEMPLATE",
                "/sports/{sport}/categories",
            ),
            tournaments_path_template=_optional(
                "STAKE_TOURNAMENTS_PATH_TEMPLATE",
                "/sports/{sport}/{category}/tournaments",
            ),
            fixtures_path_template=_optional(
                "STAKE_FIXTURES_PATH_TEMPLATE",
                "/sports/{sport}/{category}/{tournament}/fixtures",
            ),
            fixture_path_template=_optional(
                "STAKE_FIXTURE_PATH_TEMPLATE",
                "/fixtures/{fixture_slug}",
            ),
            odds_path_template=_optional(
                "STAKE_ODDS_PATH_TEMPLATE",
                "/odds/{fixture_slug}",
            ),
            sport_slug=_optional("STAKE_SPORT_SLUG", "soccer"),
            lookahead_days=_optional_int("STAKE_LOOKAHEAD_DAYS", 90),
            match_window_minutes=_optional_int("STAKE_MATCH_WINDOW_MINUTES", 720),
            timeout_seconds=_optional_float("STAKE_TIMEOUT_SECONDS", 20.0),
        )


@dataclass(frozen=True)
class TheOddsApiSettings:
    api_key: str
    base_url: str
    sport_keys: tuple[str, ...]
    regions: str
    markets: str
    odds_format: str
    date_format: str
    bookmakers: str
    lookback_hours: int
    lookahead_days: int
    match_window_minutes: int
    timeout_seconds: float
    # Fenetre (heures avant kickoff) pendant laquelle on va chercher le BTTS
    # via l'endpoint par evenement (cout quota par match). 0 = desactive.
    btts_lookahead_hours: int = 72
    # Plafond d'appels BTTS par sync : chaque appel coute [regions] credits.
    # Un weekend de pleine saison europeenne peut avoir 80+ matchs dans la
    # fenetre — sans plafond, le quota gratuit fondrait en quelques jours.
    btts_max_events_per_sync: int = 20

    @classmethod
    def from_env(cls) -> "TheOddsApiSettings":
        return cls(
            api_key=_required("THEODDS_API_KEY"),
            base_url=_optional("THEODDS_API_BASE_URL", "https://api.the-odds-api.com/v4"),
            sport_keys=_optional_csv(
                "THEODDS_API_SPORT_KEYS",
                (
                    "soccer_epl",
                    "soccer_spain_la_liga",
                    "soccer_germany_bundesliga",
                    "soccer_italy_serie_a",
                    "soccer_france_ligue_one",
                ),
            ),
            regions=_optional("THEODDS_API_REGIONS", "uk,eu"),
            # Le bulk /odds n'accepte que les marches "featured" (h2h, spreads,
            # totals). Le btts passe par l'endpoint par evenement, pilote par
            # THEODDS_API_BTTS_LOOKAHEAD_HOURS ci-dessous.
            markets=_optional("THEODDS_API_MARKETS", "h2h,totals"),
            odds_format=_optional("THEODDS_API_ODDS_FORMAT", "decimal"),
            date_format=_optional("THEODDS_API_DATE_FORMAT", "iso"),
            # Empty default on purpose: ingest every bookmaker so the model's
            # consensus (median/spread/source_count) stays meaningful. The
            # betting target is enforced at the deal layer (SPE_TARGET_BOOKMAKER).
            bookmakers=_optional("THEODDS_API_BOOKMAKERS", ""),
            lookback_hours=_optional_int("THEODDS_API_LOOKBACK_HOURS", 6),
            lookahead_days=_optional_int("THEODDS_API_LOOKAHEAD_DAYS", 14),
            match_window_minutes=_optional_int("THEODDS_API_MATCH_WINDOW_MINUTES", 180),
            timeout_seconds=_optional_float("THEODDS_API_TIMEOUT_SECONDS", 20.0),
            btts_lookahead_hours=_optional_int("THEODDS_API_BTTS_LOOKAHEAD_HOURS", 72),
            btts_max_events_per_sync=_optional_int("THEODDS_API_BTTS_MAX_EVENTS", 20),
        )
