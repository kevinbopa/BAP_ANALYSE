from __future__ import annotations

from dataclasses import dataclass
import os


@dataclass(frozen=True)
class DatabaseSettings:
    host: str
    port: int
    dbname: str
    user: str
    password: str

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
        )

    def dsn(self) -> str:
        return (
            f"host={self.host} port={self.port} dbname={self.dbname} "
            f"user={self.user} password={self.password}"
        )


def connect_db(settings: DatabaseSettings):
    import psycopg

    return psycopg.connect(settings.dsn())
