from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil
import sys
from urllib.parse import unquote, urlparse


ROOT_DIR = Path(__file__).resolve().parents[1]
PREDICTION_SRC = ROOT_DIR / "services" / "prediction" / "src"
if str(PREDICTION_SRC) not in sys.path:
    sys.path.insert(0, str(PREDICTION_SRC))

from spe_prediction.config import get_env


LOCALHOST_NAMES = {"localhost", "127.0.0.1", "::1"}


@dataclass(frozen=True)
class DbSettings:
    host: str
    port: int
    dbname: str
    user: str
    password: str

    def dsn(self) -> str:
        return (
            f"host={self.host} port={self.port} dbname={self.dbname} "
            f"user={self.user} password={self.password}"
        )


def _defaults(admin_defaults: bool) -> tuple[str, str]:
    if admin_defaults:
        return ("postgres", "postgres")
    return ("spe_app_rw", "")


def env_with_fallback(name: str, prefix: str, fallback_prefix: str, default: str) -> str:
    prefixed = get_env(f"{prefix}_{name}", "")
    if prefixed:
        return prefixed
    fallback = get_env(f"{fallback_prefix}_{name}", "")
    if fallback:
        return fallback
    return get_env(name, default)


def database_url_settings(prefix: str, fallback_prefix: str) -> dict[str, str]:
    raw_url = (
        get_env(f"{prefix}_URL", "")
        or get_env(f"{fallback_prefix}_URL", "")
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


def load_db_settings(prefix: str, fallback_prefix: str, admin_defaults: bool = False) -> DbSettings:
    default_user, default_password = _defaults(admin_defaults)
    parsed_url = database_url_settings(prefix, fallback_prefix)
    if admin_defaults:
        user = parsed_url.get("user") or get_env(f"{prefix}_USER", "").strip() or default_user
        password = parsed_url.get("password") or get_env(f"{prefix}_PASSWORD", "").strip() or default_password
    else:
        user = parsed_url.get("user") or env_with_fallback("USER", prefix, fallback_prefix, default_user)
        password = parsed_url.get("password") or env_with_fallback("PASSWORD", prefix, fallback_prefix, default_password)
    return DbSettings(
        host=parsed_url.get("host") or env_with_fallback("HOST", prefix, fallback_prefix, "localhost"),
        port=int(parsed_url.get("port") or env_with_fallback("PORT", prefix, fallback_prefix, "5432")),
        dbname=parsed_url.get("dbname") or env_with_fallback("DB", prefix, fallback_prefix, "sports_prediction_engine"),
        user=user,
        password=password,
    )


def resolve_setting(name: str, prefix: str, fallback_prefix: str, default: str = "") -> str:
    return env_with_fallback(name, prefix, fallback_prefix, default)


def is_localhost(host: str) -> bool:
    return host.strip().lower() in LOCALHOST_NAMES


def resolve_pg_binary(tool_name: str, pg_bin_dir: str | None) -> str | None:
    if pg_bin_dir:
        candidate = Path(pg_bin_dir) / f"{tool_name}.exe"
        if candidate.exists():
            return str(candidate)
        candidate = Path(pg_bin_dir) / tool_name
        if candidate.exists():
            return str(candidate)
    return shutil.which(tool_name)
