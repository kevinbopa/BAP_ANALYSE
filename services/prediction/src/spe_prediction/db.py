from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import sys
from urllib.parse import unquote, urlparse

from spe_prediction.config import get_env


@dataclass(frozen=True)
class DatabaseSettings:
    host: str
    port: int
    dbname: str
    user: str
    password: str
    application_name: str

    @classmethod
    def from_env(cls, prefix: str = "POSTGRES") -> "DatabaseSettings":
        parsed_url = _database_url_settings(prefix)

        def _get(name: str, default: str) -> str:
            return get_env(f"{prefix}_{name}", get_env(name, default))

        return cls(
            host=parsed_url.get("host") or _get("HOST", "localhost"),
            port=int(parsed_url.get("port") or _get("PORT", "5432")),
            dbname=parsed_url.get("dbname") or _get("DB", "sports_prediction_engine"),
            user=parsed_url.get("user") or _get("USER", "spe_app_rw"),
            password=parsed_url.get("password") or _get("PASSWORD", ""),
            application_name=_sanitize_application_name(
                get_env("SPE_DB_APPLICATION_NAME", "")
                or get_env("PGAPPNAME", "")
                or Path(sys.argv[0] or "spe-prediction").stem
                or "spe-prediction"
            ),
        )

    def dsn(self) -> str:
        return (
            f"host={self.host} port={self.port} dbname={self.dbname} "
            f"user={self.user} password={self.password} application_name={self.application_name}"
        )


def connect_db(settings: DatabaseSettings):
    import psycopg

    return psycopg.connect(settings.dsn())


def _sanitize_application_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip())
    return cleaned[:63] or "spe-prediction"


def _database_url_settings(prefix: str) -> dict[str, str]:
    raw_url = (
        get_env(f"{prefix}_URL", "")
        or get_env("DATABASE_URL", "")
        or get_env("POSTGRES_URL", "")
    )
    if not raw_url:
        return {}
    parsed = urlparse(raw_url)
    if parsed.scheme not in {"postgres", "postgresql"}:
        return {}
    dbname = parsed.path.lstrip("/")
    return {
        "host": parsed.hostname or "",
        "port": str(parsed.port or ""),
        "dbname": unquote(dbname) if dbname else "",
        "user": unquote(parsed.username or ""),
        "password": unquote(parsed.password or ""),
    }
