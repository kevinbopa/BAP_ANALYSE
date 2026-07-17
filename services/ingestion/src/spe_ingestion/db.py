from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import sys
from urllib.parse import unquote, urlparse


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
            return os.getenv(f"{prefix}_{name}", os.getenv(name, default)).strip()

        return cls(
            host=parsed_url.get("host") or _get("HOST", "localhost"),
            port=int(parsed_url.get("port") or _get("PORT", "5432")),
            dbname=parsed_url.get("dbname") or _get("DB", "sports_prediction_engine"),
            user=parsed_url.get("user") or _get("USER", "spe_app_rw"),
            password=parsed_url.get("password") or _get("PASSWORD", ""),
            application_name=_sanitize_application_name(
                os.getenv("SPE_DB_APPLICATION_NAME", "")
                or os.getenv("PGAPPNAME", "")
                or Path(sys.argv[0] or "spe-ingestion").stem
                or "spe-ingestion"
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
    return cleaned[:63] or "spe-ingestion"


def _database_url_settings(prefix: str) -> dict[str, str]:
    raw_url = (
        os.getenv(f"{prefix}_URL", "").strip()
        or os.getenv("DATABASE_URL", "").strip()
        or os.getenv("POSTGRES_URL", "").strip()
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
