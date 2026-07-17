from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import sys


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
        def _get(name: str, default: str) -> str:
            return os.getenv(f"{prefix}_{name}", os.getenv(name, default)).strip()

        return cls(
            host=_get("HOST", "localhost"),
            port=int(_get("PORT", "5432")),
            dbname=_get("DB", "sports_prediction_engine"),
            user=_get("USER", "spe_app_rw"),
            password=_get("PASSWORD", ""),
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
