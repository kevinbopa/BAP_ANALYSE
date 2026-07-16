from __future__ import annotations

from dataclasses import dataclass

from spe_prediction.config import get_env


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
            return get_env(f"{prefix}_{name}", get_env(name, default))

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
