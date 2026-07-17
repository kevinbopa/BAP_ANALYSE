from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
from html import escape
from http.cookies import SimpleCookie
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import traceback
from typing import Any, Mapping
from urllib.parse import parse_qs, quote, urlencode
import socket
from socketserver import ThreadingMixIn as _ThreadingMixIn
from wsgiref.simple_server import WSGIServer, make_server
from zoneinfo import ZoneInfo


ROOT_DIR = Path(__file__).resolve().parents[2]
INGESTION_SRC = ROOT_DIR / "services" / "ingestion" / "src"
PREDICTION_SRC = ROOT_DIR / "services" / "prediction" / "src"

for candidate in (str(INGESTION_SRC), str(PREDICTION_SRC)):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

from spe_prediction.bankroll import (
    DEFAULT_PROFILE,
    RISK_PROFILES,
    build_parlay_suggestions,
    build_portfolio_plan,
    stake_fraction,
)
from spe_prediction.config import get_env
from spe_prediction.db import DatabaseSettings, connect_db
from spe_prediction.domain import OutcomeProbabilities
from spe_prediction.engine import MatchAnalysisEngine
from spe_prediction.exact_score import build_exact_score_distribution
from spe_prediction.gboost import XGBoostConfig, XGBoostPredictor
from spe_prediction.pipeline import PredictionPipeline
from spe_prediction.rating import (
    BASE_K,
    BASE_RATING,
    HOME_BONUS_ELO,
    _margin_multiplier,
    competition_weight,
    is_neutral_venue,
)
from spe_prediction.repository import PostgresPredictionRepository
from spe_prediction.settlement import settle_pending_bets

import time as _time

from charts import cumulative_series, svg_line_chart, svg_sparkline


XGBOOST_MODEL_FILE = ROOT_DIR / "services" / "prediction" / "models" / "xgboost_1x2.joblib"
GOLF_SPORT_PARAM = "golf"
DEV_SERVER_VERSION = os.environ.get("SPE_DEV_VERSION") or str(int(_time.time() * 1000))
QUEBEC_TZ = ZoneInfo("America/Toronto")
APP_BUILD_VERSION = (
    os.environ.get("APP_BUILD_VERSION")
    or os.environ.get("VERCEL_GIT_COMMIT_SHA")
    or DEV_SERVER_VERSION
)


def serverless_runtime() -> bool:
    return os.environ.get("VERCEL") == "1" or bool(os.environ.get("VERCEL_URL"))


def dev_reload_script() -> str:
    """Auto-refresh tres leger pour npm run dev, sans dependance JS externe."""
    if os.environ.get("SPE_DEV_RELOAD") != "1":
        return ""
    version = escape(DEV_SERVER_VERSION)
    return f"""
<script>
(() => {{
  const current = "{version}";
  async function checkVersion() {{
    try {{
      const response = await fetch('/__dev_version', {{ cache: 'no-store' }});
      if (!response.ok) return;
      const next = (await response.text()).trim();
      if (next && next !== current) window.location.reload();
    }} catch (_error) {{}}
  }}
  setInterval(checkVersion, 900);
}})();
</script>"""


def background_actions_enabled() -> bool:
    override = (os.environ.get("SPE_ALLOW_RUNTIME_ACTIONS") or "").strip()
    if override == "1":
        return True
    return not serverless_runtime()


def background_actions_disabled_message() -> str:
    return (
        "Actions d'ingestion et de prediction desactivees sur l'instance Vercel : "
        "declenche le cycle via le runner externe/cron pour garder une prod stable."
    )


# ---------------------------------------------------------------------------
# Historique Elo (replay complet, cache 15 min) — alimente la trajectoire Elo
# de la page detail et les "mouvements Elo 30 jours" du tableau de bord.
# ---------------------------------------------------------------------------
_ELO_HISTORY_CACHE: dict[str, Any] = {"ts": 0.0, "history": {}, "names": {}}
_ELO_CACHE_TTL_SECONDS = 900.0


def get_elo_history() -> tuple[dict[int, list[tuple[float, float]]], dict[int, str]]:
    """{team_id: [(epoch, rating apres match), ...]} + noms d'equipes."""
    if (
        _time.monotonic() - _ELO_HISTORY_CACHE["ts"] < _ELO_CACHE_TTL_SECONDS
        and _ELO_HISTORY_CACHE["history"]
    ):
        return _ELO_HISTORY_CACHE["history"], _ELO_HISTORY_CACHE["names"]

    connection = connect_db(DatabaseSettings.from_env())
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT f.kickoff_utc, f.home_team_id, f.away_team_id,
                       fs.home_score, fs.away_score, l.league_name
                FROM core.fixtures f
                JOIN core.fixture_scores fs ON fs.fixture_id = f.fixture_id
                JOIN core.leagues l ON l.league_id = f.league_id
                WHERE fs.home_score IS NOT NULL AND f.kickoff_utc IS NOT NULL
                ORDER BY f.kickoff_utc ASC, f.fixture_id ASC
                """
            )
            rows = cursor.fetchall()
            cursor.execute("SELECT team_id, team_name FROM core.teams")
            names = {int(r[0]): str(r[1]) for r in cursor.fetchall()}
    finally:
        connection.close()

    import math as _math

    ratings: dict[int, float] = {}
    history: dict[int, list[tuple[float, float]]] = {}
    for kickoff, home_id, away_id, hs, aws, league in rows:
        home_id, away_id = int(home_id), int(away_id)
        rh = ratings.get(home_id, BASE_RATING)
        ra = ratings.get(away_id, BASE_RATING)
        bonus = 0.0 if is_neutral_venue(str(league)) else HOME_BONUS_ELO
        expected = 1.0 / (1.0 + _math.pow(10.0, -((rh - ra + bonus) / 400.0)))
        actual = 1.0 if hs > aws else 0.5 if hs == aws else 0.0
        k = BASE_K * competition_weight(str(league)) * _margin_multiplier(abs(int(hs) - int(aws)))
        delta = k * (actual - expected)
        ratings[home_id] = rh + delta
        ratings[away_id] = ra - delta
        ts = kickoff.timestamp()
        history.setdefault(home_id, []).append((ts, ratings[home_id]))
        history.setdefault(away_id, []).append((ts, ratings[away_id]))

    _ELO_HISTORY_CACHE.update(ts=_time.monotonic(), history=history, names=names)
    return history, names


def elo_movers(days: int = 30, top_n: int = 5) -> list[dict[str, Any]]:
    """Equipes dont l'Elo a le plus bouge sur N jours (façon "market movers")."""
    history, names = get_elo_history()
    cutoff = _time.time() - days * 86400
    movers: list[dict[str, Any]] = []
    for team_id, points in history.items():
        if len(points) < 5:
            continue
        recent = [p for p in points if p[0] >= cutoff]
        if not recent:
            continue
        before = [p for p in points if p[0] < cutoff]
        start_rating = before[-1][1] if before else recent[0][1]
        delta = recent[-1][1] - start_rating
        movers.append({
            "team_id": team_id,
            "name": names.get(team_id, f"Team {team_id}"),
            "rating": recent[-1][1],
            "delta": delta,
            "spark": [v for _ts, v in points[-20:]],
        })
    movers.sort(key=lambda m: m["delta"], reverse=True)
    return movers[:top_n] + movers[-top_n:][::-1]


def _build_match_engine() -> MatchAnalysisEngine:
    if XGBOOST_MODEL_FILE.exists():
        predictor = XGBoostPredictor(XGBoostConfig(model_path=str(XGBOOST_MODEL_FILE)))
        if predictor.available:
            return MatchAnalysisEngine(xgboost_predictor=predictor)
    return MatchAnalysisEngine()


DEFAULT_LEAGUE = "FIFA World Cup"


@dataclass(frozen=True)
class MetricCard:
    label: str
    value: str
    tone: str = "neutral"


@dataclass(frozen=True)
class ActionReport:
    title: str
    status: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class UserContext:
    user_id: int
    email: str
    display_name: str
    role_code: str

    @property
    def is_admin(self) -> bool:
        return self.role_code == "ADMIN"


SESSION_COOKIE_NAME = "spe_session"
PBKDF2_ITERATIONS = 260_000
AUTH_DISABLED = os.environ.get("SPE_AUTH_DISABLED") == "1"
PERMISSIONS: dict[str, set[str]] = {
    "CLIENT": {
        "PREDICTIONS_VIEW", "DEALS_VIEW", "STRATEGY_VIEW",
        "BACK_VIEW_OWN", "BANKROLL_VIEW_OWN",
        "VALIDATION_REFRESH_OWN", "BANKROLL_MANAGE_OWN",
    },
    "ADMIN": {
        "PREDICTIONS_VIEW", "DEALS_VIEW", "STRATEGY_VIEW",
        "BACK_VIEW_OWN", "BANKROLL_VIEW_OWN",
        "VALIDATION_REFRESH_OWN", "POSITION_WRITE_OWN", "BANKROLL_MANAGE_OWN",
        "BACK_VIEW_ANY", "BANKROLL_VIEW_ANY", "CYCLE_RUN",
        "CLIENT_AUDIT_VIEW", "ADMIN_VIEW",
    },
}


def _session_secret() -> str:
    return get_env("SESSION_SECRET", "local-dev-session-secret-change-me")


def _password_min_length() -> int:
    try:
        return max(8, int(get_env("PASSWORD_MIN_LENGTH", "10") or "10"))
    except ValueError:
        return 10


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("ascii"), PBKDF2_ITERATIONS
    ).hex()
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt}${digest}"


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        scheme, iterations_raw, salt, digest = stored_hash.split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        computed = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt.encode("ascii"), int(iterations_raw)
        ).hex()
        return hmac.compare_digest(computed, digest)
    except Exception:
        return False


def _hash_session_token(token: str) -> str:
    return hashlib.sha256((token + _session_secret()).encode("utf-8")).hexdigest()


def _cookie_value(environ: dict[str, Any], name: str) -> str:
    cookie = SimpleCookie()
    try:
        cookie.load(environ.get("HTTP_COOKIE", "") or "")
    except Exception:
        return ""
    morsel = cookie.get(name)
    return morsel.value if morsel else ""


def has_permission(user: UserContext | None, permission: str) -> bool:
    if AUTH_DISABLED:
        return True
    if user is None:
        return False
    return permission in PERMISSIONS.get(user.role_code, set())


def require_permission(user: UserContext | None, permission: str) -> None:
    if not has_permission(user, permission):
        raise PermissionError(f"Permission requise: {permission}")


def can_refresh_validation(user: UserContext | None) -> bool:
    return has_permission(user, "VALIDATION_REFRESH_OWN")


def can_manage_positions(user: UserContext | None) -> bool:
    return has_permission(user, "POSITION_WRITE_OWN")


def can_manage_bankroll(user: UserContext | None) -> bool:
    return has_permission(user, "BANKROLL_MANAGE_OWN")


def current_user_from_request(environ: dict[str, Any]) -> UserContext | None:
    if AUTH_DISABLED:
        return UserContext(1, "test@bp-edge.local", "Test Admin", "ADMIN")
    token = _cookie_value(environ, SESSION_COOKIE_NAME)
    if not token:
        return None
    connection = connect_db(DatabaseSettings.from_env())
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT u.user_id, u.email, u.display_name, u.role_code
                FROM app_auth.sessions s
                JOIN app_auth.users u ON u.user_id = s.user_id
                WHERE s.token_hash = %s
                  AND s.revoked_at IS NULL
                  AND s.expires_at > now()
                  AND u.status_code = 'ACTIVE'
                  AND u.deleted_at IS NULL
                """,
                (_hash_session_token(token),),
            )
            row = cursor.fetchone()
            if not row:
                return None
            cursor.execute(
                "UPDATE app_auth.sessions SET last_seen_at = now() WHERE token_hash = %s",
                (_hash_session_token(token),),
            )
        connection.commit()
    finally:
        connection.close()
    return UserContext(int(row[0]), str(row[1]), str(row[2]), str(row[3]))


def create_session(user_id: int, environ: dict[str, Any]) -> str:
    token = secrets.token_urlsafe(32)
    ttl_hours = max(1, int(get_env("SESSION_TTL_HOURS", "24") or "24"))
    user_agent = str(environ.get("HTTP_USER_AGENT", ""))[:500]
    ip_address = str(environ.get("REMOTE_ADDR", ""))[:120]
    connection = connect_db(DatabaseSettings.from_env())
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO app_auth.sessions (user_id, token_hash, expires_at, user_agent, ip_address)
                VALUES (%s, %s, now() + (%s || ' hours')::interval, %s, %s)
                """,
                (user_id, _hash_session_token(token), ttl_hours, user_agent, ip_address),
            )
            cursor.execute(
                "UPDATE app_auth.users SET last_login_at = now(), updated_at = now() WHERE user_id = %s",
                (user_id,),
            )
            cursor.execute(
                """
                INSERT INTO app_auth.audit_log (actor_user_id, target_user_id, action_code, route, ip_address, user_agent)
                VALUES (%s, %s, 'LOGIN_SUCCESS', %s, %s, %s)
                """,
                (user_id, user_id, str(environ.get("PATH_INFO", "")), ip_address, user_agent),
            )
        connection.commit()
    finally:
        connection.close()
    return token


def revoke_session(environ: dict[str, Any], user: UserContext | None = None) -> None:
    token = _cookie_value(environ, SESSION_COOKIE_NAME)
    if not token:
        return
    connection = connect_db(DatabaseSettings.from_env())
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE app_auth.sessions SET revoked_at = now() WHERE token_hash = %s",
                (_hash_session_token(token),),
            )
            if user is not None:
                cursor.execute(
                    """
                    INSERT INTO app_auth.audit_log (actor_user_id, target_user_id, action_code, route)
                    VALUES (%s, %s, 'LOGOUT', %s)
                    """,
                    (user.user_id, user.user_id, str(environ.get("PATH_INFO", ""))),
                )
        connection.commit()
    finally:
        connection.close()


def authenticate_user(email: str, password: str, environ: dict[str, Any]) -> UserContext | None:
    connection = connect_db(DatabaseSettings.from_env())
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT user_id, email, display_name, role_code, password_hash
                FROM app_auth.users
                WHERE lower(email) = lower(%s)
                  AND status_code = 'ACTIVE'
                  AND deleted_at IS NULL
                """,
                (email.strip(),),
            )
            row = cursor.fetchone()
            if not row or not verify_password(password, str(row[4])):
                cursor.execute(
                    """
                    INSERT INTO app_auth.audit_log (action_code, route, ip_address, user_agent, metadata)
                    VALUES ('LOGIN_FAILED', %s, %s, %s, %s::jsonb)
                    """,
                    (
                        str(environ.get("PATH_INFO", "")),
                        str(environ.get("REMOTE_ADDR", ""))[:120],
                        str(environ.get("HTTP_USER_AGENT", ""))[:500],
                        json.dumps({"email": email.strip().lower()}),
                    ),
                )
                connection.commit()
                return None
    finally:
        connection.close()
    return UserContext(int(row[0]), str(row[1]), str(row[2]), str(row[3]))


def _safe_next(raw_next: str) -> str:
    if raw_next.startswith("/") and not raw_next.startswith("//"):
        return raw_next
    return "/"


def redirect_response(start_response, location: str, extra_headers: list[tuple[str, str]] | None = None):
    start_response("303 See Other", [("Location", location), *(extra_headers or [])])
    return [b""]


def json_response(start_response, status: str, payload: Mapping[str, Any]) -> list[bytes]:
    body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    start_response(
        status,
        [
            ("Content-Type", "application/json; charset=utf-8"),
            ("Cache-Control", "no-store"),
            ("Content-Length", str(len(body))),
        ],
    )
    return [body]


def latest_migration_name() -> str:
    migration_files = sorted((ROOT_DIR / "db" / "migrations").glob("*.sql"))
    return migration_files[-1].name if migration_files else ""


def check_database_ready() -> tuple[bool, dict[str, Any]]:
    expected_migration = latest_migration_name()
    payload: dict[str, Any] = {
        "database": "down",
        "expected_migration": expected_migration,
    }
    latest_applied = ""
    try:
        connection = connect_db(DatabaseSettings.from_env())
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.execute("SELECT to_regclass('public.schema_migrations')")
                ledger_exists = (cursor.fetchone() or [None])[0] is not None
                if ledger_exists:
                    cursor.execute(
                        "SELECT migration_name FROM public.schema_migrations "
                        "ORDER BY migration_name DESC LIMIT 1"
                    )
                    latest_applied = str((cursor.fetchone() or [""])[0] or "")
        finally:
            connection.close()
    except Exception as exc:
        payload["error"] = str(exc)
        return False, payload

    payload["database"] = "up"
    payload["latest_applied_migration"] = latest_applied
    payload["migration_in_sync"] = bool(
        not expected_migration or latest_applied == expected_migration
    )
    if expected_migration and latest_applied != expected_migration:
        payload["error"] = (
            f"Migration attendue {expected_migration}, "
            f"mais base sur {latest_applied or 'aucune'}"
        )
        return False, payload
    return True, payload


def release_payload() -> dict[str, Any]:
    return {
        "app": "bp-edge-dashboard",
        "environment": os.environ.get("APP_ENV", "local"),
        "version": APP_BUILD_VERSION,
        "commit_sha": os.environ.get("VERCEL_GIT_COMMIT_SHA", ""),
        "host": socket.gethostname(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def merge_query_string(qs: str, updates: Mapping[str, str]) -> str:
    params = parse_qs(qs, keep_blank_values=True)
    for key, value in updates.items():
        if value:
            params[key] = [value]
        elif key in params:
            params.pop(key, None)
    flattened = {key: values[-1] for key, values in params.items() if values}
    return urlencode(flattened)


def append_url_params(url: str, updates: Mapping[str, str]) -> str:
    query = ""
    base = url
    if "?" in url:
        base, query = url.split("?", 1)
    merged = merge_query_string(query, updates)
    return f"{base}?{merged}" if merged else base


def user_action_error(params: Mapping[str, list[str]]) -> str | None:
    value = (params.get("action_error", [""])[0] or "").strip()
    return value[:240] if value else None


# =============================================================================
# Systeme visuel — Style Typographique International (Swiss)
# Blanc pur / Helvetica seule famille / filets 1px / accent unique #E4002B
# reserve au signal "value". Numeraux tabulaires partout, zero radius,
# zero ombre. Le differenciateur : le rail de probabilites.
# =============================================================================
BASE_CSS = """
  /* ================================================================
     BPREDICTION — Sports Trading Intelligence
     DA inspiree du premium fintech (CMC Markets) : autorite marine,
     accent teal electrique, or de confiance, chiffres tabulaires
     geants, profondeur douce, rythme hero-marine / corps-clair.
     Noms de classes inchanges (aucune casse de markup).
     ================================================================ */
  :root {
    --navy: #0A1A2F;           /* encre marine : hero, nav, verdict */
    --navy-2: #12273F;
    --navy-3: #1C3A5C;
    --bg: #EEF2F8;             /* fond application, frais */
    --paper: #FFFFFF;          /* cartes / surfaces */
    --paper-2: #F4F7FB;
    --ink: #0C1B2E;            /* texte principal */
    --muted: #64748B;          /* texte secondaire (slate) */
    --slate: #9FB2C9;          /* texte sur fond marine */
    --rule: #E3E9F2;           /* filets */
    --rule-strong: #0C1B2E;
    --line: #E3E9F2;
    --panel: #FFFFFF;
    --teal: #0C8F97;           /* accent marque : CTA, remplissages (texte blanc OK) */
    --teal-bright: #17C4CE;    /* accent lumineux : filets, glow, barres actives */
    --teal-soft: rgba(12,143,151,.10);
    --gold: #B8860B;           /* confiance / verifie (texte sur blanc) */
    --gold-fill: #F0B429;
    --win: #0E9F6E;            /* gagne / profit */
    --loss: #E02D3C;           /* perdu / erreur */
    --edge: #0C8F97;           /* alias accent (utilise partout) */
    --edge-bright: #17C4CE;
    --accent: #0C8F97;
    --red: #E02D3C;
    --shadow-sm: 0 1px 2px rgba(12,27,46,.06);
    --shadow: 0 1px 2px rgba(12,27,46,.05), 0 14px 34px rgba(12,27,46,.08);
    --shadow-navy: 0 18px 44px rgba(10,26,47,.30);
    --radius: 16px;
    --radius-sm: 10px;
  }
  * { box-sizing: border-box; margin: 0; }
  html { -webkit-text-size-adjust: 100%; scroll-behavior: smooth; }
  body {
    background:
      radial-gradient(1100px 460px at 88% -160px, rgba(23,196,206,.10), transparent 62%),
      radial-gradient(900px 420px at 6% -120px, rgba(10,26,47,.05), transparent 60%),
      var(--bg);
    color: var(--ink);
    font-family: "Inter", "SF Pro Display", "Segoe UI", system-ui, -apple-system, Helvetica, Arial, sans-serif;
    font-size: 15px;
    line-height: 1.5;
    font-variant-numeric: tabular-nums;
    -webkit-font-smoothing: antialiased;
  }
  a { color: var(--ink); }
  .shell { width: min(1320px, calc(100% - 40px)); margin: 0 auto; padding: 14px 0 120px; }

  /* --- Navigation produit (barre marine, sticky) ------------------------- */
  .sportnav {
    position: sticky; top: 12px; z-index: 50;
    display: grid; grid-template-columns: auto 1fr auto; gap: 18px; align-items: center;
    background: linear-gradient(135deg, var(--navy) 0%, #0D2039 100%);
    border: 1px solid rgba(255,255,255,.06);
    border-radius: var(--radius);
    padding: 11px 18px; margin-bottom: 18px;
    box-shadow: var(--shadow-navy);
  }
  .nav-primary, .nav-access { display: flex; align-items: center; gap: 10px; min-width: 0; }
  .nav-primary { justify-content: center; }
  .nav-access { justify-content: flex-end; }
  .brand {
    display: inline-flex; align-items: center; gap: 9px;
    text-decoration: none; margin-right: 6px; line-height: 1;
  }
  .brand::before {
    content: ""; width: 12px; height: 22px; border-radius: 3px;
    background: linear-gradient(180deg, var(--teal-bright), var(--teal));
    box-shadow: 0 0 16px rgba(23,196,206,.55);
    transform: skewX(-10deg);
  }
  .brand b {
    font-weight: 800; font-size: 20px; letter-spacing: -0.03em; color: #fff;
  }
  .brand b i { color: var(--teal-bright); font-style: normal; }
  .brand small {
    margin-left: 8px; font-size: 9px; font-weight: 700; color: var(--slate);
    letter-spacing: .22em; text-transform: uppercase; align-self: center;
    border-left: 1px solid rgba(255,255,255,.14); padding-left: 8px;
  }
  .sport-switch { display: flex; gap: 6px; align-items: center; }
  .sport-pill {
    padding: 8px 15px; border: 1px solid rgba(255,255,255,.16); border-radius: 999px;
    text-decoration: none; color: var(--slate); font-weight: 700; font-size: 12px;
    text-transform: uppercase; letter-spacing: .06em; transition: all .16s;
  }
  .sport-pill:hover { color: #fff; border-color: rgba(255,255,255,.45); }
  .sport-pill.active {
    background: linear-gradient(180deg, var(--teal-bright), var(--teal));
    color: var(--navy); border-color: transparent; font-weight: 800;
    box-shadow: 0 4px 14px rgba(23,196,206,.35);
  }
  .nav-tabs { display: flex; gap: 3px; flex-wrap: wrap; justify-content: center; }
  .nav-tab {
    padding: 9px 14px; text-decoration: none; color: var(--slate);
    font-size: 12px; font-weight: 700; border-radius: 8px;
    text-transform: uppercase; letter-spacing: .05em;
    border-bottom: 2px solid transparent;
  }
  .nav-tab:hover { color: #fff; background: rgba(255,255,255,.06); }
  .nav-tab.active { color: var(--teal-bright); border-bottom-color: var(--teal-bright); }
  .account-chip, .system-chip {
    display: inline-flex; align-items: center; justify-content: center;
    min-height: 34px; padding: 7px 12px; border: 1px solid rgba(255,255,255,.16);
    color: var(--slate); background: rgba(255,255,255,.04); font-size: 11px;
    font-weight: 700; text-transform: uppercase; letter-spacing: .06em;
    white-space: nowrap; border-radius: 999px; text-decoration: none;
  }
  .account-chip:hover, .system-chip:hover { color: #fff; border-color: rgba(255,255,255,.4); }
  .account-chip.active { color: var(--navy); background: var(--teal-bright); border-color: transparent; }
  .account-chip.locked { border-style: dashed; }
  .system-chip { color: var(--teal-bright); border-color: rgba(23,196,206,.4); }

  /* --- Hero (masthead / matchline) : bande marine emotionnelle ----------- */
  .masthead, .matchline {
    --hero-img: url('/static/stadium.jpg');
    display: grid; grid-template-columns: 1fr auto; align-items: center; gap: 24px;
    background:
      radial-gradient(680px 300px at 92% -60px, rgba(23,196,206,.22), transparent 60%),
      linear-gradient(115deg, rgba(10,26,47,.94) 0%, rgba(13,33,64,.88) 48%, rgba(10,26,47,.82) 100%),
      var(--hero-img) center/cover no-repeat;
    background-color: var(--navy);
    border-radius: var(--radius);
    padding: 34px 34px 32px;
    color: #fff; box-shadow: var(--shadow-navy);
    margin-bottom: 18px; position: relative; overflow: hidden;
  }
  .masthead.hero-golf, .matchline.hero-golf { --hero-img: url('/static/golf.jpg'); }
  /* Landing (login/signup) : photo assumee, voile plus leger, hero plus haut. */
  .masthead.hero-auth {
    min-height: 320px; align-items: end;
    background:
      radial-gradient(700px 320px at 88% 10%, rgba(23,196,206,.28), transparent 58%),
      linear-gradient(105deg, rgba(10,26,47,.90) 0%, rgba(13,33,64,.66) 52%, rgba(10,26,47,.42) 100%),
      url('/static/stadium.jpg') center/cover no-repeat;
    background-color: var(--navy);
  }
  .masthead.hero-auth.golf { background-image:
      radial-gradient(700px 320px at 88% 10%, rgba(23,196,206,.28), transparent 58%),
      linear-gradient(105deg, rgba(10,26,47,.90) 0%, rgba(13,33,64,.66) 52%, rgba(10,26,47,.42) 100%),
      url('/static/golf2.jpg'); }
  .masthead::before, .matchline::before {
    content: ""; position: absolute; left: 0; top: 0; bottom: 0; width: 5px;
    background: linear-gradient(180deg, var(--teal-bright), var(--teal));
  }
  .masthead h1, .matchline h1 {
    font-size: clamp(27px, 3.7vw, 46px); font-weight: 800;
    letter-spacing: -0.035em; line-height: 1.0; color: #fff;
  }
  .masthead .sub, .matchline .meta { color: var(--slate); margin-top: 11px; font-size: 14.5px; max-width: 74ch; }
  .masthead .sub a.detail-link, .matchline .meta a.detail-link { color: var(--teal-bright); border-bottom-color: rgba(23,196,206,.5); }
  .masthead .folio {
    font-size: 10.5px; font-weight: 800; letter-spacing: .16em; text-transform: uppercase;
    color: var(--teal-bright); border: 1px solid rgba(23,196,206,.4);
    padding: 8px 14px; border-radius: 999px; white-space: nowrap;
    background: rgba(23,196,206,.08);
  }
  .hero-visual {
    width: clamp(180px, 24vw, 320px); height: 132px; position: relative;
    border: 1px solid rgba(255,255,255,.14); border-radius: 12px;
    background: rgba(255,255,255,.03); overflow: hidden;
  }
  .hero-visual::before { content: ""; position: absolute; inset: 20px 32px; border: 2px solid rgba(255,255,255,.20); border-radius: 6px; }
  .hero-visual::after {
    content: ""; position: absolute; left: 50%; top: 50%;
    width: 44px; height: 44px; transform: translate(-50%, -50%);
    border: 2px solid var(--teal-bright); border-radius: 50%;
    box-shadow: 0 0 24px rgba(23,196,206,.5);
  }
  .hero-visual .ball {
    position: absolute; right: 44px; bottom: 26px;
    width: 16px; height: 16px; border-radius: 50%;
    background: var(--teal-bright); box-shadow: 0 0 16px rgba(23,196,206,.7), 20px -44px 0 -4px rgba(255,255,255,.5);
  }
  .hero-visual.golf::before { inset: auto 30px 24px 30px; height: 36px; border-radius: 999px; border-color: rgba(255,255,255,.18); }
  .hero-visual.golf::after { left: 68%; top: 36%; width: 12px; height: 12px; background: var(--teal-bright); border: none; box-shadow: 0 0 18px rgba(23,196,206,.6); }
  .hero-visual.golf .ball { right: 68px; bottom: 58px; width: 2px; height: 48px; border-radius: 0; background: rgba(255,255,255,.6); box-shadow: none; }
  .hero-visual.golf .ball::after { content: ""; position: absolute; left: -4px; top: -7px; width: 10px; height: 10px; border-radius: 50%; background: var(--teal-bright); box-shadow: 0 0 14px rgba(23,196,206,.7); }
  .matchline .verdict-no { font-size: clamp(40px, 6vw, 78px); font-weight: 800; line-height: .9; letter-spacing: -0.03em; color: var(--teal-bright); }
  .matchline .verdict-no small { display: block; font-size: 12px; font-weight: 700; color: var(--slate); letter-spacing: .08em; text-transform: uppercase; margin-top: 6px; }

  /* --- Barre d'actions ---------------------------------------------------- */
  .actions {
    display: flex; flex-wrap: wrap; gap: 10px; align-items: center;
    background: var(--paper); border: 1px solid var(--rule);
    border-radius: var(--radius); padding: 13px 15px;
    box-shadow: var(--shadow); margin-bottom: 16px;
  }
  .actions form { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; }
  .actions select {
    font: inherit; color: var(--ink); background: var(--paper-2);
    border: 1px solid var(--rule); border-radius: var(--radius-sm);
    padding: 10px 14px; min-width: 240px;
  }
  .actions button {
    font: inherit; font-weight: 700; cursor: pointer;
    background: var(--paper); color: var(--ink);
    border: 1px solid var(--rule); border-radius: var(--radius-sm);
    padding: 10px 17px; transition: all .16s;
  }
  .actions button:hover { border-color: var(--navy); }
  .actions button.primary {
    background: linear-gradient(180deg, var(--teal), #0A7B84); color: #fff; border-color: transparent;
    box-shadow: 0 6px 16px rgba(12,143,151,.30);
  }
  .actions button.primary:hover { filter: brightness(1.06); }
  .actions.dashboard-actions form:first-child { flex: 1 1 520px; }
  .actions.dashboard-actions form:first-child select { flex: 1 1 520px; min-width: min(520px, 100%); }
  .actions.dashboard-actions form + form { margin-left: auto; }
  button, select, input, summary, a { transition: border-color .16s, background-color .16s, color .16s, box-shadow .16s; }
  button:focus-visible, select:focus-visible, input:focus-visible, summary:focus-visible, a:focus-visible {
    outline: 2px solid var(--teal); outline-offset: 2px;
  }
  button:disabled, select:disabled, input:disabled { opacity: .55; cursor: not-allowed; }

  /* --- Bande de metriques (cartes, chiffres geants) ---------------------- */
  .metrics {
    display: grid; gap: 13px;
    grid-template-columns: repeat(auto-fit, minmax(165px, 1fr));
    margin-bottom: 6px;
  }
  .metric {
    background: var(--paper); border: 1px solid var(--rule);
    border-radius: var(--radius); padding: 17px 18px 15px;
    box-shadow: var(--shadow); position: relative; overflow: hidden;
  }
  .metric::before { content: ""; position: absolute; inset: 0 auto 0 0; width: 4px; background: var(--rule); }
  .metric .value { font-size: 34px; font-weight: 800; letter-spacing: -0.025em; line-height: 1; color: var(--navy); }
  .metric .label { color: var(--muted); font-size: 11px; margin-top: 8px; font-weight: 700; letter-spacing: .06em; text-transform: uppercase; }
  .metric.accent::before { background: linear-gradient(180deg, var(--teal-bright), var(--teal)); }
  .metric.accent .value { color: var(--teal); }
  .metric.negative .value { color: var(--muted); }
  .metric.timestamp::before { background: var(--navy); }
  .metric.timestamp .value { font-size: 20px; line-height: 1.1; letter-spacing: -0.01em; }

  /* --- Filtres (chips) ----------------------------------------------------- */
  .filters {
    display: flex; flex-wrap: wrap; align-items: center; gap: 6px;
    margin-top: 14px; background: var(--paper); border: 1px solid var(--rule);
    border-radius: var(--radius); padding: 11px 13px; width: fit-content; box-shadow: var(--shadow);
  }
  .filters-label { padding: 6px 8px; font-size: 10.5px; font-weight: 800; color: var(--muted); letter-spacing: 0.1em; text-transform: uppercase; }
  .chip { padding: 7px 15px; font-size: 13px; font-weight: 700; color: var(--ink); text-decoration: none; border-radius: 999px; border: 1px solid var(--rule); }
  .chip:hover { border-color: var(--navy); }
  .chip.active { background: var(--navy); color: #fff; border-color: var(--navy); }
  .thresholds { display: flex; align-items: center; gap: 6px; }
  .thresholds label { padding: 6px 4px 6px 10px; font-size: 10.5px; font-weight: 800; color: var(--muted); letter-spacing: 0.1em; text-transform: uppercase; white-space: nowrap; }
  .thresholds select { font: inherit; font-size: 13px; color: var(--ink); background: var(--paper-2); border: 1px solid var(--rule); border-radius: var(--radius-sm); padding: 6px 10px; }
  .thresholds button { font: inherit; font-size: 13px; font-weight: 700; cursor: pointer; background: var(--paper); color: var(--ink); border: 1px solid var(--rule); border-radius: var(--radius-sm); padding: 6px 14px; }
  .thresholds button:hover { border-color: var(--navy); }
  .periodbar {
    display: flex; flex-wrap: wrap; align-items: center; gap: 10px;
    background: var(--paper); border: 1px solid var(--rule); border-radius: var(--radius);
    padding: 13px 15px; width: 100%; box-shadow: var(--shadow); margin-top: 14px;
  }
  .periodbar .label { font-size: 10.5px; font-weight: 800; color: var(--muted); letter-spacing: .1em; text-transform: uppercase; margin-right: 2px; }
  .periodbar .period-chip {
    display: inline-flex; align-items: center; justify-content: center;
    min-height: 36px; padding: 8px 16px; border: 1px solid var(--rule);
    background: #fff; color: var(--ink); text-decoration: none;
    font-size: 12px; font-weight: 800; letter-spacing: .02em; border-radius: 999px;
  }
  .periodbar .period-chip:hover { border-color: var(--navy); }
  .periodbar .period-chip.active { background: linear-gradient(180deg, var(--teal), #0A7B84); border-color: transparent; color: #fff; box-shadow: 0 4px 12px rgba(12,143,151,.3); }
  .periodbar .thresholds { margin-left: 8px; }
  .periodbar .thresholds label { padding-left: 4px; }

  /* --- En-tetes triables --------------------------------------------------- */
  th a.sort { color: var(--muted); text-decoration: none; padding: 2px 6px; margin: -2px -4px; border-radius: 6px; }
  th a.sort:hover { color: var(--ink); }
  th a.sort.active { background: var(--navy); color: #fff; padding: 3px 18px 3px 8px; position: relative; }
  th a.sort.active::after { content: ""; position: absolute; right: 6px; top: 50%; border-left: 4px solid transparent; border-right: 4px solid transparent; }
  th a.sort.active.caret-up::after { border-bottom: 5px solid var(--teal-bright); margin-top: -3px; }
  th a.sort.active.caret-down::after { border-top: 5px solid var(--teal-bright); margin-top: -2px; }

  /* --- Sections (cartes) --------------------------------------------------- */
  .section {
    margin-top: 18px; background: var(--paper);
    border: 1px solid var(--rule); border-radius: var(--radius);
    padding: 22px 24px 24px; box-shadow: var(--shadow);
  }
  .section-head {
    display: grid; grid-template-columns: auto minmax(0, 1fr) minmax(220px, 52ch);
    align-items: center; gap: 12px;
    padding-bottom: 13px; margin-bottom: 8px; border-bottom: 1px solid var(--rule);
  }
  .section-head .no {
    font-size: 12px; font-weight: 800; color: #fff;
    background: linear-gradient(180deg, var(--teal), #0A7B84); padding: 5px 10px; border-radius: 8px;
    letter-spacing: .02em; box-shadow: 0 3px 10px rgba(12,143,151,.28);
  }
  .section-head h2 { font-size: 20px; font-weight: 800; letter-spacing: -0.02em; color: var(--navy); }
  .section-head .note { color: var(--muted); font-size: 12.5px; max-width: 52ch; text-align: right; justify-self: end; }

  /* --- Tables -------------------------------------------------------------- */
  .table-wrap { overflow-x: auto; border-radius: var(--radius-sm); }
  table { width: 100%; border-collapse: collapse; }
  th { text-align: left; font-size: 10.5px; font-weight: 800; color: var(--muted); letter-spacing: 0.08em; text-transform: uppercase; padding: 12px 10px 9px; border-bottom: 2px solid var(--rule); white-space: nowrap; }
  td { padding: 13px 10px; border-bottom: 1px solid var(--rule); font-size: 14px; vertical-align: middle; }
  tbody tr { transition: background-color .12s; }
  tbody tr:hover td { background: var(--paper-2); }
  td.num, th.num { text-align: right; }
  .empty { color: var(--muted); text-align: center; padding: 32px 12px; }
  .muted { color: var(--muted); }

  /* --- Rail de probabilites ------------------------------------------------ */
  .rail { display: flex; height: 42px; min-width: 260px; border-radius: var(--radius-sm); overflow: hidden; border: 1px solid var(--rule); }
  .rail .seg { display: flex; align-items: center; justify-content: center; font-weight: 800; font-size: 14px; min-width: 30px; border-right: 1px solid var(--rule); background: var(--paper-2); color: var(--ink); }
  .rail .seg:last-child { border-right: none; }
  .rail .seg.lead { background: var(--navy); color: #fff; }
  .rail .seg.lead.value { background: linear-gradient(180deg, var(--teal), #0A7B84); color: #fff; }
  .rail-hero { height: 90px; }
  .rail-hero .seg { font-size: clamp(20px, 3vw, 34px); }
  .rail-legend { display: flex; justify-content: space-between; color: var(--muted); font-size: 12px; margin-top: 6px; }

  /* --- Encodage semantique ------------------------------------------------- */
  .sig { color: var(--teal); font-weight: 800; }
  .pick { display: inline-block; padding: 3px 11px; border-radius: 999px; border: 1px solid var(--navy); color: var(--navy); font-size: 12px; font-weight: 800; letter-spacing: 0.02em; }
  .pick.lead { background: var(--navy); color: #fff; }
  .pick.sig { border-color: var(--teal); color: var(--teal); background: var(--teal-soft); }
  .chip-won { background: var(--win); color: #fff; padding: 2px 11px; border-radius: 999px; font-size: 12px; font-weight: 800; }
  .chip-lost { border: 1px solid var(--loss); color: var(--loss); padding: 1px 10px; border-radius: 999px; font-size: 12px; font-weight: 800; }
  .badge-valid { color: var(--gold); font-weight: 800; font-size: 12px; letter-spacing: 0.04em; }
  .badge-info { color: var(--muted); font-size: 12px; }
  .form-badge { display: inline-flex; width: 24px; height: 24px; align-items: center; justify-content: center; font-weight: 800; font-size: 12px; border-radius: 7px; }
  .form-w { background: var(--win); color: #fff; }
  .form-n { background: var(--paper-2); color: var(--ink); border: 1px solid var(--rule); }
  .form-l { border: 1px solid var(--loss); color: var(--loss); }
  .best-odd { background: var(--navy); color: var(--teal-bright); font-weight: 800; border-radius: 7px; }
  a.detail-link { color: var(--teal); font-weight: 700; text-decoration: none; border-bottom: 1px solid rgba(12,143,151,.45); }
  a.detail-link:hover { border-bottom-color: var(--teal); }
  .score-stack { display: grid; gap: 8px; min-width: 200px; }
  .score-stack.compact { gap: 6px; min-width: 176px; }
  .score-chip { display: grid; grid-template-columns: auto 1fr; gap: 10px; align-items: baseline; padding: 9px 13px; border: 1px solid var(--rule); border-radius: var(--radius-sm); background: var(--paper-2); }
  .score-stack.compact .score-chip { padding: 6px 10px; }
  .score-chip strong { font-size: 18px; font-weight: 800; letter-spacing: -0.02em; line-height: 1; color: var(--navy); }
  .score-stack.compact .score-chip strong { font-size: 15px; }
  .score-chip span { color: var(--muted); font-size: 12px; }
  .score-note { color: var(--muted); font-size: 13px; line-height: 1.6; margin-top: 12px; max-width: 90ch; }

  /* --- Lignes d'explication ------------------------------------------------ */
  tr.explain-row td { padding: 0 10px 14px; border-bottom: 1px solid var(--rule); }
  tr.explain-row summary { cursor: pointer; color: var(--teal); font-size: 13px; font-weight: 700; list-style: none; }
  tr.explain-row summary::-webkit-details-marker { display: none; }
  tr.explain-row p { color: var(--muted); font-size: 13.5px; line-height: 1.6; margin-top: 8px; max-width: 90ch; }

  /* --- Courbes ------------------------------------------------------------- */
  .chart { margin: 14px 0 6px; border: 1px solid var(--rule); border-radius: var(--radius-sm); padding: 14px 12px 8px; background: linear-gradient(180deg, var(--paper), var(--paper-2)); }
  .chart-legend { display: flex; flex-wrap: wrap; gap: 18px; padding: 8px 6px 6px; border-top: 1px solid var(--rule); }
  .chart-key { font-size: 12.5px; color: var(--muted); display: inline-flex; align-items: center; gap: 6px; }
  .chart-key strong { color: var(--ink); font-weight: 700; }
  .chart-swatch { display: inline-block; width: 14px; height: 3px; border-radius: 2px; }
  .sub-head { font-size: 12.5px; font-weight: 800; margin: 22px 0 8px; color: var(--navy); letter-spacing: .06em; text-transform: uppercase; display: flex; align-items: center; gap: 8px; }
  .sub-head::before { content: ""; width: 14px; height: 3px; border-radius: 2px; background: var(--teal); }

  /* --- Cartes pilotage ----------------------------------------------------- */
  .run-grid { display: grid; gap: 13px; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); }
  .run-card { padding: 17px; border: 1px solid var(--rule); border-radius: var(--radius-sm); background: var(--paper-2); }
  .run-card h3 { font-size: 15px; font-weight: 800; margin-bottom: 8px; color: var(--navy); }
  .run-card p { font-size: 13px; color: var(--muted); margin: 3px 0; }
  .run-card p strong { color: var(--ink); font-weight: 700; }

  /* --- Rapport d'action ---------------------------------------------------- */
  .flash { background: var(--paper); border: 1px solid var(--rule); border-left: 4px solid var(--teal); border-radius: var(--radius-sm); margin-top: 16px; padding: 18px; box-shadow: var(--shadow); }
  .flash.flash-error { border-left-color: var(--loss); }
  .flash h2 { font-size: 16px; font-weight: 800; color: var(--navy); }
  .flash pre { margin-top: 10px; padding: 12px; overflow-x: auto; background: var(--navy); color: #D6E2F0; border: none; border-radius: var(--radius-sm); font-size: 12.5px; font-family: "SF Mono", Consolas, monospace; }
  .report { background: var(--paper); border: 1px solid var(--rule); border-left: 4px solid var(--teal); border-radius: var(--radius-sm); margin: 0 0 16px; padding: 15px 17px; box-shadow: var(--shadow); color: var(--muted); font-size: 14px; }
  .report strong { color: var(--navy); }
  .report.error { border-left-color: var(--loss); }
  .report.error strong { color: var(--loss); }

  /* --- Modal de prise ------------------------------------------------------ */
  .prise-btn { font: inherit; font-size: 12px; letter-spacing: .02em; padding: 7px 15px; background: linear-gradient(180deg, var(--teal), #0A7B84); color: #fff; border: none; cursor: pointer; border-radius: var(--radius-sm); font-weight: 700; box-shadow: 0 4px 12px rgba(12,143,151,.28); }
  .prise-btn:hover { filter: brightness(1.07); }
  .prise-modal { border: 1px solid var(--rule); border-radius: var(--radius); padding: 26px 28px; max-width: 420px; width: 92%; box-shadow: 0 30px 80px rgba(10,26,47,.42); font-family: inherit; color: var(--ink); }
  .prise-modal::backdrop { background: rgba(10,26,47,.55); backdrop-filter: blur(3px); }
  .modal-title { margin: 0 0 4px; font-size: 20px; letter-spacing: -.02em; color: var(--navy); font-weight: 800; }
  .modal-sub { margin: 0 0 16px; font-size: 13px; color: var(--muted); }
  .form-field { display: flex; flex-direction: column; gap: 6px; font-size: 11px; letter-spacing: .05em; text-transform: uppercase; color: var(--muted); font-weight: 700; }
  .form-field input, .form-field select, .input-compact { font: inherit; padding: 9px; border: 1px solid var(--line); background: var(--paper); color: var(--ink); border-radius: var(--radius-sm); text-transform: none; letter-spacing: 0; }
  .form-field input { width: 118px; }
  .modal-field { margin-bottom: 14px; }
  .modal-grid { display: flex; gap: 12px; margin-bottom: 16px; flex-wrap: wrap; }
  .form-actions { display: flex; gap: 8px; justify-content: flex-end; align-items: center; }
  .button-secondary, .button-primary, .button-danger { font: inherit; padding: 10px 18px; border: 1px solid var(--line); cursor: pointer; background: var(--paper); color: var(--ink); border-radius: var(--radius-sm); font-weight: 700; }
  .button-secondary:hover { border-color: var(--navy); }
  .button-primary { background: linear-gradient(180deg, var(--teal), #0A7B84); color: #fff; border-color: transparent; box-shadow: 0 6px 16px rgba(12,143,151,.3); }
  .button-primary:hover { filter: brightness(1.06); }
  .button-danger { color: var(--loss); border-color: rgba(224,45,60,.4); }
  .button-danger:hover { background: rgba(224,45,60,.06); }
  .inline-form { display: inline; margin: 0; }
  .inline-bet-form { margin: 0; display: grid; grid-template-columns: minmax(72px, auto) 72px 62px auto; gap: 5px; align-items: center; }
  .taken-meta { display: flex; align-items: center; gap: 6px; margin-bottom: 4px; white-space: nowrap; flex-wrap: wrap; }
  .cell-nowrap { white-space: nowrap; }
  .input-odd { width: 72px; font-size: 12px; }
  .input-stake { width: 62px; font-size: 12px; }
  .select-market { width: 160px; font-size: 12px; }
  .button-compact { font: inherit; font-size: 12px; padding: 6px 10px; background: var(--navy); color: #fff; border: 1px solid var(--navy); cursor: pointer; border-radius: 8px; font-weight: 700; }
  .button-compact:hover { background: var(--navy-2); }
  .button-mini { font: inherit; font-size: 11px; padding: 4px 9px; border: 1px solid var(--line); background: var(--paper); cursor: pointer; border-radius: 7px; font-weight: 600; }
  .button-mini:hover { border-color: var(--navy); }
  .ticket-details { display: inline-block; margin: 0 6px 0 0; }
  .ticket-details summary { cursor: pointer; font-size: 12px; color: var(--teal); font-weight: 700; }
  .ticket-list { display: flex; gap: 4px; flex-wrap: wrap; margin-top: 4px; }
  .golf-take-form { display: inline-flex; gap: 4px; align-items: center; margin: 0; flex-wrap: wrap; }
  .note-form { margin: 0; display: flex; gap: 4px; align-items: center; }
  .note-form input { font: inherit; font-size: 12px; padding: 5px 8px; border: 1px solid var(--line); border-radius: 7px; width: 150px; background: var(--paper); }
  .ticket-chip { display: inline-flex; align-items: center; gap: 5px; border: 1px solid var(--line); border-radius: 999px; padding: 3px 9px; margin: 2px; font-size: 12px; background: var(--paper-2); }
  .button-ghost-danger { font: inherit; font-size: 12px; padding: 0 5px; border: none; background: none; color: var(--loss); cursor: pointer; font-weight: 800; }
  .subline { font-size: 12px; }
  .tiny-label { font-size: 11px; margin-right: 6px; }

  /* --- Divers -------------------------------------------------------------- */
  .back { display: inline-flex; align-items: center; min-height: 34px; margin: 4px 0 10px; padding: 7px 13px; color: var(--muted); font-size: 13px; font-weight: 700; text-decoration: none; border: 1px solid var(--rule); background: var(--paper); border-radius: 999px; }
  .back:hover { color: var(--navy); border-color: var(--navy); }
  .verdict-strip {
    display: flex; flex-wrap: wrap; gap: 0;
    background:
      radial-gradient(500px 200px at 100% 0%, rgba(23,196,206,.16), transparent 60%),
      linear-gradient(135deg, var(--navy), #0E2648);
    border-radius: var(--radius); overflow: hidden; margin: 4px 0 16px;
    box-shadow: var(--shadow-navy);
  }
  .verdict-strip .cell { padding: 18px 24px 16px; border-right: 1px solid rgba(255,255,255,.08); }
  .verdict-strip .cell .v { font-size: 26px; font-weight: 800; color: var(--teal-bright); letter-spacing: -0.015em; }
  .verdict-strip .cell .l { color: var(--slate); font-size: 11px; margin-top: 4px; font-weight: 700; letter-spacing: .06em; text-transform: uppercase; }
  .prose { font-size: 15.5px; line-height: 1.7; max-width: 92ch; padding: 14px 0 4px; }
  .grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 32px; }
  .golf-tourneys { display: flex; flex-wrap: wrap; gap: 10px; margin: 14px 0 6px; }
  .golf-chip { display: flex; flex-direction: column; gap: 2px; padding: 11px 17px; border: 1px solid var(--rule); border-radius: var(--radius-sm); text-decoration: none; color: var(--ink); background: var(--paper); box-shadow: var(--shadow-sm); transition: all .16s; }
  .golf-chip:hover { border-color: var(--teal); transform: translateY(-1px); }
  .golf-chip.active { border-color: var(--teal); background: var(--teal-soft); }
  .golf-chip-title { font-weight: 800; font-size: 14px; color: var(--navy); }
  .golf-chip-sub { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: .05em; }
  .golf-tourney-head { padding: 14px 0 4px; border-bottom: 1px solid var(--rule); margin-bottom: 8px; }
  .golf-tourney-head h2 { margin: 0; font-size: 22px; font-weight: 800; color: var(--navy); }
  .golf-tourney-head p { margin: 4px 0 0; color: var(--muted); font-size: 13px; }
  .client-hint { border: 1px solid rgba(12,143,151,.25); background: var(--teal-soft); border-radius: var(--radius-sm); padding: 13px 15px; margin: 12px 0 8px; font-size: 14px; line-height: 1.5; }
  .taken-row td { background: var(--teal-soft); }
  .taken-row td:first-child { border-left: 3px solid var(--teal); }
  .filterbar { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 14px; padding: 19px; align-items: end; background: var(--paper); border: 1px solid var(--rule); border-radius: var(--radius); box-shadow: var(--shadow); margin-bottom: 6px; }
  .filterbar label { display: flex; flex-direction: column; gap: 6px; font-size: 10.5px; letter-spacing: .08em; text-transform: uppercase; color: var(--muted); font-weight: 800; }
  .filterbar select, .filterbar input { font: inherit; padding: 10px 11px; border: 1px solid var(--rule); border-radius: var(--radius-sm); background: var(--paper-2); }
  .filterbar .filter-actions { display: flex; gap: 10px; align-items: center; grid-column: 1 / -1; }
  .filterbar button { border-radius: var(--radius-sm) !important; }
  .validation-form { margin: 12px 0 0; display: flex; gap: 10px; align-items: center; flex-wrap: wrap; padding: 13px 15px; border: 1px solid var(--rule); border-radius: var(--radius-sm); background: var(--paper-2); }
  .validation-form .muted { font-size: 13px; }
  .profile-bar { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 10px; }
  .strategy-copy { color: var(--muted); font-size: 13px; margin: 0 0 8px; max-width: 100ch; }
  .product-footer {
    margin-top: 40px; padding: 26px 26px 22px;
    background: linear-gradient(135deg, var(--navy), #0D2039);
    border-radius: var(--radius); box-shadow: var(--shadow-navy);
    display: grid; grid-template-columns: 1.15fr .85fr .85fr; gap: 26px;
    color: var(--slate); font-size: 12.5px;
  }
  .product-footer h2 { font-size: 12px; line-height: 1.1; color: #fff; letter-spacing: .12em; text-transform: uppercase; margin-bottom: 9px; }
  .product-footer p { margin: 0; max-width: 72ch; }
  .product-footer strong { color: var(--teal-bright); font-weight: 800; }
  .product-footer .footer-line { display: flex; flex-wrap: wrap; gap: 8px 12px; margin-top: 12px; }
  .product-footer .footer-tag { border: 1px solid rgba(255,255,255,.14); background: rgba(255,255,255,.04); border-radius: 999px; padding: 5px 11px; color: #fff; font-weight: 700; letter-spacing: .05em; text-transform: uppercase; font-size: 10.5px; }
  .table-wrap { -webkit-overflow-scrolling: touch; }

  /* --- Tablette --------------------------------------------------------- */
  @media (max-width: 980px) {
    .shell { width: calc(100% - 28px); }
    .grid2 { grid-template-columns: 1fr; }
    .product-footer { grid-template-columns: 1fr 1fr; }
    .sportnav { grid-template-columns: 1fr; row-gap: 10px; }
    .nav-primary { justify-content: flex-start; overflow-x: auto; }
    .nav-access { justify-content: flex-start; }
  }

  /* --- Telephone ------------------------------------------------------- */
  @media (max-width: 640px) {
    .shell { width: calc(100% - 24px); padding: 10px 0 90px; }
    .sportnav { position: static; top: 0; padding: 12px; border-radius: 14px; }
    .brand small { display: none; }
    .nav-tabs { flex-wrap: nowrap; overflow-x: auto; gap: 4px; padding-bottom: 2px;
      -webkit-overflow-scrolling: touch; scrollbar-width: none; }
    .nav-tabs::-webkit-scrollbar { display: none; }
    .nav-tab { white-space: nowrap; padding: 8px 12px; }
    .sport-switch { flex-wrap: wrap; }

    .masthead, .matchline { grid-template-columns: 1fr; padding: 24px 20px; }
    .masthead h1, .matchline h1 { font-size: clamp(23px, 7vw, 30px); }
    .masthead .sub, .matchline .meta { font-size: 13.5px; }
    .masthead.hero-auth { min-height: 240px; }
    .hero-visual { display: none; }
    .matchline .verdict-no { font-size: clamp(34px, 12vw, 52px); }

    .verdict-strip { border-radius: 14px; }
    .verdict-strip .cell { flex: 1 1 46%; padding: 14px 16px 12px; }
    .verdict-strip .cell .v { font-size: 21px; }

    .metrics { grid-template-columns: 1fr 1fr; gap: 10px; }
    .metric { padding: 14px 14px 12px; }
    .metric .value { font-size: 26px; }

    .section { padding: 18px 16px 20px; margin-top: 14px; border-radius: 14px; }
    .section-head { grid-template-columns: auto 1fr; row-gap: 4px; }
    .section-head h2 { font-size: 17px; }
    .section-head .note { grid-column: 1 / -1; text-align: left; justify-self: start; max-width: none; }

    td, th { padding: 10px 8px; font-size: 13px; }
    .filterbar { grid-template-columns: 1fr 1fr; padding: 15px; gap: 11px; }
    .filterbar .filter-actions { grid-column: 1 / -1; flex-wrap: wrap; }
    .actions form, .periodbar { width: 100%; }
    .actions select, .actions button, .periodbar .period-chip { width: 100%; justify-content: center; }
    .inline-bet-form { grid-template-columns: 1fr 68px 58px auto; }
    .product-footer { grid-template-columns: 1fr; padding: 22px 18px; }
    .prise-modal { padding: 22px 20px; }
  }
"""


def run_prediction_pipeline(league_name: str | None = None) -> dict[str, Any]:
    """Pipeline cible : la competition demandee seulement (charge legere),
    ou balayage global toutes competitions si None/TOUTES."""
    connection = connect_db(DatabaseSettings.from_env())
    try:
        repository = PostgresPredictionRepository(
            connection, scope_name="WORLD_CUP_V1_SCOPE", league_name=league_name,
        )
        engine = _build_match_engine()
        processed = PredictionPipeline(repository, engine).run()
        repository.finalize_run()
        return {
            "processed_fixtures": processed,
            "competition": league_name or "toutes",
            "xgboost_active": engine._xgboost is not None and engine._xgboost.available,
        }
    finally:
        connection.close()


def run_project_script(script_path: Path, args: list[str] | None = None) -> dict[str, Any]:
    result = subprocess.run(
        [sys.executable, str(script_path), *(args or [])],
        cwd=str(ROOT_DIR),
        capture_output=True,
        text=True,
        check=True,
    )
    stdout = result.stdout.strip()
    if not stdout:
        return {"stdout": ""}
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        return {"stdout": stdout}


def _golf_script_args(selected_tournament: str = "all", filters: dict[str, str] | None = None) -> list[str]:
    filters = filters or {}
    args: list[str] = []
    if selected_tournament != "all" and selected_tournament.isdigit():
        args.extend(["--golf-tournament-id", selected_tournament])
    if filters.get("tour", "all") != "all":
        args.extend(["--tour", str(filters["tour"])])
    if filters.get("date_from"):
        args.extend(["--date-from", str(filters["date_from"])])
    if filters.get("date_to"):
        args.extend(["--date-to", str(filters["date_to"])])
    return args


def _datagolf_ingest_args(selected_tournament: str = "all", filters: dict[str, str] | None = None) -> list[str]:
    # DataGolf field/odds endpoints sont par tour; le tournament_id sert surtout
    # au runner prediction. Pour l'ingestion, on scinde donc par tour/date.
    filters = filters or {}
    args: list[str] = []
    if filters.get("tour", "all") != "all":
        args.extend(["--tour", str(filters["tour"])])
    if filters.get("date_from"):
        args.extend(["--date-from", str(filters["date_from"])])
    if filters.get("date_to"):
        args.extend(["--date-to", str(filters["date_to"])])
    return args


# ---------------------------------------------------------------------------
# Actions lourdes en ARRIERE-PLAN (subprocess detache).
#
# Le pipeline de prediction (Poisson / Elo / XGBoost) est CPU-heavy ; un
# thread Python reste bloque par le GIL et gele quand meme le serveur HTTP.
# On lance donc un SUBPROCESS DETACHE (run_action.py) qui a son propre GIL,
# communique via un fichier JSON dans logs/action_jobs/, et survit meme si
# le dev-watcher redemarre le serveur.
# ---------------------------------------------------------------------------
_ACTION_JOBS_DIR = ROOT_DIR / "logs" / "action_jobs"
_ACTION_RUNNER = Path(__file__).with_name("run_action.py")
_JOB_STALE_SECONDS = 1800


def start_action_in_background(action: str, **kwargs: Any) -> ActionReport:
    if not background_actions_enabled():
        return ActionReport(
            title="Action desactivee sur Vercel",
            status="error",
            payload={"info": background_actions_disabled_message()},
        )
    _ACTION_JOBS_DIR.mkdir(parents=True, exist_ok=True)

    for running_file in _ACTION_JOBS_DIR.glob("*.running"):
        try:
            age = _time.time() - running_file.stat().st_mtime
        except OSError:
            age = 0
        if age < _JOB_STALE_SECONDS:
            running_action = running_file.stem.rsplit("_", 1)[0]
            return ActionReport(
                title=f"Une action tourne deja : {running_action}",
                status="error",
                payload={
                    "info": "Une seule action lourde a la fois (quotas API et base). "
                            "Attends la fin puis relance.",
                },
            )
        running_file.unlink(missing_ok=True)

    job_id = f"{action}_{int(_time.time())}"
    running_file = _ACTION_JOBS_DIR / f"{job_id}.running"
    output_file = _ACTION_JOBS_DIR / f"{job_id}.json"
    running_file.write_text(
        datetime.now(timezone.utc).strftime("%H:%M:%S UTC"), encoding="utf-8",
    )

    cmd: list[str] = [sys.executable, str(_ACTION_RUNNER), action, str(output_file)]
    league = kwargs.get("league_name")
    if league:
        cmd.extend(["--league", league])
    golf_tournament = kwargs.get("golf_tournament", "all")
    if golf_tournament and golf_tournament != "all":
        cmd.extend(["--golf-tournament", golf_tournament])
    golf_filters = kwargs.get("golf_filters")
    if golf_filters:
        cmd.extend(["--golf-filters", json.dumps(golf_filters)])

    creation_flags = 0
    if os.name == "nt":
        creation_flags = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        )
    subprocess.Popen(
        cmd,
        cwd=str(ROOT_DIR),
        creationflags=creation_flags,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )

    return ActionReport(
        title=f"Lance en arriere-plan : {action}",
        status="success",
        payload={
            "info": "Le site reste utilisable pendant le traitement. "
                    "Recharge la page dans quelques minutes : le resultat s'affichera ici.",
        },
    )


def consume_finished_action_report() -> "ActionReport | None":
    """Rapport de la derniere action terminee, affiche UNE fois."""
    if not _ACTION_JOBS_DIR.exists():
        return None
    for result_file in sorted(
        _ACTION_JOBS_DIR.glob("*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    ):
        try:
            data = json.loads(result_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        result_file.unlink(missing_ok=True)
        if data.get("status") == "done":
            return ActionReport(
                title=data.get("title", "Action terminee"),
                status=data.get("report_status", "success"),
                payload=data.get("payload", {}),
            )
        if data.get("status") == "failed":
            return ActionReport(
                title="Echec de l'action en arriere-plan",
                status="error",
                payload={"erreur": str(data.get("error", ""))[:4000]},
            )
    return None


def execute_action(
    action: str,
    league_name: str | None = None,
    golf_tournament: str = "all",
    golf_filters: dict[str, str] | None = None,
) -> ActionReport:
    if action == "sync_reference":
        from spe_ingestion.main import ingest_thesportsdb_reference_data

        summary = ingest_thesportsdb_reference_data()
        return ActionReport(
            title="Synchronisation TheSportsDB terminee",
            status="success",
            payload=asdict(summary),
        )
    if action == "sync_odds":
        from spe_ingestion.main import ingest_theoddsapi_odds_data

        summary = ingest_theoddsapi_odds_data()
        return ActionReport(
            title="Synchronisation The Odds API terminee",
            status="success",
            payload=asdict(summary),
        )
    if action == "run_predictions":
        return ActionReport(
            title="Pipeline de prediction termine",
            status="success",
            payload=run_prediction_pipeline(league_name),
        )
    if action == "full_refresh":
        from spe_ingestion.main import ingest_theoddsapi_odds_data, ingest_thesportsdb_reference_data

        reference_summary = asdict(ingest_thesportsdb_reference_data())
        odds_summary = asdict(ingest_theoddsapi_odds_data())
        prediction_summary = run_prediction_pipeline(league_name)
        return ActionReport(
            title="Cycle V1 complet termine",
            status="success",
            payload={
                "reference_sync": reference_summary,
                "odds_sync": odds_summary,
                "prediction_run": prediction_summary,
            },
        )
    if action == "sync_golf_odds":
        payload = run_project_script(
            ROOT_DIR / "services" / "ingestion" / "run_ingest_datagolf.py",
            _datagolf_ingest_args(golf_tournament, golf_filters),
        )
        return ActionReport(
            title="Synchronisation DataGolf terminee",
            status="success",
            payload=payload,
        )
    if action == "sync_golf_catalog":
        payload = run_project_script(
            ROOT_DIR / "services" / "ingestion" / "run_ingest_datagolf.py",
            [*_datagolf_ingest_args(golf_tournament, golf_filters), "--catalog-only"],
        )
        return ActionReport(
            title="Catalogue DataGolf synchronise",
            status="success",
            payload=payload,
        )
    if action == "run_golf_predictions":
        payload = run_project_script(
            ROOT_DIR / "services" / "prediction" / "run_golf_predictions.py",
            _golf_script_args(golf_tournament, golf_filters),
        )
        return ActionReport(
            title="Predictions golf terminees",
            status="success",
            payload=payload,
        )
    if action == "golf_full_refresh":
        odds_payload = run_project_script(
            ROOT_DIR / "services" / "ingestion" / "run_ingest_datagolf.py",
            _datagolf_ingest_args(golf_tournament, golf_filters),
        )
        prediction_payload = run_project_script(
            ROOT_DIR / "services" / "prediction" / "run_golf_predictions.py",
            _golf_script_args(golf_tournament, golf_filters),
        )
        return ActionReport(
            title="Cycle Golf complet termine",
            status="success",
            payload={"datagolf_sync": odds_payload, "prediction_run": prediction_payload},
        )
    raise ValueError(f"Action inconnue: {action}")


def validate_back_payload(sport: str) -> dict[str, Any]:
    sport = (sport or "all").strip().lower()
    if sport not in ("all", "football", "golf"):
        sport = "all"
    payload = run_project_script(
        ROOT_DIR / "services" / "prediction" / "run_validate_back.py",
        ["--sport", sport],
    )
    try:
        connection = connect_db(DatabaseSettings.from_env())
        try:
            payload["bankroll_reconciliation"] = reconcile_bankroll_settlements(connection)
        finally:
            connection.close()
    except Exception as exc:
        payload["bankroll_reconciliation_error"] = str(exc)
    football_refresh = payload.get("football_refresh") or {}
    golf_refresh = payload.get("golf_refresh") or {}
    settlement = payload.get("settlement") or {}
    football_settlement = settlement.get("football") or {}
    golf_settlement = settlement.get("golf") or {}
    settled_count = (
        int(football_settlement.get("newly_settled") or 0)
        + int((football_settlement.get("predictions_settled") or {}).get("newly_settled") or 0)
        + int((football_settlement.get("parlays_settled") or {}).get("newly_settled") or 0)
        + int((football_settlement.get("scorer_settled") or {}).get("newly_settled") or 0)
        + int(golf_settlement.get("outright_settled") or 0)
        + int(golf_settlement.get("matchup_settled") or 0)
    )
    payload["_summary_qs"] = (
        f"validated=1&vf={int(football_refresh.get('scores_written') or 0)}"
        f"&vg={int(golf_refresh.get('results_written') or 0)}"
        f"&vs={settled_count}"
    )
    return payload


def validation_notice_from_params(params: dict[str, list[str]]) -> str:
    action_error = user_action_error(params)
    if action_error:
        return (
            "<div class='report error'><strong>Action refusee.</strong> "
            f"{escape(action_error)}</div>"
        )
    if (params.get("validation_error", [""])[0] or "") == "1":
        return (
            "<div class='report error'><strong>Validation echouee.</strong> "
            "Le cycle leger n'a pas ete lance; verifie les cles API ou les logs.</div>"
        )
    if (params.get("validated", [""])[0] or "") == "1":
        return (
            "<div class='report success'><strong>Validation terminee.</strong> "
            f"Scores foot: {escape(params.get('vf', ['0'])[0] or '0')} · "
            f"Resultats golf: {escape(params.get('vg', ['0'])[0] or '0')} · "
            f"Paris regles: {escape(params.get('vs', ['0'])[0] or '0')}.</div>"
        )
    return ""


def strategy_validation_form(sport: str, return_to: str) -> str:
    sport = GOLF_SPORT_PARAM if sport == GOLF_SPORT_PARAM else "football"
    return (
        "<form method='post' action='/bankroll' class='validation-form'>"
        "<input type='hidden' name='action' value='validate_back'>"
        f"<input type='hidden' name='sport' value='{escape(sport)}'>"
        f"<input type='hidden' name='return_to' value='{escape(return_to)}'>"
        "<button type='submit' class='button-primary'>Valider mes paris</button>"
        "<span class='muted'>Recupere uniquement les resultats lies a tes positions ouvertes, puis met a jour le Back et la bankroll.</span>"
        "</form>"
    )


def fetch_one(cursor, query: str, params: tuple[Any, ...] = ()) -> Any:
    cursor.execute(query, params)
    row = cursor.fetchone()
    return row[0] if row else None


def fetch_dicts(cursor, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    cursor.execute(query, params)
    columns = [column.name for column in cursor.description]
    return [dict(zip(columns, row, strict=False)) for row in cursor.fetchall()]


# Valeur speciale du selecteur : parcourir TOUTES les competitions pour
# trouver les meilleurs deals de la semaine (pas de filtre ligue).
ALL_LEAGUES = "__ALL__"
ALL_LEAGUES_LABEL = "Toutes competitions (deals de la semaine)"
# Faits divers : les marches long terme (vainqueurs, buteurs, indice BdO).
FAITS_DIVERS = "__FAITS_DIVERS__"
FAITS_DIVERS_LABEL = "Faits divers (previsions long terme)"


def load_outrights_data() -> dict[str, Any]:
    """Marches long terme : top candidats par marche + deals actifs."""
    connection = connect_db(DatabaseSettings.from_env())
    try:
        with connection.cursor() as cursor:
            rows = fetch_dicts(
                cursor,
                """
                SELECT outright_market_id, market_code, market_label, season_name,
                       deadline_utc, league_name, subject_type, subject_label,
                       probability, method_code, generated_at, best_odd, bookmaker_name
                FROM reporting.v_outright_board
                WHERE probability IS NOT NULL
                ORDER BY market_code, market_label, probability DESC
                """,
            )
            deals = fetch_dicts(
                cursor,
                """
                SELECT m.market_label, s.subject_label,
                       ROUND(od.model_probability * 100, 1) AS model_pct,
                       ROUND(od.implied_probability * 100, 1) AS implied_pct,
                       ROUND(od.edge_probability * 100, 1) AS edge_pct,
                       od.market_odd, b.bookmaker_name, od.detected_at
                FROM model.outright_deals od
                JOIN core.outright_markets m ON m.outright_market_id = od.outright_market_id
                JOIN core.outright_selections s
                  ON s.outright_selection_id = od.outright_selection_id
                LEFT JOIN core.bookmakers b ON b.bookmaker_id = od.bookmaker_id
                WHERE od.status_code = 'ACTIVE' AND od.result_code IS NULL
                ORDER BY od.edge_probability DESC
                """,
            )
    finally:
        connection.close()

    markets: dict[int, dict[str, Any]] = {}
    for row in rows:
        market = markets.setdefault(
            int(row["outright_market_id"]),
            {
                "market_code": row["market_code"],
                "market_label": row["market_label"],
                "deadline_utc": row["deadline_utc"],
                "generated_at": row["generated_at"],
                "candidates": [],
            },
        )
        if len(market["candidates"]) < 8:
            market["candidates"].append(row)
    market_order = ("TOURNAMENT_WINNER", "LEAGUE_WINNER", "TOP_SCORER", "BALLON_DOR_INDEX")
    ordered = sorted(
        markets.values(),
        key=lambda m: (
            market_order.index(m["market_code"]) if m["market_code"] in market_order else 9,
            str(m["market_label"]),
        ),
    )
    return {"markets": ordered, "deals": deals}


def render_outrights_page(data: dict[str, Any]) -> str:
    sections: list[str] = []

    deals = data["deals"]
    if deals:
        deal_rows = "".join(
            "<tr>"
            f"<td>{escape(str(d['market_label']))}</td>"
            f"<td><strong>{escape(str(d['subject_label']))}</strong></td>"
            f"<td class='num'>{d['model_pct']}</td>"
            f"<td class='num'>{d['implied_pct']}</td>"
            + _edge_cell(_to_float(d["edge_pct"])) +
            f"<td class='num'><strong>{float(d['market_odd']):.2f}</strong></td>"
            f"<td>{escape(str(d['bookmaker_name'] or '-'))}</td>"
            "</tr>"
            for d in deals
        )
        sections.append(render_section(
            "01", "Deals long terme actifs",
            "<div class='table-wrap'><table>"
            "<thead><tr><th>Marche</th><th>Candidat</th><th class='num'>Modele %</th>"
            "<th class='num'>Marche %</th><th class='num'>Edge</th>"
            "<th class='num'>Cote</th><th>Bookmaker</th></tr></thead>"
            f"<tbody>{deal_rows}</tbody></table></div>",
            note="Meme methode que les matchs : edge = P(simulation) - P(implicite normalisee du marche outright), gardes renforcees (horizon long).",
        ))

    number = 2 if deals else 1
    for market in data["markets"]:
        candidate_rows = "".join(
            "<tr>"
            f"<td class='num'>{i}</td>"
            f"<td><strong>{escape(str(c['subject_label']))}</strong></td>"
            f"<td class='num'><strong>{float(c['probability']) * 100:.1f}</strong></td>"
            + (
                f"<td class='num'>{float(c['best_odd']):.2f}</td>"
                if c.get("best_odd") else "<td class='num muted'>-</td>"
            )
            + f"<td class='muted'>{escape(str(c['bookmaker_name'] or '-'))}</td>"
            "</tr>"
            for i, c in enumerate(market["candidates"], start=1)
        )
        code = str(market["market_code"])
        unit = "indice (max 1)" if code == "BALLON_DOR_INDEX" else "probabilite %"
        note = (
            "Indice de performance 12 mois (buts + passes ponderes par competition). "
            "PAS une probabilite : le Ballon d'Or est un vote, aucun marche cote - "
            "affiche a titre indicatif, jamais vendu comme deal."
            if code == "BALLON_DOR_INDEX"
            else "Probabilites par simulation Monte Carlo (matchs restants evalues par le moteur calibre)."
        )
        sections.append(render_section(
            f"{number:02d}", str(market["market_label"]),
            "<div class='table-wrap'><table>"
            f"<thead><tr><th class='num'>#</th><th>Candidat</th><th class='num'>{unit}</th>"
            "<th class='num'>Meilleure cote</th><th>Book</th></tr></thead>"
            f"<tbody>{candidate_rows}</tbody></table></div>",
            note=note,
        ))
        number += 1

    if not sections:
        sections.append(
            "<p class='empty'>Aucun marche long terme en base - lancer "
            "py services/prediction/run_outright_predictions.py</p>"
        )

    hero_title = "Back golf - performance verifiee" if current_sport == "golf" else "Back football - performance verifiee"
    hero_meta = (
        "Deals golf suivis par tournoi, matchups et resultats DataGolf."
        if current_sport == "golf"
        else "Pronostics, deals football et combines verifies a la fin de chaque match."
    )

    return f"""<!DOCTYPE html>
<html lang="fr">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Faits divers - previsions long terme</title>
  <style>{BASE_CSS}</style>
</head>
<body>
  <main class="shell">
    {render_sport_nav("football", "predictions")}
    <header class="matchline">
      <div>
        <h1>Faits divers</h1>
        <p class="meta">Previsions long terme : vainqueurs, buteurs, indice Ballon d'Or -
        integrees automatiquement aux strategies de 1 mois et plus.</p>
      </div>
    </header>
    {"".join(sections)}
    {render_product_footer("football")}
  </main>
{dev_reload_script()}
</body>
</html>"""


# Ordre d'affichage des marches golf (le vainqueur d'abord).
GOLF_MARKET_ORDER = ["TOURNAMENT_WINNER", "TOP_5", "TOP_10", "TOP_20", "MAKE_CUT"]
GOLF_MARKET_ROW_LIMITS = {
    "TOURNAMENT_WINNER": 30,
    "TOP_5": 12,
    "TOP_10": 18,
    "TOP_20": 25,
    "MAKE_CUT": 20,
}
GOLF_MARKET_LABELS = {
    "TOURNAMENT_WINNER": "Vainqueur du tournoi",
    "TOP_3": "Top 3",
    "TOP_5": "Top 5",
    "TOP_10": "Top 10",
    "TOP_20": "Top 20",
    "MAKE_CUT": "Passe le cut",
}
GOLF_MATCHUP_LABELS = {
    "TOURNAMENT_MATCHUP": "Duel sur tout le tournoi",
    "ROUND_MATCHUP": "Duel du tour",
    "THREE_BALL": "Meilleur du groupe de 3",
}
GOLF_BOOK_PATHS = {
    "TOURNAMENT_WINNER": "Tournoi > Marge du vainqueur / Vainqueur du tournoi",
    "TOP_3": "Principaux > Meilleur des 3",
    "TOP_5": "Principaux > Top classement",
    "TOP_10": "Principaux > Top classement",
    "TOP_20": "Principaux > Top classement",
    "MAKE_CUT": "Principaux > Se qualifie",
    "TOURNAMENT_MATCHUP": "Tournoi > 72 Trous - Match - 2 options",
    "ROUND_MATCHUP": "Tour > 18 trous - Face-a-face",
    "THREE_BALL": "Tour > Meilleur des 3",
}
GOLF_UNSUPPORTED_BOOK_PATHS = [
    ("Meilleur des 3 - Handicap", "Pas encore modele: demande un handicap/ligne par joueur."),
    ("Mene apres le 2e/3e tour et gagne", "Pas encore modele: marche conditionnel multi-etapes."),
    ("Marge de victoire / Trou en un / Albatros", "Pas encore modele: marche evenement rare, variance trop haute."),
    ("Joueur - Totaux", "Pas encore modele: besoin de lignes statistiques joueur detaillees."),
]
GOLF_TOUR_LABELS = {
    "pga": "PGA Tour",
    "euro": "DP World Tour",
    "kft": "Korn Ferry Tour",
    "liv": "LIV Golf",
}
GOLF_BOOKMAKER_COVERAGE = [
    {
        "book_name": "Open d'Ecosse",
        "aliases": ("Genesis Scottish Open", "Scottish Open", "Open d'Ecosse"),
        "reason": "Nom bookmaker different: c'est le Genesis Scottish Open cote DataGolf.",
        "action": "Normaliser le nom et afficher la correspondance.",
    },
    {
        "book_name": "ISCO Championship",
        "aliases": ("ISCO Championship",),
        "reason": "Tournoi PGA possible, mais seulement si DataGolf expose le field et les cotes.",
        "action": "Surveiller la prochaine sync DataGolf.",
    },
    {
        "book_name": "The Amundi Evian Championship",
        "aliases": ("Amundi Evian Championship", "The Amundi Evian Championship"),
        "reason": "LPGA: pas encore dans les tours modelises de la V2 actuelle.",
        "action": "Ajouter LPGA quand la source donne joueurs, odds et resultats.",
    },
    {
        "book_name": "Kaulig Companies Championship",
        "aliases": ("Kaulig Companies Championship",),
        "reason": "Champions/Senior tour: pas encore modelise en V2.",
        "action": "Ajouter seulement avec historique fiable.",
    },
    {
        "book_name": "Irish Legends",
        "aliases": ("Irish Legends",),
        "reason": "Legends tour: trop peu de donnees comparables pour le modele actuel.",
        "action": "A garder hors modele tant que le feed n'est pas propre.",
    },
    {
        "book_name": "2026 Open Championship",
        "aliases": ("The Open Championship", "Open Championship"),
        "reason": "Future majeure: le field complet n'est pas stable longtemps avant le tournoi.",
        "action": "Activer quand le field officiel et les cotes joueurs sont presents.",
    },
    {
        "book_name": "Presidents Cup 2026",
        "aliases": ("Presidents Cup",),
        "reason": "Competition par equipes: marche different d'un tournoi stroke-play classique.",
        "action": "Modele separe equipe/match-play requis.",
    },
    {
        "book_name": "Solheim Cup 2026",
        "aliases": ("Solheim Cup",),
        "reason": "Competition equipe feminine: format et donnees differents.",
        "action": "Modele equipe/match-play + LPGA requis.",
    },
    {
        "book_name": "US Masters 2027",
        "aliases": ("Masters Tournament", "US Masters"),
        "reason": "Future 2027: trop loin, field non definitif.",
        "action": "Ne pas modeliser avant donnees joueurs exploitables.",
    },
    {
        "book_name": "2027 PGA Championship",
        "aliases": ("PGA Championship",),
        "reason": "Future 2027: field non definitif.",
        "action": "Attendre field + odds actifs.",
    },
    {
        "book_name": "US Open 2027",
        "aliases": ("U.S. Open", "US Open"),
        "reason": "Future 2027: field non definitif.",
        "action": "Attendre field + odds actifs.",
    },
    {
        "book_name": "Ryder Cup 2027",
        "aliases": ("Ryder Cup",),
        "reason": "Competition par equipes/match-play, pas un outright joueur standard.",
        "action": "Modele dedie equipe requis.",
    },
    {
        "book_name": "Golf virtuel / Milton Manor",
        "aliases": ("Golf virtuel", "Virtual Golf", "Milton Manor"),
        "reason": "Produit virtuel bookmaker: pas un evenement sportif reel avec historique joueurs.",
        "action": "Exclure du modele prediction sportive.",
    },
]


def _clean_golf_name(name: Any) -> str:
    """Affichage client: DataGolf peut parfois fournir 'Nom, Prenom'."""
    text = str(name or "").strip()
    if "," in text:
        last, first = text.split(",", 1)
        text = f"{first.strip()} {last.strip()}".strip()
    return " ".join(text.split())


def _golf_market_label(market_code: Any) -> str:
    code = str(market_code or "")
    return GOLF_MARKET_LABELS.get(code, code.replace("_", " ").title())


def _golf_market_section_title(market_code: Any, shown: int) -> str:
    label = _golf_market_label(market_code)
    code = str(market_code or "")
    if code == "TOURNAMENT_WINNER":
        return f"Candidats vainqueur - {shown} meilleurs profils"
    return f"Candidats pari {label} - {shown} meilleurs profils"


def _golf_market_explanation(market_code: Any, shown: int) -> str:
    label = _golf_market_label(market_code)
    code = str(market_code or "")
    if code == "TOURNAMENT_WINNER":
        return (
            f"Lecture: on affiche les {shown} meilleurs candidats pour gagner le tournoi. "
            "Un deal n'apparait que si la cote book paie mieux que la cote juste du modele."
        )
    if code in {"TOP_3", "TOP_5", "TOP_10", "TOP_20"}:
        return (
            f"Lecture: ce n'est pas une liste de seulement {label.lower()} joueurs; "
            f"ce sont les {shown} meilleurs candidats a jouer sur le marche {label}. "
            f"Le pari gagne si le joueur termine {label.lower()}."
        )
    if code == "MAKE_CUT":
        return (
            f"Lecture: les {shown} profils affiches sont les meilleurs candidats pour passer le cut. "
            "Le marche est pris seulement quand le feed donne une cote exploitable."
        )
    return "Lecture: classement modele par probabilite, puis comparaison avec les cotes disponibles."


def _golf_matchup_label(market_code: Any) -> str:
    code = str(market_code or "")
    return GOLF_MATCHUP_LABELS.get(code, code.replace("_", " ").title())


def _golf_book_path(market_code: Any) -> str:
    code = str(market_code or "")
    return GOLF_BOOK_PATHS.get(code, "Verifier le libelle exact dans le bookmaker")


def _golf_place_instruction(market_code: Any) -> str:
    return f"Ou poser: {_golf_book_path(market_code)}"


def _golf_tour_label(tour_code: Any) -> str:
    code = str(tour_code or "").strip().lower()
    return GOLF_TOUR_LABELS.get(code, code.upper() if code else "Golf")


def _golf_catalog_status_label(status: Any) -> str:
    code = str(status or "").strip().upper()
    return {
        "DISCOVERED": "catalogue",
        "FIELD_SYNCED": "donnees chargees",
        "ODDS_SYNCED": "cotes chargees",
        "COMPLETED": "termine",
        "UNSUPPORTED": "non supporte",
    }.get(code, code.lower() if code else "catalogue")


def _golf_competition_line(row: dict[str, Any]) -> str:
    parts = [str(row.get("tournament_name") or "").strip()]
    tour = _golf_tour_label(row.get("tour_code"))
    if tour and tour != "Golf":
        parts.append(tour)
    course = str(row.get("course_name") or "").strip()
    if course:
        parts.append(course)
    return " - ".join(part for part in parts if part)


def _normalize_golf_lookup(value: Any) -> str:
    text = str(value or "").lower()
    return "".join(ch for ch in text if ch.isalnum())


def _golf_coverage_rows(tournaments: list[dict[str, Any]]) -> list[dict[str, str]]:
    tournament_names = [
        (str(t.get("tournament_name") or ""), _golf_competition_line(t))
        for t in tournaments
    ]
    rows: list[dict[str, str]] = []
    matched_tournament_keys: set[str] = set()
    for item in GOLF_BOOKMAKER_COVERAGE:
        matched = ""
        aliases = item["aliases"]
        alias_keys = [_normalize_golf_lookup(alias) for alias in aliases]
        for raw_name, display_name in tournament_names:
            raw_key = _normalize_golf_lookup(raw_name)
            if any(alias and (alias in raw_key or raw_key in alias) for alias in alias_keys):
                matched = display_name or raw_name
                matched_tournament_keys.add(raw_key)
                break
        rows.append({
            "book_name": str(item["book_name"]),
            "status": "Couvert" if matched else "Non couvert V2",
            "matched": matched,
            "reason": str(item["reason"]),
            "action": str(item["action"]),
        })
    for raw_name, display_name in tournament_names:
        raw_key = _normalize_golf_lookup(raw_name)
        if raw_key and raw_key not in matched_tournament_keys:
            rows.append({
                "book_name": raw_name,
                "status": "DataGolf seulement",
                "matched": display_name,
                "reason": "Present dans DataGolf, mais absent de la liste bookmaker observee.",
                "action": "Ne pas poser de pari si ton bookmaker ne propose pas ce championnat.",
            })
    return rows


def _golf_outright_bet_sentence(player_name: Any, market_code: Any) -> str:
    name = _clean_golf_name(player_name)
    code = str(market_code or "")
    if code == "TOURNAMENT_WINNER":
        return f"Parier que {name} gagne le tournoi"
    if code in {"TOP_3", "TOP_5", "TOP_10", "TOP_20"}:
        return f"Parier que {name} finit {GOLF_MARKET_LABELS[code]}"
    if code == "MAKE_CUT":
        return f"Parier que {name} passe le cut"
    return f"Parier sur {name} - {_golf_market_label(code)}"


def _golf_matchup_bet_sentence(pick_name: Any, opponent_name: Any, market_code: Any) -> str:
    pick = _clean_golf_name(pick_name)
    opponent = _clean_golf_name(opponent_name)
    code = str(market_code or "")
    if code == "THREE_BALL":
        return f"Parier que {pick} finit devant son groupe de 3"
    if code == "ROUND_MATCHUP":
        return f"Parier que {pick} bat {opponent} sur ce tour"
    return f"Parier que {pick} bat {opponent} sur le tournoi"


def _golf_back_bet_sentence(row: dict[str, Any]) -> str:
    if str(row.get("deal_type")) == "MATCHUP" and " vs " in str(row.get("subject") or ""):
        pick, opponent = str(row["subject"]).split(" vs ", 1)
        return _golf_matchup_bet_sentence(pick, opponent, row.get("market_code"))
    return _golf_outright_bet_sentence(row.get("subject"), row.get("market_code"))


def _valid_date_filter(value: Any) -> str:
    text = str(value or "").strip()[:10]
    if not text:
        return ""
    try:
        datetime.fromisoformat(text)
    except ValueError:
        return ""
    return text


GOLF_PERIODS: dict[str, tuple[str, int | None]] = {
    "today": ("Jour", 1),
    "7d": ("Semaine", 7),
    "30d": ("Mois", 30),
    "all": ("Tout", None),
}
DEFAULT_GOLF_PERIOD = "7d"


def _golf_period_dates(period: str) -> tuple[str, str]:
    if period not in GOLF_PERIODS:
        period = DEFAULT_GOLF_PERIOD
    days = GOLF_PERIODS[period][1]
    if days is None:
        return "", ""
    today = datetime.utcnow().date()
    return (today - timedelta(days=4)).isoformat(), (today + timedelta(days=days)).isoformat()


def golf_filters_from_params(params: dict[str, list[str]]) -> dict[str, str]:
    period = (params.get("golf_period", params.get("period", [DEFAULT_GOLF_PERIOD]))[0] or DEFAULT_GOLF_PERIOD).strip()
    if period not in GOLF_PERIODS:
        period = DEFAULT_GOLF_PERIOD
    date_from = _valid_date_filter(params.get("golf_date_from", [""])[0])
    date_to = _valid_date_filter(params.get("golf_date_to", [""])[0])
    if not date_from and not date_to:
        date_from, date_to = _golf_period_dates(period)
    return {
        "tour": (params.get("golf_tour", params.get("tour", ["all"]))[0] or "all").strip().lower(),
        "market": (params.get("golf_market", params.get("market", ["all"]))[0] or "all").strip(),
        "deal_status": (params.get("golf_deal_status", ["active"])[0] or "active").strip().lower(),
        "date_from": date_from,
        "date_to": date_to,
        "period": period,
    }


def _golf_query(filters: dict[str, str], **overrides: str) -> str:
    params = {
        "sport": GOLF_SPORT_PARAM,
        "tournament": overrides.pop("tournament", overrides.pop("selected_tournament", "all")),
        "golf_tour": filters.get("tour", "all"),
        "golf_market": filters.get("market", "all"),
        "golf_deal_status": filters.get("deal_status", "active"),
        "golf_period": filters.get("period", DEFAULT_GOLF_PERIOD),
        "golf_date_from": filters.get("date_from", ""),
        "golf_date_to": filters.get("date_to", ""),
    }
    params.update({k: v for k, v in overrides.items() if v is not None})
    return urlencode({k: v for k, v in params.items() if str(v) != ""})


def _golf_filter_clauses(alias: str, filters: dict[str, str], *, include_market: bool = True) -> tuple[list[str], list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    tour = filters.get("tour", "all")
    market = filters.get("market", "all")
    if tour != "all":
        clauses.append(f"{alias}.tour_code = %s")
        params.append(tour)
    if include_market and market != "all":
        clauses.append(f"{alias}.market_code = %s")
        params.append(market)
    date_expr = f"COALESCE({alias}.date_start, {alias}.commence_time::date)"
    if filters.get("date_from"):
        clauses.append(f"{date_expr} >= %s::date")
        params.append(filters["date_from"])
    if filters.get("date_to"):
        clauses.append(f"{date_expr} <= %s::date")
        params.append(filters["date_to"])
    return clauses, params


def load_golf_data(
    selected_tournament: str = "all",
    filters: dict[str, str] | None = None,
    user_id: int = 1,
) -> dict[str, Any]:
    filters = filters or {
        "tour": "all", "market": "all", "deal_status": "active",
        "date_from": "", "date_to": "",
    }
    connection = connect_db(DatabaseSettings.from_env())
    try:
        with connection.cursor() as cursor:
            tournament_clauses = ["tour_code IS NOT NULL"]
            tournament_params: list[Any] = []
            # Navigation clinique : sans filtre de dates explicite, on ne liste
            # que les tournois CONSULTABLES (predictions en base) ou IMMINENTS
            # (14 jours). Le catalogue complet (108+) reste accessible via les
            # filtres de dates.
            if filters.get("period") != "all" and not filters.get("date_from") and not filters.get("date_to"):
                tournament_clauses.append(
                    "(EXISTS (SELECT 1 FROM model.golf_predictions gp "
                    "         WHERE gp.golf_tournament_id = core.golf_tournaments.golf_tournament_id) "
                    " OR COALESCE(date_start, commence_time::date) "
                    "    BETWEEN current_date - 2 AND current_date + 14)"
                )
            if filters.get("tour", "all") != "all":
                tournament_clauses.append("tour_code = %s")
                tournament_params.append(filters["tour"])
            date_expr = "COALESCE(date_start, commence_time::date)"
            if filters.get("date_from"):
                tournament_clauses.append(f"{date_expr} >= %s::date")
                tournament_params.append(filters["date_from"])
            if filters.get("date_to"):
                tournament_clauses.append(f"{date_expr} <= %s::date")
                tournament_params.append(filters["date_to"])
            tournaments = fetch_dicts(
                cursor,
                f"""
                SELECT golf_tournament_id, tournament_name, sport_title, tour_code,
                       course_name, current_round, commence_time, date_start,
                       catalog_status, last_field_sync_at
                FROM core.golf_tournaments
                WHERE {' AND '.join(tournament_clauses)}
                ORDER BY COALESCE(date_start, commence_time::date) DESC NULLS LAST, tournament_name
                """,
                tuple(tournament_params),
            )
            tour_options = fetch_dicts(
                cursor,
                """
                SELECT DISTINCT tour_code
                FROM core.golf_tournaments
                WHERE tour_code IS NOT NULL
                ORDER BY tour_code
                """,
            )
            params: list[Any] = []
            # tour_code IS NOT NULL => uniquement les tournois DataGolf V2
            # (masque l'ancien The Open ingere via The Odds API en V1).
            where_parts = ["b.model_probability IS NOT NULL", "b.tour_code IS NOT NULL"]
            row_filters, row_filter_params = _golf_filter_clauses("b", filters)
            where_parts.extend(row_filters)
            params.extend(row_filter_params)
            deal_filter_parts, deal_filter_params = _golf_filter_clauses(
                "gt", filters, include_market=False
            )
            if filters.get("market", "all") != "all":
                deal_filter_parts.append("gd.market_code = %s")
                deal_filter_params.append(filters["market"])
            matchup_filter_parts, matchup_filter_params = _golf_filter_clauses(
                "gt", filters, include_market=False
            )
            if filters.get("market", "all") != "all":
                matchup_filter_parts.append("gmd.market_code = %s")
                matchup_filter_params.append(filters["market"])
            if selected_tournament != "all" and selected_tournament.isdigit():
                tournament_id = int(selected_tournament)
                where_parts.append("b.golf_tournament_id = %s")
                params.append(tournament_id)
                deal_filter_parts.append("gt.golf_tournament_id = %s")
                deal_filter_params.append(tournament_id)
                matchup_filter_parts.append("gt.golf_tournament_id = %s")
                matchup_filter_params.append(tournament_id)
            deal_status = filters.get("deal_status", "active")
            if deal_status == "settled":
                deal_status_sql = "gd.result_code IS NOT NULL"
                matchup_status_sql = "gmd.result_code IS NOT NULL"
            elif deal_status == "all":
                deal_status_sql = "gd.status_code <> 'CANCELLED'"
                matchup_status_sql = "gmd.status_code <> 'CANCELLED'"
            else:
                deal_status_sql = "gd.status_code = 'ACTIVE' AND gd.result_code IS NULL"
                matchup_status_sql = "gmd.status_code = 'ACTIVE' AND gmd.result_code IS NULL"
            where = "WHERE " + " AND ".join(where_parts)
            deal_where = "WHERE " + " AND ".join([deal_status_sql] + deal_filter_parts)
            matchup_where = "WHERE " + " AND ".join([matchup_status_sql] + matchup_filter_parts)
            # Jointure SG (strokes-gained DataGolf) : transparence du POURQUOI.
            rows = fetch_dicts(
                cursor,
                f"""
                SELECT b.golf_tournament_id, b.tournament_name, b.sport_title, b.tour_code,
                       b.course_name, b.commence_time, b.player_name, b.country_code, b.owgr_rank,
                       b.market_code, b.model_probability, b.model_fair_odd,
                       b.best_odd, b.bookmaker_name, b.edge_probability, b.deal_odd,
                       sr.sg_total, sr.sg_ott, sr.sg_app, sr.sg_arg, sr.sg_putt
                FROM reporting.v_golf_board b
                LEFT JOIN core.golf_skill_ratings sr ON sr.golf_player_id = b.golf_player_id
                {where}
                ORDER BY b.commence_time DESC NULLS LAST, b.market_code,
                         b.model_probability DESC NULLS LAST, b.player_name
                """,
                tuple(params),
            )
            deals = fetch_dicts(
                cursor,
                f"""
                SELECT gd.golf_deal_id, gt.tournament_name, gt.tour_code, gt.course_name,
                       gd.selection_name, gd.market_code,
                       ROUND(gd.model_probability * 100, 1) AS model_pct,
                       ROUND(gd.implied_probability * 100, 1) AS implied_pct,
                       ROUND(gd.edge_probability * 100, 1) AS edge_pct,
                       gd.market_odd, b.bookmaker_name, gd.detected_at,
                       COALESCE(pos.position_count, 0) AS position_count,
                       pos.total_stake, pos.position_ids
                FROM model.golf_deals gd
                JOIN core.golf_tournaments gt ON gt.golf_tournament_id = gd.golf_tournament_id
                LEFT JOIN core.bookmakers b ON b.bookmaker_id = gd.bookmaker_id
                LEFT JOIN LATERAL (
                    SELECT COUNT(*)::integer AS position_count,
                           ROUND(SUM(p.stake_amount)::numeric, 2) AS total_stake,
                           ARRAY_AGG(p.position_id ORDER BY p.taken_at DESC) AS position_ids
                    FROM model.user_bet_positions p
                    WHERE p.golf_deal_id = gd.golf_deal_id
                      AND p.user_id = %s
                      AND p.deleted_at IS NULL
                ) pos ON true
                {deal_where}
                ORDER BY gd.edge_probability DESC, gd.detected_at DESC
                LIMIT 80
                """,
                (user_id, *deal_filter_params),
            )
            matchup_deals = fetch_dicts(
                cursor,
                f"""
                SELECT gmd.golf_matchup_deal_id, gt.tournament_name, gt.tour_code,
                       gt.course_name, gmd.market_code,
                       pk.player_name AS pick_name, opp.player_name AS opp_name,
                       ROUND(gmd.model_probability * 100, 1) AS model_pct,
                       ROUND(gmd.edge_probability * 100, 1) AS edge_pct,
                       gmd.market_odd, b.bookmaker_name,
                       COALESCE(pos.position_count, 0) AS position_count,
                       pos.total_stake, pos.position_ids
                FROM model.golf_matchup_deals gmd
                JOIN core.golf_tournaments gt ON gt.golf_tournament_id = gmd.golf_tournament_id
                JOIN core.golf_players pk ON pk.golf_player_id = gmd.pick_golf_player_id
                JOIN core.golf_players opp ON opp.golf_player_id = gmd.opponent_golf_player_id
                LEFT JOIN core.bookmakers b ON b.bookmaker_id = gmd.bookmaker_id
                LEFT JOIN LATERAL (
                    SELECT COUNT(*)::integer AS position_count,
                           ROUND(SUM(p.stake_amount)::numeric, 2) AS total_stake,
                           ARRAY_AGG(p.position_id ORDER BY p.taken_at DESC) AS position_ids
                    FROM model.user_bet_positions p
                    WHERE p.golf_matchup_deal_id = gmd.golf_matchup_deal_id
                      AND p.user_id = %s
                      AND p.deleted_at IS NULL
                ) pos ON true
                {matchup_where}
                ORDER BY gmd.edge_probability DESC
                LIMIT 80
                """,
                (user_id, *matchup_filter_params),
            )
            position_filter_parts, position_filter_params = _golf_filter_clauses(
                "gt", filters, include_market=False
            )
            if filters.get("market", "all") != "all":
                position_filter_parts.append("p.market_code = %s")
                position_filter_params.append(filters["market"])
            if selected_tournament != "all" and selected_tournament.isdigit():
                position_filter_parts.append("gt.golf_tournament_id = %s")
                position_filter_params.append(int(selected_tournament))
            position_where = " AND ".join([
                "p.bet_kind = 'GOLF'",
                "p.user_id = %s",
                "p.deleted_at IS NULL",
            ] + position_filter_parts)
            unique_players = {
                str(row.get("player_name") or "")
                for row in rows
                if row.get("player_name")
            }
            # Positions PRISES sur les deals golf (indicateur permanent :
            # un pari engage ne disparait jamais des vues, meme s'il sort
            # du portefeuille recommande).
            golf_positions = fetch_dicts(
                cursor,
                f"""
                SELECT p.position_id, p.selection_label, p.market_code,
                       p.taken_odd, p.stake_amount, p.taken_at,
                       p.cashout_amount, p.cashed_out_at,
                       (p.golf_matchup_deal_id IS NOT NULL) AS is_matchup
                FROM model.user_bet_positions p
                LEFT JOIN model.golf_deals gd ON gd.golf_deal_id = p.golf_deal_id
                LEFT JOIN model.golf_matchup_deals gmd ON gmd.golf_matchup_deal_id = p.golf_matchup_deal_id
                LEFT JOIN core.golf_tournaments gt
                    ON gt.golf_tournament_id = COALESCE(gd.golf_tournament_id, gmd.golf_tournament_id)
                WHERE {position_where}
                ORDER BY p.taken_at DESC
                LIMIT 60
                """,
                (user_id, *position_filter_params),
            )
            golf_last_update = fetch_one(
                cursor,
                """
                SELECT MAX(updated_at) FROM (
                    SELECT MAX(last_field_sync_at) AS updated_at FROM core.golf_tournaments
                    UNION ALL
                    SELECT MAX(generated_at) FROM model.golf_predictions
                    UNION ALL
                    SELECT MAX(generated_at) FROM model.golf_matchup_predictions
                    UNION ALL
                    SELECT MAX(detected_at) FROM model.golf_deals
                    UNION ALL
                    SELECT MAX(detected_at) FROM model.golf_matchup_deals
                    UNION ALL
                    SELECT MAX(captured_at) FROM core.golf_odds
                    UNION ALL
                    SELECT MAX(captured_at) FROM core.golf_matchup_odds
                ) updates
                """,
            )
            # Deals golf REGLES (outrights + matchups) -> courbe d'equite,
            # meme section « Marche et performance » que le foot.
            settled_rows = fetch_dicts(
                cursor,
                """
                SELECT profit_units, settled_at FROM (
                    SELECT gd.profit_units, gd.settled_at
                    FROM model.golf_deals gd
                    WHERE gd.result_code IN ('WON', 'LOST') AND gd.profit_units IS NOT NULL
                    UNION ALL
                    SELECT gmd.profit_units, gmd.settled_at
                    FROM model.golf_matchup_deals gmd
                    WHERE gmd.result_code IN ('WON', 'LOST', 'PUSH') AND gmd.profit_units IS NOT NULL
                ) s ORDER BY settled_at
                """,
            )
    finally:
        connection.close()

    equity_series: list[tuple[float, float]] = [(0.0, 0.0)]
    running = 0.0
    for i, r in enumerate(settled_rows, start=1):
        running += float(r["profit_units"] or 0)
        equity_series.append((float(i), round(running, 4)))
    settled_won = sum(1 for r in settled_rows if float(r["profit_units"] or 0) > 0)
    settled_count = len(settled_rows)
    win_rate = (settled_won / settled_count * 100.0) if settled_count else 0.0
    roi_flat = (running / settled_count * 100.0) if settled_count else 0.0
    metrics = [
        MetricCard("Derniere mise a jour Quebec", format_metric_timestamp(golf_last_update), "timestamp"),
        MetricCard("Competitions filtrees", str(len(tournaments))),
        MetricCard("Joueurs filtres", str(len(unique_players))),
        MetricCard("Deals outright", str(len(deals)), "accent"),
        MetricCard("Deals matchups", str(len(matchup_deals)), "accent"),
        MetricCard("Positions prises", str(len(golf_positions)), "accent"),
        MetricCard("Deals regles", str(settled_count)),
        MetricCard("Taux de reussite %", f"{win_rate:.1f}" if settled_count else "-"),
        MetricCard(
            "ROI % (mise plate 1 unite)",
            f"{roi_flat:+.2f}" if settled_count else "-",
            "accent" if roi_flat > 0 else "negative" if roi_flat < 0 else "neutral",
        ),
        MetricCard(
            "Profit cumule (unites)",
            f"{running:+.2f}" if settled_count else "-",
            "accent" if running > 0 else "negative" if running < 0 else "neutral",
        ),
    ]

    markets: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        markets.setdefault(str(row["market_code"] or "UNKNOWN"), []).append(row)
    # Reordonne selon GOLF_MARKET_ORDER (vainqueur en premier).
    ordered = {code: markets[code] for code in GOLF_MARKET_ORDER if code in markets}
    for code, market_rows in markets.items():
        if code not in ordered:
            ordered[code] = market_rows
    return {
        "tournaments": tournaments,
        "selected_tournament": selected_tournament,
        "markets": ordered,
        "deals": deals,
        "matchup_deals": matchup_deals,
        "coverage_rows": _golf_coverage_rows(tournaments),
        "filters": filters,
        "tour_options": [str(r["tour_code"]) for r in tour_options],
        "market_options": list(GOLF_MARKET_LABELS) + list(GOLF_MATCHUP_LABELS),
        "metrics": metrics,
        "equity_series": equity_series,
        "settled_count": settled_count,
        "settled_won": settled_won,
        "settled_profit": round(running, 2),
        "golf_positions": golf_positions,
    }


def load_golf_detail(golf_tournament_id: int) -> dict[str, Any] | None:
    connection = connect_db(DatabaseSettings.from_env())
    try:
        with connection.cursor() as cursor:
            tournament = fetch_dicts(
                cursor,
                """
                SELECT golf_tournament_id, tournament_name, sport_title, tour_code,
                       course_name, current_round, commence_time, venue_name,
                       venue_city, venue_country
                FROM core.golf_tournaments
                WHERE golf_tournament_id = %s
                """,
                (golf_tournament_id,),
            )
            if not tournament:
                return None
            rows = fetch_dicts(
                cursor,
                """
                SELECT b.golf_tournament_id, b.tournament_name, b.tour_code,
                       b.course_name, b.player_name, b.country_code, b.owgr_rank,
                       b.market_code, b.model_probability, b.model_fair_odd,
                       b.best_odd, b.bookmaker_name, b.edge_probability,
                       sr.sg_total, sr.sg_ott, sr.sg_app, sr.sg_arg, sr.sg_putt
                FROM reporting.v_golf_board b
                LEFT JOIN core.golf_skill_ratings sr ON sr.golf_player_id = b.golf_player_id
                WHERE b.golf_tournament_id = %s AND b.model_probability IS NOT NULL
                ORDER BY b.market_code, b.model_probability DESC NULLS LAST, b.player_name
                """,
                (golf_tournament_id,),
            )
            deals = fetch_dicts(
                cursor,
                """
                SELECT gt.tournament_name, gt.tour_code, gt.course_name,
                       gd.selection_name, gd.market_code,
                       ROUND(gd.model_probability * 100, 1) AS model_pct,
                       ROUND(gd.implied_probability * 100, 1) AS implied_pct,
                       ROUND(gd.edge_probability * 100, 1) AS edge_pct,
                       gd.market_odd, b.bookmaker_name, gd.detected_at
                FROM model.golf_deals gd
                JOIN core.golf_tournaments gt ON gt.golf_tournament_id = gd.golf_tournament_id
                LEFT JOIN core.bookmakers b ON b.bookmaker_id = gd.bookmaker_id
                WHERE gd.golf_tournament_id = %s
                  AND gd.status_code = 'ACTIVE' AND gd.result_code IS NULL
                ORDER BY gd.edge_probability DESC, gd.detected_at DESC
                LIMIT 25
                """,
                (golf_tournament_id,),
            )
            matchup_deals = fetch_dicts(
                cursor,
                """
                SELECT gt.tournament_name, gt.tour_code, gt.course_name, gmd.market_code,
                       pk.player_name AS pick_name, opp.player_name AS opp_name,
                       ROUND(gmd.model_probability * 100, 1) AS model_pct,
                       ROUND(gmd.edge_probability * 100, 1) AS edge_pct,
                       gmd.market_odd, b.bookmaker_name
                FROM model.golf_matchup_deals gmd
                JOIN core.golf_tournaments gt ON gt.golf_tournament_id = gmd.golf_tournament_id
                JOIN core.golf_players pk ON pk.golf_player_id = gmd.pick_golf_player_id
                JOIN core.golf_players opp ON opp.golf_player_id = gmd.opponent_golf_player_id
                LEFT JOIN core.bookmakers b ON b.bookmaker_id = gmd.bookmaker_id
                WHERE gmd.golf_tournament_id = %s
                  AND gmd.status_code = 'ACTIVE' AND gmd.result_code IS NULL
                ORDER BY gmd.edge_probability DESC
                LIMIT 25
                """,
                (golf_tournament_id,),
            )
            matchups = fetch_dicts(
                cursor,
                """
                SELECT market_code, p1_name, p2_name, p1_probability,
                       p2_probability, tie_probability, deal_odd,
                       deal_edge, deal_bookmaker_name
                FROM reporting.v_golf_matchup_board
                WHERE golf_tournament_id = %s
                ORDER BY GREATEST(p1_probability, p2_probability) DESC NULLS LAST
                LIMIT 20
                """,
                (golf_tournament_id,),
            )
    finally:
        connection.close()

    markets: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        markets.setdefault(str(row["market_code"] or "UNKNOWN"), []).append(row)
    ordered = {code: markets[code] for code in GOLF_MARKET_ORDER if code in markets}
    for code, market_rows in markets.items():
        if code not in ordered:
            ordered[code] = market_rows
    return {
        "tournament": tournament[0],
        "markets": ordered,
        "deals": deals,
        "matchup_deals": matchup_deals,
        "matchups": matchups,
    }


# Categories de strokes-gained (transparence : le POURQUOI d'un pick).
_GOLF_SG_CATS = {"sg_ott": "tee", "sg_app": "app", "sg_arg": "petit jeu", "sg_putt": "putt"}


def _golf_sg_cell(r: dict[str, Any]) -> str:
    """Cellule SG : total strokes-gained + la categorie la plus forte."""
    sg = _to_float(r.get("sg_total"))
    if sg is None:
        return "<td class='num muted'>-</td>"
    cats = [(lbl, _to_float(r.get(key))) for key, lbl in _GOLF_SG_CATS.items()]
    cats = [(lbl, val) for lbl, val in cats if val is not None]
    tag = ""
    if cats:
        best_lbl, _ = max(cats, key=lambda c: c[1])
        tag = f"<div class='muted' style='font-size:11px'>fort: {escape(best_lbl)}</div>"
    return f"<td class='num'><strong>{sg:+.2f}</strong>{tag}</td>"


GOLF_SCOPE_POLICIES: dict[str, dict[str, Any]] = {
    "jour": {
        "label": "Ultra selectif",
        "description": "fenetre courte : seulement les meilleurs edges, exposition reduite.",
        "min_edge_delta": 0.015,
        "min_ev_delta": 0.015,
        "max_positions_factor": 0.35,
        "exposure_factor": 0.45,
        "stake_cap_factor": 0.65,
        "stake_factor": 0.85,
    },
    "semaine": {
        "label": "Selectif",
        "description": "semaine active : shortlist plus stricte que le mois, risque contenu.",
        "min_edge_delta": 0.0075,
        "min_ev_delta": 0.005,
        "max_positions_factor": 0.65,
        "exposure_factor": 0.70,
        "stake_cap_factor": 0.80,
        "stake_factor": 0.95,
    },
    "mois": {
        "label": "Standard",
        "description": "mois courant : profil de risque applique sans correction scope.",
        "min_edge_delta": 0.0,
        "min_ev_delta": 0.0,
        "max_positions_factor": 1.00,
        "exposure_factor": 1.00,
        "stake_cap_factor": 1.00,
        "stake_factor": 1.00,
    },
    "trimestre": {
        "label": "Exploration controlee",
        "description": "horizon large : plus de positions possibles, mise unitaire un peu reduite.",
        "min_edge_delta": -0.003,
        "min_ev_delta": -0.002,
        "max_positions_factor": 1.25,
        "exposure_factor": 1.05,
        "stake_cap_factor": 0.85,
        "stake_factor": 0.90,
    },
    "annee": {
        "label": "Pipeline long terme",
        "description": "annee : couverture large, mais mise par ticket abaissee pour l'incertitude.",
        "min_edge_delta": -0.006,
        "min_ev_delta": -0.004,
        "max_positions_factor": 1.50,
        "exposure_factor": 1.15,
        "stake_cap_factor": 0.65,
        "stake_factor": 0.75,
    },
}


def _golf_scope_policy(periode: str) -> dict[str, Any]:
    return GOLF_SCOPE_POLICIES.get(periode, GOLF_SCOPE_POLICIES[DEFAULT_PERIOD])


def _golf_strategy_candidates(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Deals golf EN COURS -> candidats de portefeuille (proba, cote, edge).

    Pour le STAKING on utilise l'implicite BRUT (1/cote) : c'est le prix reel
    auquel le book paie, donc la seule base coherente pour Kelly + EV (l'edge
    devige sert a DETECTER le deal, pas a dimensionner la mise)."""
    cands: list[dict[str, Any]] = []
    for d in data.get("deals", []):
        odd = float(d["market_odd"])
        model_p = float(d["model_pct"]) / 100.0
        implied_p = 1.0 / odd if odd > 1.0 else model_p
        cands.append({
            "type": "Outright",
            "label": _golf_outright_bet_sentence(d["selection_name"], d["market_code"]),
            "tournoi": f"{_golf_competition_line(d)} - {_golf_place_instruction(d['market_code'])}",
            "market": str(d["market_code"]),
            "model_p": model_p, "implied_p": implied_p, "edge": model_p - implied_p,
            "odd": odd, "book": str(d.get("bookmaker_name") or "-"),
            "deal_type": "OUTRIGHT", "deal_id": d.get("golf_deal_id"),
            "position_count": int(d.get("position_count") or 0),
            "total_stake": d.get("total_stake"),
            "position_ids": d.get("position_ids"),
        })
    for m in data.get("matchup_deals", []):
        odd = float(m["market_odd"])
        model_p = float(m["model_pct"]) / 100.0
        implied_p = 1.0 / odd if odd > 1.0 else model_p
        cands.append({
            "type": "Matchup",
            "label": _golf_matchup_bet_sentence(m["pick_name"], m["opp_name"], m["market_code"]),
            "tournoi": f"{_golf_competition_line(m)} - {_golf_place_instruction(m['market_code'])}",
            "market": str(m["market_code"]),
            "model_p": model_p, "implied_p": implied_p, "edge": model_p - implied_p,
            "odd": odd, "book": str(m.get("bookmaker_name") or "-"),
            "deal_type": "MATCHUP", "deal_id": m.get("golf_matchup_deal_id"),
            "position_count": int(m.get("position_count") or 0),
            "total_stake": m.get("total_stake"),
            "position_ids": m.get("position_ids"),
        })
    return cands


def _cashout_cell(p: dict, return_to: str, qs_mode: bool = False,
                  can_write: bool = True) -> str:
    """Cellule d'actions d'un ticket : CASHOUT (couper le pari en cours) +
    Supprimer. Si deja cashout -> badge avec le P&L reel."""
    pid = int(p["position_id"])
    stake = float(p.get("stake_amount") or 0)
    if p.get("cashed_out_at") is not None:
        amount = float(p.get("cashout_amount") or 0)
        pnl = amount - stake
        cls = "sig" if pnl >= 0 else "muted"
        return (f"<td><span class='pick {cls}' title='Pari coupe en cours de match'>"
                f"CASHOUT {amount:g}$ ({pnl:+.2f}$)</span></td>")
    if not can_write:
        return "<td><span class='muted'>Lecture seule</span></td>"
    target = ("<input type='hidden' name='qs' value='" + escape(return_to) + "' />") if qs_mode \
        else ("<input type='hidden' name='return_to' value='" + escape(return_to) + "' />")
    return (
        "<td style='white-space:nowrap'>"
        "<form method='post' class='inline-form' "
        "onsubmit=\"return confirm('Confirmer le cashout de ce pari ?');\">"
        "<input type='hidden' name='action' value='cashout_position' />"
        f"<input type='hidden' name='position_id' value='{pid}' />"
        + target +
        f"<input type='number' name='cashout_amount' step='any' min='0' placeholder='recu $' "
        "title='Montant recupere au cashout' "
        "style='width:70px;padding:4px;border:1px solid var(--rule);border-radius:7px;font:inherit;font-size:12px' />"
        "<button type='submit' class='button-mini' title='Couper le pari (cashout)'>Cashout</button>"
        "</form> "
        "<form method='post' class='inline-form'>"
        "<input type='hidden' name='action' value='delete_ticket' />"
        f"<input type='hidden' name='position_id' value='{pid}' />"
        + target +
        "<button type='submit' class='button-mini'>Supprimer</button></form></td>"
    )


def render_golf_strategy(
    data: dict[str, Any],
    profile_code: str,
    bankroll: float,
    periode: str = "mois",
    days: int = 30,
    user: UserContext | None = None,
) -> str:
    """Portefeuille golf : mise Kelly credibilisee par profil, plafond d'exposition.
    Reutilise exactement la mecanique bankroll du foot (stake_fraction)."""
    profile = RISK_PROFILES.get(profile_code, RISK_PROFILES[DEFAULT_PROFILE])
    if periode not in STRATEGY_PERIODS:
        periode = DEFAULT_PERIOD
    period_label = STRATEGY_PERIODS[periode][0]
    scope_policy = _golf_scope_policy(periode)
    scoped_profile = replace(
        profile,
        min_edge=max(0.0, profile.min_edge + float(scope_policy["min_edge_delta"])),
        min_expected_value=max(0.0, profile.min_expected_value + float(scope_policy["min_ev_delta"])),
        max_positions=max(1, int(round(profile.max_positions * float(scope_policy["max_positions_factor"])))),
        exposure_cap=min(0.75, profile.exposure_cap * float(scope_policy["exposure_factor"])),
        stake_cap=max(0.001, profile.stake_cap * float(scope_policy["stake_cap_factor"])),
    )
    # Confiance golf : DataGolf est sharp -> proxy croissant avec l'edge.
    sized = []
    for c in _golf_strategy_candidates(data):
        conf = min(0.9, 0.5 + 2.0 * c["edge"])
        frac = stake_fraction(c["model_p"], c["implied_p"], c["odd"], conf, scoped_profile)
        frac *= float(scope_policy["stake_factor"])
        if frac > 1e-4:
            sized.append({**c, "frac": frac})
    sized.sort(key=lambda c: c["frac"], reverse=True)

    kept: list[dict[str, Any]] = []
    exposure = 0.0
    for c in sized[: scoped_profile.max_positions]:
        frac = min(c["frac"], max(0.0, scoped_profile.exposure_cap - exposure))
        if frac <= 1e-4:
            break
        exposure += frac
        kept.append({**c, "frac": frac})

    # Selecteur de profil (liens) + saisie bankroll — sur la page /bankroll golf.
    pills = "".join(
        f"<a class='sport-pill{' active' if p.code == profile.code else ''}' "
        f"href='/bankroll?sport={GOLF_SPORT_PARAM}&golf_profile={p.code}&bankroll={bankroll:g}&periode={escape(periode)}'>{escape(p.label)}</a>"
        for p in RISK_PROFILES.values()
    )
    period_options = "".join(
        f"<option value='{escape(code)}'{' selected' if code == periode else ''}>"
        f"{escape(label)}</option>"
        for code, (label, _) in STRATEGY_PERIODS.items()
    )
    bankroll_form = (
        f"<form method='get' action='/bankroll' class='filterbar'>"
        f"<input type='hidden' name='sport' value='{GOLF_SPORT_PARAM}' />"
        f"<input type='hidden' name='golf_profile' value='{escape(profile.code)}' />"
        "<input type='hidden' name='recalculate' value='1' />"
        "<label>Budget strategie ($)"
        f"<input type='number' name='bankroll' value='{bankroll:g}' min='1' step='any' "
        "/></label>"
        "<label>Scope"
        "<select name='periode'>"
        f"{period_options}</select></label>"
        "<div class='filter-actions'><button type='submit' class='button-primary'>Recalculer</button></div></form>"
    )
    controls = (
        f"<div class='profile-bar'>{pills}</div>"
        f"<p class='strategy-copy'>{escape(profile.description)} "
        f"Scope actif : {escape(period_label)} ({int(days)} jours). "
        f"Vision scope : {escape(str(scope_policy['label']))} - {escape(str(scope_policy['description']))}</p>"
        "<p class='strategy-copy'>"
        f"Parametres reels : edge min {scoped_profile.min_edge * 100:.1f}% · "
        f"EV min {scoped_profile.min_expected_value * 100:.1f}% · "
        f"max {scoped_profile.max_positions} positions · "
        f"exposition max {scoped_profile.exposure_cap * 100:.1f}% · "
        f"plafond par pari {scoped_profile.stake_cap * 100:.1f}%.</p>"
        f"{bankroll_form}"
        f"<p class='client-hint'><strong>Budget utilise :</strong> {bankroll:g}$ pour le golf. "
        "Les colonnes Mise % et Mise $ sont calculees sur ce budget sport, pas sur le cash global.</p>"
    )

    can_edit_positions = can_manage_positions(user)
    read_only_notice = (
        "<p class='client-hint'><strong>Mode lecture seule :</strong> "
        "validation autorisee, mais la prise, la suppression et le cashout des tickets restent reserves a l'administration.</p>"
        if not can_edit_positions else ""
    )
    taken_count = len(data.get("golf_positions") or [])
    if not kept:
        strip = (
            "<div class='verdict-strip'>"
            "<div class='cell'><div class='v'>0</div><div class='l'>Positions</div></div>"
            "<div class='cell'><div class='v'>0.0%</div><div class='l'>Exposition budget</div></div>"
            "<div class='cell'><div class='v'>0$</div><div class='l'>Mise totale</div></div>"
            "<div class='cell'><div class='v'>+0.0$</div><div class='l'>Profit espere</div></div>"
            f"<div class='cell'><div class='v'>{taken_count}</div><div class='l'>Deja prises</div></div>"
            "</div>"
        )
        body = (
            strip
            + read_only_notice
            + "<p class='empty'>Aucune mise recommandee sur ce profil "
            "(edges trop faibles ou cotes hors bornes). Essaie un profil plus agressif.</p>"
        )
    else:
        rows = []
        total_stake = 0.0
        exp_profit = 0.0
        strategy_return_to = f"/bankroll?sport={GOLF_SPORT_PARAM}&golf_profile={profile.code}&bankroll={bankroll:g}&periode={periode}"
        for c in kept:
            stake = c["frac"] * bankroll
            total_stake += stake
            exp_profit += c["model_p"] * (c["odd"] - 1.0) * stake - (1.0 - c["model_p"]) * stake
            rows.append(
                ("<tr class='taken-row'>" if c.get("position_count") else "<tr>")
                + f"<td><strong>{escape(c['label'])}</strong>"
                f"<div class='muted' style='font-size:11px'>{escape(c['tournoi'])} · {escape(c['type'])}</div></td>"
                f"<td>{escape(_golf_market_label(c['market']) if c['type'] == 'Outright' else _golf_matchup_label(c['market']))}</td>"
                f"<td class='num'>{c['odd']:.2f}</td>"
                f"<td class='num'>{c['model_p'] * 100:.1f}</td>"
                + _edge_cell(round(c["edge"] * 100, 1))
                + f"<td class='num'>{c['frac'] * 100:.2f}%</td>"
                f"<td class='num'><strong>{stake:.2f}$</strong></td>"
                + _golf_take_cell(str(c.get("deal_type") or "OUTRIGHT"), c.get("deal_id"),
                                  c.get("odd"), int(c.get("position_count") or 0),
                                  c.get("total_stake"), strategy_return_to,
                                  stake_default=stake,
                                  position_ids=c.get("position_ids"),
                                  can_write=can_edit_positions)
                + "</tr>"
            )
        strip = (
            "<div class='verdict-strip'>"
            f"<div class='cell'><div class='v'>{len(kept)}</div><div class='l'>Positions</div></div>"
            f"<div class='cell'><div class='v'>{exposure * 100:.1f}%</div><div class='l'>Exposition budget</div></div>"
            f"<div class='cell'><div class='v'>{total_stake:.0f}$</div><div class='l'>Mise totale</div></div>"
            f"<div class='cell'><div class='v'>{exp_profit:+.1f}$</div><div class='l'>Profit espere</div></div>"
            f"<div class='cell'><div class='v'>{taken_count}</div><div class='l'>Deja prises</div></div>"
            "</div>"
        )
        table = (
            "<div class='table-wrap'><table><thead><tr><th>Pari</th><th>Marche</th>"
            "<th class='num'>Cote</th><th class='num'>Modele %</th><th class='num'>Edge</th>"
            "<th class='num'>Mise % budget</th><th class='num'>Mise $</th>"
            "<th>Prise (cote / mise $)</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table></div>"
        )
        body = strip + read_only_notice + table

    # Positions DEJA PRISES : bloc permanent — un pari engage ne disparait
    # JAMAIS de la strategie, meme s'il sort du portefeuille recommande.
    golf_positions = data.get("golf_positions") or []
    if golf_positions:
        pos_rows = "".join(
            "<tr class='taken-row'>"
            f"<td><strong>{escape(str(p['selection_label'] or '-'))}</strong></td>"
            f"<td>{escape(_golf_matchup_label(p['market_code']) if p.get('is_matchup') else _golf_market_label(p['market_code']))}</td>"
            f"<td class='num'>{float(p['taken_odd']):.2f}</td>"
            f"<td class='num'><strong>{float(p['stake_amount']):g}$</strong></td>"
            f"<td class='muted'>{format_timestamp(p['taken_at'])[:16]}</td>"
            + _cashout_cell(
                p,
                f"/bankroll?sport={GOLF_SPORT_PARAM}&golf_profile={profile.code}&bankroll={bankroll:g}&periode={periode}",
                can_write=can_edit_positions,
            )
            + "</tr>"
            for p in golf_positions
        )
        total_engaged = sum(float(p.get("stake_amount") or 0) for p in golf_positions)
        body += (
            f"<h3 class='sub-head'>Positions deja prises — {len(golf_positions)} ticket(s) · {total_engaged:g}$ engages</h3>"
            "<div class='table-wrap'><table><thead><tr><th>Pari</th><th>Marche</th>"
            "<th class='num'>Cote prise</th><th class='num'>Mise</th><th>Date de prise</th><th></th></tr></thead>"
            f"<tbody>{pos_rows}</tbody></table></div>"
        )
    return render_section(
        "01", f"Prises de position recommandees - {profile.label}",
        controls + body,
        note="Plan de mise client: quel pari prendre, a quelle cote, et quel montant engager selon le profil choisi. Les positions deja prises restent listees meme si le deal sort du portefeuille.",
        anchor="strategie",
    )


def _golf_take_cell(deal_type: str, deal_id: Any, market_odd: Any,
                    position_count: int = 0, total_stake: Any = None,
                    return_to: str = "", stake_default: float | None = None,
                    position_ids: Any = None, can_write: bool = True) -> str:
    """Cellule de prise d'un deal golf : badge PRIS xN (mise cumulee) +
    mini-formulaire cote/mise -> ticket complet (cote+mise+date en base)."""
    if deal_id is None:
        return "<td class='muted'>-</td>"
    badge = ""
    if position_count:
        stake_txt = f" · {float(total_stake):g}$" if total_stake else ""
        badge = (f"<span class='pick sig' title='Positions prises'>PRIS x{position_count}"
                 f"{stake_txt}</span> ")
    if not can_write:
        hint = "<span class='muted'>Lecture seule</span>" if position_count else "<span class='muted'>Validation seulement</span>"
        return f"<td class='cell-nowrap'>{badge}{hint}</td>"
    ticket_ids = [int(pid) for pid in (position_ids or []) if str(pid).isdigit()]
    delete_controls = ""
    if ticket_ids:
        ticket_forms = "".join(
            "<form method='post' class='inline-form'>"
            "<input type='hidden' name='action' value='delete_ticket' />"
            f"<input type='hidden' name='position_id' value='{pid}' />"
            f"<input type='hidden' name='return_to' value='{escape(return_to)}' />"
            f"<button type='submit' class='button-mini'>Ticket {i}</button>"
            "</form>"
            for i, pid in enumerate(ticket_ids, start=1)
        )
        delete_controls = (
            "<details class='ticket-details'>"
            "<summary class='detail-link'>Supprimer prise</summary>"
            f"<div class='ticket-list'>{ticket_forms}</div>"
            "</details>"
        )
    odd_val = f"{float(market_odd):.2f}" if market_odd else ""
    stake_val = f"{stake_default:.2f}" if stake_default else ""
    form = (
        "<form method='post' class='golf-take-form'>"
        "<input type='hidden' name='action' value='golf_take_position' />"
        f"<input type='hidden' name='golf_deal_type' value='{escape(deal_type)}' />"
        f"<input type='hidden' name='golf_deal_id' value='{escape(str(deal_id))}' />"
        f"<input type='hidden' name='return_to' value='{escape(return_to)}' />"
        f"<input type='number' name='taken_odd' value='{odd_val}' step='any' min='1.01' "
        "title='Cote prise' class='input-compact input-stake' />"
        f"<input type='number' name='stake_amount' value='{stake_val}' step='any' min='0.01' placeholder='mise $' "
        "title='Mise en $' class='input-compact input-stake' />"
        "<button type='submit' class='button-compact'>Prendre</button>"
        "</form>"
    )
    return f"<td class='cell-nowrap'>{badge}{delete_controls}{form}</td>"


def render_golf_strategy_page(
    data: dict[str, Any],
    profile_code: str,
    bankroll: float,
    periode: str = "mois",
    days: int = 30,
    user: UserContext | None = None,
    bankroll_context: dict[str, Any] | None = None,
    validation_notice: str = "",
) -> str:
    """Page /bankroll?sport=golf — la strategie golf sur SA page, DA foot
    (meme structure que la strategie de paris football)."""
    strategy_section = render_golf_strategy(data, profile_code, bankroll, periode, days, user)
    allocation_html = render_strategy_bankroll_context(bankroll_context)
    strategy_return = (
        f"/bankroll?sport={GOLF_SPORT_PARAM}&golf_profile={escape(profile_code)}"
        f"&bankroll={bankroll:g}&periode={escape(periode)}"
    )
    validation_form = strategy_validation_form(GOLF_SPORT_PARAM, strategy_return)
    period_label = STRATEGY_PERIODS.get(periode, STRATEGY_PERIODS[DEFAULT_PERIOD])[0]
    method_section = render_section(
        "02", "Methode",
        "<div class='prose'>"
        "<p>Le portefeuille dimensionne chaque pari en <strong>Kelly credibilise</strong> : "
        "la probabilite du modele (DataGolf, blend + normalisation) est comparee au prix reel du "
        "bookmaker, la mise est une fraction du Kelly plein selon le profil de risque, plafonnee "
        "par pari et par exposition totale. Les deals viennent des marches golf actifs "
        "(vainqueur, top 5/10/20, cut, duels, 3-balls) — chacun avec ses propres garde-fous.</p>"
        "<p>Le reglement est automatique a la fin de chaque tournoi (positions finales DataGolf) "
        "et alimente le <a class='detail-link' href='/back?sport=golf'>track record golf</a>.</p>"
        "</div>",
        note="Meme moteur bankroll que la strategie football (profils prudent / equilibre / agressif).",
    )
    return f"""<!DOCTYPE html>
<html lang="fr">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Strategie golf</title>
  <style>{BASE_CSS}</style>
</head>
<body>
  <main class="shell">
    {render_sport_nav("golf", "strategie", user)}
    <header class="masthead hero-golf">
      <div>
        <h1>Strategie de paris - Golf</h1>
        <p class="sub">Plan de mise personnalise sur les deals golf actifs, avec exposition, profil de risque et scope {escape(period_label)} ({int(days)} jours).</p>
      </div><div class="hero-visual golf" aria-hidden="true"><span class="ball"></span></div>
    </header>
    {validation_notice}
    {validation_form}
    {allocation_html}
    {strategy_section}
    {method_section}
    {render_product_footer("golf")}
  </main>
{dev_reload_script()}
</body>
</html>"""


def render_golf_page(
    data: dict[str, Any],
    report: ActionReport | None = None,
    error_message: str | None = None,
    profile_code: str = DEFAULT_PROFILE,
    bankroll: float = 100.0,
    user: UserContext | None = None,
) -> str:
    active_view = str(data.get("view") or "predictions").strip().lower()
    if active_view not in ("predictions", "deals"):
        active_view = "predictions"
    can_edit_positions = can_manage_positions(user)
    report_html = render_action_report(report, error_message)
    metrics_html = "".join(render_metric(metric) for metric in data["metrics"])
    selected = str(data.get("selected_tournament") or "all")
    filters = data.get("filters") or {
        "tour": "all", "market": "all", "deal_status": "active",
        "date_from": "", "date_to": "",
    }
    selected_detail = next(
        (
            t for t in data.get("tournaments", [])
            if str(t.get("golf_tournament_id")) == selected
        ),
        None,
    )

    # --- Controles EXACTEMENT facon foot : masthead + UN selecteur de
    # competition + boutons d'action. (Chips, filtres multiples et hints
    # supprimes : meme experience que la page football.) --------------------
    tournament_options = [
        f"<option value='all'{' selected' if selected == 'all' else ''}>"
        "Toutes competitions (deals de la semaine)</option>"
    ]
    for t in data.get("tournaments", []):
        value = str(t["golf_tournament_id"])
        date_value = t.get("date_start") or t.get("commence_time")
        label = " - ".join(p for p in (
            str(t["tournament_name"]),
            _golf_tour_label(t.get("tour_code")),
            format_timestamp(date_value)[:10] if date_value else "",
        ) if p)
        tournament_options.append(
            f"<option value='{escape(value)}'{' selected' if selected == value else ''}>"
            f"{escape(label)}</option>"
        )

    controls = (
        render_sport_nav("golf", active_view, user)
        + "<div class='actions dashboard-actions'>"
        "<form method='get'>"
        f"<input type='hidden' name='sport' value='{GOLF_SPORT_PARAM}' />"
        f"<input type='hidden' name='view' value='{escape(active_view)}' />"
        f"<select name='tournament' aria-label='Competition'>{''.join(tournament_options)}</select>"
        "<button type='submit'>Afficher</button>"
        "</form>"
        + (
            "<form method='post'>"
            f"<input type='hidden' name='sport' value='{GOLF_SPORT_PARAM}' />"
            f"<input type='hidden' name='view' value='{escape(active_view)}' />"
            f"<input type='hidden' name='tournament' value='{escape(selected)}' />"
            "<button type='submit' name='action' value='sync_golf_catalog'>Sync catalogue</button>"
            "<button type='submit' name='action' value='sync_golf_odds'>Sync DataGolf</button>"
            "<button type='submit' name='action' value='run_golf_predictions' class='primary'>Predictions golf</button>"
            "<button type='submit' name='action' value='golf_full_refresh'>Cycle golf complet</button>"
            "</form>"
            if has_permission(user, "CYCLE_RUN") and background_actions_enabled() else ""
        )
        + (
            f"<p class='client-hint'><strong>Mode Vercel :</strong> {escape(background_actions_disabled_message())}</p>"
            if has_permission(user, "CYCLE_RUN") and not background_actions_enabled()
            else ""
        )
        + "</div>"
    )
    golf_period = filters.get("period", DEFAULT_GOLF_PERIOD)
    golf_period_chips = "".join(
        f"<a class='period-chip{' active' if code == golf_period else ''}' "
        f"href='/?sport={GOLF_SPORT_PARAM}&view={escape(active_view)}&tournament={escape(selected)}&golf_period={escape(code)}'>"
        f"{escape(label)}</a>"
        for code, (label, _days) in GOLF_PERIODS.items()
    )
    period_bar = (
        "<div class='periodbar'>"
        "<span class='label'>Periode</span>"
        f"{golf_period_chips}"
        "<span class='muted' style='font-size:12px'>Choisis Tout pour afficher le catalogue complet connu.</span>"
        "</div>"
    )

    # DA foot : 01 Classement predit, 02 Deals classes, 03 Strategie,
    # 04 Pilotage & couverture (guides/diagnostics en FIN de page, pas en tete).
    sections: list[str] = []
    pilotage_blocks: list[str] = []

    supported_rows = "".join(
        "<tr>"
        f"<td><strong>{escape(_golf_market_label(code) if code in GOLF_MARKET_LABELS else _golf_matchup_label(code))}</strong></td>"
        f"<td>{escape(path)}</td>"
        "</tr>"
        for code, path in GOLF_BOOK_PATHS.items()
    )
    unsupported_rows = "".join(
        "<tr>"
        f"<td>{escape(name)}</td>"
        f"<td class='muted'>{escape(reason)}</td>"
        "</tr>"
        for name, reason in GOLF_UNSUPPORTED_BOOK_PATHS
    )
    pilotage_blocks.append(
        "<h3 class='sub-head' id='poser'>Ou poser tes paris</h3>"
        "<p class='client-hint'><strong>Regle simple :</strong> prends uniquement les lignes affichees dans "
        "<strong>Deals classes</strong> ou <strong>Strategie golf</strong>. "
        "Ensuite cherche le chemin bookmaker ci-dessous.</p>"
        "<div class='table-wrap'><table><thead><tr><th>Marche du logiciel</th><th>Ou cliquer chez le bookmaker</th></tr></thead>"
        f"<tbody>{supported_rows}</tbody></table></div>"
        "<h3 class='sub-head'>A ne pas jouer automatiquement pour l'instant</h3>"
        "<div class='table-wrap'><table><thead><tr><th>Menu bookmaker</th><th>Pourquoi</th></tr></thead>"
        f"<tbody>{unsupported_rows}</tbody></table></div>"
    )

    coverage_rows = data.get("coverage_rows") or _golf_coverage_rows(data.get("tournaments", []))
    if coverage_rows:
        coverage_html_rows = []
        for row in coverage_rows:
            status_cls = "sig" if row["status"] == "Couvert" else "muted"
            matched = row["matched"] or row["reason"]
            coverage_html_rows.append(
                "<tr>"
                f"<td><strong>{escape(row['book_name'])}</strong></td>"
                f"<td><span class='pick {status_cls}'>{escape(row['status'])}</span></td>"
                f"<td>{escape(matched)}</td>"
                f"<td>{escape(row['action'])}</td>"
                "</tr>"
            )
        markets_note = (
            "<p class='client-hint'><strong>Marches golf V2 pris en compte :</strong> "
            "vainqueur du tournoi, Top 5, Top 10, Top 20, passe le cut, duel tournoi, duel du tour et groupe de 3. "
            "<strong>Non pris volontairement :</strong> golf virtuel, futures sans field stable, formats equipe type Ryder/Presidents Cup, "
            "LPGA/Senior/Legends tant que les donnees joueurs-cotes-resultats ne sont pas assez propres.</p>"
        )
        pilotage_blocks.append(
            "<h3 class='sub-head' id='couverture'>Couverture bookmaker vs modele</h3>"
            + markets_note
            + "<div class='table-wrap'><table><thead><tr><th>Competition / source</th>"
            "<th>Statut</th><th>Explication</th><th>Decision produit</th></tr></thead>"
            f"<tbody>{''.join(coverage_html_rows)}</tbody></table></div>"
        )

    # --- Section 01 : CLASSEMENT PREDIT (un seul bloc, sous-blocs par marche,
    # comme la section Predictions du foot). ---------------------------------
    classement_blocks: list[str] = []
    if data["markets"]:
        for market_code, rows in data["markets"].items():
            limit = GOLF_MARKET_ROW_LIMITS.get(market_code, 20)
            table_rows = []
            for rank, r in enumerate(rows[:limit], start=1):
                model_p = float(r["model_probability"])
                best = _to_float(r.get("best_odd"))
                edge_pct = None
                if best and best > 1:
                    edge_pct = round((model_p - 1.0 / best) * 100, 1)
                owgr = r.get("owgr_rank")
                if selected == "all":
                    # Vue multi-tournois : on montre a quel tournoi appartient la ligne.
                    tournament_id = int(r.get("golf_tournament_id") or 0)
                    sub_html = escape(str(r.get("tournament_name") or ""))
                    if tournament_id:
                        sub_html += f" - <a class='detail-link' href='/golf/{tournament_id}'>Analyse</a>"
                else:
                    sub_html = escape(f"OWGR #{int(owgr)}" if owgr else str(r.get("tour_code") or "").upper())
                table_rows.append(
                    "<tr>"
                    f"<td class='num'><strong>{rank}</strong></td>"
                    f"<td><strong>{escape(_clean_golf_name(r['player_name']))}</strong>"
                    f"<div class='muted' style='font-size:12px'>{sub_html}</div></td>"
                    + _golf_sg_cell(r)
                    + f"<td class='num'><strong>{model_p * 100:.1f}</strong></td>"
                    f"<td class='num'>{float(r['model_fair_odd']):.1f}</td>"
                    + (
                        f"<td class='num'><strong>{best:.1f}</strong></td>"
                        if best else "<td class='num muted'>-</td>"
                    )
                    + _edge_cell(edge_pct)
                    + f"<td class='muted'>{escape(str(r['bookmaker_name'] or '-'))}</td>"
                    "</tr>"
                )
            classement_blocks.append(
                f"<h3 class='sub-head'>{escape(_golf_market_section_title(market_code, len(table_rows)))}</h3>"
                f"<p class='muted' style='font-size:13px;margin:0 0 8px'>{escape(_golf_market_explanation(market_code, len(table_rows)))}</p>"
                "<div class='table-wrap'><table>"
                "<thead><tr><th class='num'>Rang</th><th>Joueur</th>"
                "<th class='num'>SG</th>"
                "<th class='num'>Proba modele %</th><th class='num'>Cote juste</th>"
                "<th class='num'>Meilleure cote</th><th class='num'>Edge</th>"
                "<th>Book</th></tr></thead>"
                f"<tbody>{''.join(table_rows)}</tbody></table></div>"
            )

    if classement_blocks:
        predictions_section = render_section(
            "01", "Predictions",
            "".join(classement_blocks),
            note="Classement par le modele (blend DataGolf + normalisation par marche). Edge rouge = un book paie plus que la proba modele -> value.",
            anchor="classement",
        )
        if active_view == "predictions":
            sections.append(predictions_section)

    # --- Section 02 : DEALS CLASSES (outrights + matchups reunis, comme la
    # section Deals classes du foot). -----------------------------------------
    deals_blocks: list[str] = []
    golf_return_to = f"/?sport={GOLF_SPORT_PARAM}&view=deals&tournament={selected}"
    if data["deals"]:
        deal_rows = "".join(
            ("<tr class='taken-row'>" if int(d.get("position_count") or 0) else "<tr>")
            + f"<td><strong>{escape(_golf_outright_bet_sentence(d['selection_name'], d['market_code']))}</strong>"
            f"<div class='muted' style='font-size:12px'>{escape(_golf_competition_line(d))}</div>"
            f"<div class='muted' style='font-size:12px'>{escape(_golf_place_instruction(d['market_code']))}</div></td>"
            f"<td>{escape(_golf_market_label(d['market_code']))}</td>"
            f"<td class='num'>{d['model_pct']}</td>"
            f"<td class='num'>{d['implied_pct']}</td>"
            + _edge_cell(_to_float(d["edge_pct"]))
            + f"<td class='num'><strong>{float(d['market_odd']):.2f}</strong></td>"
            + f"<td>{escape(str(d['bookmaker_name'] or '-'))}</td>"
            + _golf_take_cell("OUTRIGHT", d.get("golf_deal_id"), d.get("market_odd"),
                              int(d.get("position_count") or 0), d.get("total_stake"),
                              golf_return_to, position_ids=d.get("position_ids"),
                              can_write=can_edit_positions)
            + "</tr>"
            for d in data["deals"]
        )
        deals_blocks.append(
            "<h3 class='sub-head'>Outrights (vainqueur, top, cut)</h3>"
            "<div class='table-wrap'><table>"
            "<thead><tr><th>Pari a prendre</th><th>Type de pari</th>"
            "<th class='num'>Modele %</th><th class='num'>Book %</th>"
            "<th class='num'>Edge</th><th class='num'>Cote</th><th>Book</th>"
            "<th>Prise (cote / mise $)</th></tr></thead>"
            f"<tbody>{deal_rows}</tbody></table></div>"
        )
    else:
        deals_blocks.append(
            "<h3 class='sub-head'>Outrights (vainqueur, top, cut)</h3>"
            "<p class='empty'>Aucun value bet outright sur la cible actuelle - "
            "normal quand le book cible est sharp ou colle au modele.</p>"
        )

    # --- Section : DEALS matchups (le coeur de la value golf) ----------------
    if data.get("matchup_deals"):
        m_rows = "".join(
            ("<tr class='taken-row'>" if int(m.get("position_count") or 0) else "<tr>")
            + f"<td><strong>{escape(_golf_matchup_bet_sentence(m['pick_name'], m['opp_name'], m['market_code']))}</strong>"
            f"<div class='muted' style='font-size:12px'>{escape(_golf_competition_line(m))}</div>"
            f"<div class='muted' style='font-size:12px'>{escape(_golf_place_instruction(m['market_code']))}</div></td>"
            f"<td>{escape(_golf_matchup_label(m['market_code']))}</td>"
            f"<td class='num'>{m['model_pct']}</td>"
            + _edge_cell(_to_float(m["edge_pct"]))
            + f"<td class='num'><strong>{float(m['market_odd']):.2f}</strong></td>"
            + f"<td>{escape(str(m['bookmaker_name'] or '-'))}</td>"
            + _golf_take_cell("MATCHUP", m.get("golf_matchup_deal_id"), m.get("market_odd"),
                              int(m.get("position_count") or 0), m.get("total_stake"),
                              golf_return_to, position_ids=m.get("position_ids"),
                              can_write=can_edit_positions)
            + "</tr>"
            for m in data["matchup_deals"]
        )
        deals_blocks.append(
            "<h3 class='sub-head'>Matchups (joueur contre joueur)</h3>"
            "<div class='table-wrap'><table>"
            "<thead><tr><th>Pari a prendre</th><th>Type de confrontation</th>"
            "<th class='num'>Modele %</th><th class='num'>Edge</th>"
            "<th class='num'>Cote</th><th>Book</th>"
            "<th>Prise (cote / mise $)</th></tr></thead>"
            f"<tbody>{m_rows}</tbody></table></div>"
        )

    # Indicateur de prise agrege sur les lignes visibles dans cette section.
    visible_taken_rows = [
        *(d for d in data.get("deals", []) if int(d.get("position_count") or 0)),
        *(m for m in data.get("matchup_deals", []) if int(m.get("position_count") or 0)),
    ]
    visible_ticket_count = sum(int(r.get("position_count") or 0) for r in visible_taken_rows)
    if visible_ticket_count:
        total_staked = sum(float(r.get("total_stake") or 0) for r in visible_taken_rows)
        taken_summary = (
            "<p style='margin:0 0 12px'>"
            f"<span class='pick sig'>PRIS : {visible_ticket_count} ticket(s) · {total_staked:g}$ engages</span> "
            f"<span class='muted' style='font-size:13px'>sur {len(visible_taken_rows)} deal(s) "
            "— lignes surlignees ci-dessous.</span></p>"
        )
    else:
        taken_summary = (
            "<p class='muted' style='margin:0 0 12px;font-size:13px'>"
            "Aucune position prise pour l'instant — utilise la colonne Prise pour enregistrer un pari reel.</p>"
        )
    deals_section = render_section(
        "02", "Deals classes",
        taken_summary + "".join(deals_blocks),
        note="Chaque ligne est un pari concret : joueur + marche + cote + bookmaker. La value golf vit surtout dans les matchups.",
        anchor="deals",
    )
    if active_view == "deals":
        sections.append(deals_section)

    # --- Section 03 : MARCHE ET PERFORMANCE (courbe d'equite, DA foot) -------
    # La strategie a sa PROPRE page (/bankroll?sport=golf), comme le foot.
    settled_count = int(data.get("settled_count") or 0)
    if settled_count >= 2 and len(data.get("equity_series") or []) >= 3:
        perf_body = (
            "<div class='verdict-strip'>"
            f"<div class='cell'><div class='v'>{settled_count}</div><div class='l'>Deals regles</div></div>"
            f"<div class='cell'><div class='v'>{data.get('settled_won', 0)}</div><div class='l'>Gagnes</div></div>"
            f"<div class='cell'><div class='v'>{(data.get('settled_profit') or 0):+.2f}</div><div class='l'>Profit (unites)</div></div>"
            "</div>"
            f"<h3 class='sub-head'>Courbe d'equite - deals golf regles (mise plate 1 unite)</h3>"
            + svg_line_chart(
                [("Profit cumule", "#0E1B2E", data["equity_series"])],
                height=220, y_format="{:+.2f}",
                x_labels=("pari 1", f"pari {settled_count}"),
            )
        )
    else:
        perf_body = (
            "<p class='empty'>La courbe d'equite apparaitra des que des deals golf "
            "seront regles (reglement automatique a la fin de chaque tournoi, "
            "positions finales DataGolf).</p>"
        )
    if active_view == "deals":
        sections.append(render_section(
            "03", "Marche et performance",
            perf_body,
            note="Courbe calculee sur les deals golf regles (mise plate 1 unite), comme la page football.",
        ))

    # --- Section 04 : PILOTAGE (guides en fin de page, DA foot) --------------
    if pilotage_blocks and active_view == "deals":
        sections.append(render_section(
            "04", "Pilotage",
            "".join(pilotage_blocks),
            note="Guides d'execution : ou cliquer chez le bookmaker, et pourquoi certains marches du book ne sont pas analyses.",
            anchor="pilotage",
        ))

    if not sections:
        empty_title = "Predictions" if active_view == "predictions" else "Deals classes"
        empty_message = (
            "Aucune prediction golf en base pour cette cible. Lance Sync DataGolf puis Predictions golf."
            if active_view == "predictions"
            else "Aucun deal golf actif pour cette cible."
        )
        sections = [render_section("01", empty_title, f"<p class='empty'>{escape(empty_message)}</p>")]

    if not data["markets"] and not data["deals"] and not data.get("matchup_deals"):
        if selected_detail and str(selected_detail.get("catalog_status") or "").upper() == "DISCOVERED":
            empty_body = (
                "<p class='empty'>Ce championnat est disponible dans le catalogue, mais ses donnees "
                "ne sont pas encore chargees. Clique <strong>Sync DataGolf</strong> pour charger "
                "joueurs/cotes/matchups, puis <strong>Predictions golf</strong>.</p>"
            )
        else:
            empty_body = (
                "<p class='empty'>Aucune prediction golf en base. Lance d'abord "
                "Sync DataGolf puis Predictions golf.</p>"
            )
        sections = [render_section("01", "Golf", empty_body)]

    return f"""<!DOCTYPE html>
<html lang="fr">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Golf Deals V2</title>
  <style>{BASE_CSS}</style>
</head>
<body>
    <main class="shell">
    {controls}
    {report_html}
    <div class="metrics">{metrics_html}</div>
    {period_bar}
    {"".join(sections)}
    {render_product_footer("golf")}
  </main>
{dev_reload_script()}
</body>
</html>"""


def render_golf_detail(data: dict[str, Any], user: UserContext | None = None) -> str:
    tournament = data["tournament"]
    markets = data.get("markets", {})
    deals = data.get("deals", [])
    matchup_deals = data.get("matchup_deals", [])
    matchups = data.get("matchups", [])
    can_edit_positions = can_manage_positions(user)
    title = str(tournament.get("tournament_name") or "Tournoi golf")
    context_parts = [
        _golf_tour_label(tournament.get("tour_code")),
        str(tournament.get("course_name") or "").strip(),
        format_timestamp(tournament.get("commence_time"))[:10] if tournament.get("commence_time") else "",
    ]
    context_line = " - ".join(part for part in context_parts if part and part != "Golf")
    player_count = len({
        str(r.get("player_name") or "")
        for rows in markets.values()
        for r in rows
        if r.get("player_name")
    })
    market_count = len(markets)
    strip = (
        "<div class='verdict-strip'>"
        f"<div class='cell'><div class='v'>{player_count}</div><div class='l'>Joueurs modelises</div></div>"
        f"<div class='cell'><div class='v'>{market_count}</div><div class='l'>Marches outright</div></div>"
        f"<div class='cell'><div class='v'>{len(deals)}</div><div class='l'>Deals joueurs</div></div>"
        f"<div class='cell'><div class='v'>{len(matchup_deals)}</div><div class='l'>Deals duels</div></div>"
        "</div>"
    )
    summary = render_section(
        "01", "Resume analyse",
        strip
        + "<p class='client-hint'><strong>Lecture client :</strong> cette page explique un tournoi precis. "
        "Le classement montre les joueurs que le modele aime, les Deals disent quoi jouer, et les Duels montrent les confrontations joueur contre joueur.</p>",
        note="Un tournoi bookmaker peut avoir un nom different du nom DataGolf; la page principale garde la table de correspondance.",
    )

    best_rows: list[str] = []
    winner_rows = markets.get("TOURNAMENT_WINNER") or next(iter(markets.values()), [])
    for rank, r in enumerate(winner_rows[:12], start=1):
        model_p = float(r["model_probability"])
        best = _to_float(r.get("best_odd"))
        edge_pct = round((model_p - 1.0 / best) * 100, 1) if best and best > 1 else None
        best_rows.append(
            "<tr>"
            f"<td class='num'><strong>{rank}</strong></td>"
            f"<td><strong>{escape(_clean_golf_name(r['player_name']))}</strong>"
            f"<div class='muted' style='font-size:12px'>{escape(str(r.get('country_code') or ''))}</div></td>"
            + _golf_sg_cell(r)
            + f"<td class='num'>{model_p * 100:.1f}</td>"
            f"<td class='num'>{float(r['model_fair_odd']):.1f}</td>"
            + (f"<td class='num'>{best:.1f}</td>" if best else "<td class='num muted'>-</td>")
            + _edge_cell(edge_pct)
            + "</tr>"
        )
    best_rows_empty = "<tr><td colspan='7' class='muted'>Aucun classement disponible.</td></tr>"
    ranking = render_section(
        "02", "Pourquoi le modele aime ces joueurs",
        "<div class='table-wrap'><table><thead><tr><th class='num'>Rang</th><th>Joueur</th>"
        "<th class='num'>SG</th><th class='num'>Proba %</th><th class='num'>Cote juste</th>"
        "<th class='num'>Meilleure cote</th><th class='num'>Edge</th></tr></thead>"
        f"<tbody>{''.join(best_rows) if best_rows else best_rows_empty}</tbody></table></div>",
        note="SG = strokes gained. Le tag 'fort' indique la categorie dominante: tee, approche, petit jeu ou putting.",
    )

    deal_rows = []
    for d in deals:
        deal_rows.append(
            "<tr>"
            f"<td><strong>{escape(_golf_outright_bet_sentence(d['selection_name'], d['market_code']))}</strong>"
            f"<div class='muted' style='font-size:12px'>{escape(_golf_place_instruction(d['market_code']))}</div></td>"
            f"<td>{escape(_golf_market_label(d['market_code']))}</td>"
            f"<td class='num'>{d['model_pct']}</td>"
            f"<td class='num'>{d['implied_pct']}</td>"
            + _edge_cell(_to_float(d["edge_pct"]))
            + f"<td class='num'><strong>{float(d['market_odd']):.2f}</strong></td>"
            f"<td>{escape(str(d.get('bookmaker_name') or '-'))}</td>"
            "</tr>"
        )
    for m in matchup_deals:
        deal_rows.append(
            "<tr>"
            f"<td><strong>{escape(_golf_matchup_bet_sentence(m['pick_name'], m['opp_name'], m['market_code']))}</strong>"
            f"<div class='muted' style='font-size:12px'>{escape(_golf_place_instruction(m['market_code']))}</div></td>"
            f"<td>{escape(_golf_matchup_label(m['market_code']))}</td>"
            f"<td class='num'>{m['model_pct']}</td>"
            "<td class='num muted'>-</td>"
            + _edge_cell(_to_float(m["edge_pct"]))
            + f"<td class='num'><strong>{float(m['market_odd']):.2f}</strong></td>"
            f"<td>{escape(str(m.get('bookmaker_name') or '-'))}</td>"
            "</tr>"
        )
    deal_rows_empty = "<tr><td colspan='7' class='muted'>Aucun deal actif pour ce tournoi.</td></tr>"
    deals_section = render_section(
        "03", "Paris recommandes",
        "<div class='table-wrap'><table><thead><tr><th>Pari clair</th><th>Marche</th>"
        "<th class='num'>Modele %</th><th class='num'>Book %</th><th class='num'>Edge</th>"
        "<th class='num'>Cote</th><th>Book</th></tr></thead>"
        f"<tbody>{''.join(deal_rows) if deal_rows else deal_rows_empty}</tbody></table></div>",
        note="Un pari recommande exige une proba modele superieure a la proba implicite de la cote, apres filtres de qualite.",
        anchor="deals",
    )

    matchup_rows = []
    for m in matchups[:12]:
        p1 = float(m.get("p1_probability") or 0)
        p2 = float(m.get("p2_probability") or 0)
        pick = m["p1_name"] if p1 >= p2 else m["p2_name"]
        opp = m["p2_name"] if p1 >= p2 else m["p1_name"]
        prob = max(p1, p2) * 100
        deal_odd_cell = (
            f"<td class='num'>{float(m['deal_odd']):.2f}</td>"
            if m.get("deal_odd") else "<td class='num muted'>-</td>"
        )
        matchup_rows.append(
            "<tr>"
            f"<td><strong>{escape(_golf_matchup_bet_sentence(pick, opp, m['market_code']))}</strong></td>"
            f"<td>{escape(_golf_matchup_label(m['market_code']))}</td>"
            f"<td class='num'>{prob:.1f}</td>"
            + _edge_cell(round(float(m["deal_edge"]) * 100, 1) if m.get("deal_edge") is not None else None)
            + deal_odd_cell
            + f"<td>{escape(str(m.get('deal_bookmaker_name') or '-'))}</td>"
            "</tr>"
        )
    matchup_rows_empty = "<tr><td colspan='6' class='muted'>Aucun duel disponible pour ce tournoi.</td></tr>"
    matchup_section = render_section(
        "04", "Analyse des confrontations",
        "<div class='table-wrap'><table><thead><tr><th>Lecture du duel</th><th>Type</th>"
        "<th class='num'>Proba cote forte %</th><th class='num'>Edge deal</th>"
        "<th class='num'>Cote deal</th><th>Book</th></tr></thead>"
        f"<tbody>{''.join(matchup_rows) if matchup_rows else matchup_rows_empty}</tbody></table></div>",
        note="Les matchups sont souvent plus lisibles que les vainqueurs de tournoi: variance plus basse, marche plus direct.",
    )

    coverage = render_section(
        "05", "Marches couverts et limites",
        "<p class='client-hint'><strong>Couvert maintenant :</strong> vainqueur, Top 5, Top 10, Top 20, cut, duel tournoi, duel du tour, groupe de 3. "
        "<strong>Pas encore couvert :</strong> position exacte, each-way, round leader, equipe/match-play, virtuel, futures tres lointains sans field stable.</p>",
        note="On prefere refuser un marche mal alimente plutot que donner une fausse precision vendable mais dangereuse.",
    )

    return f"""<!DOCTYPE html>
<html lang="fr">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Analyse golf - {escape(title)}</title>
  <style>{BASE_CSS}</style>
</head>
<body>
  <main class="shell">
    {render_sport_nav("golf", "predictions")}
    <a class="back" href="/?sport={GOLF_SPORT_PARAM}">Retour golf</a>
    <header class="matchline">
      <div>
        <h1>{escape(title)}</h1>
        <p class="meta">{escape(context_line)}</p>
      </div>
      <a class="detail-link" href="/back?sport=golf">Voir performance golf</a>
    </header>
    {summary}
    {ranking}
    {deals_section}
    {matchup_section}
    {coverage}
    {render_product_footer("golf")}
  </main>
{dev_reload_script()}
</body>
</html>"""


# ---------------------------------------------------------------------------
# Espace "back" : historique verifie de tous les pronostics et deals.
# ---------------------------------------------------------------------------

BACK_KIND_LABELS = {
    "DEAL": "Deal",
    "PRONOSTIC": "Pronostic",
    "OUTRIGHT": "Long terme",
    "PARLAY": "Combine",
}
BACK_MARKET_LABELS = {
    "1X2": "1X2",
    "OU15": "+/-1,5 but",
    "OU25": "+/-2,5 buts",
    "OU35": "+/-3,5 buts",
    "BTTS": "BTTS",
    "DOUBLE_CHANCE": "Double chance",
    "DNB": "Remb. si nul",
    "HANDICAP": "Handicap",
    "TOURNAMENT_WINNER": "Vainqueur tournoi",
    "LEAGUE_WINNER": "Champion",
    "TOP_SCORER": "Meilleur buteur",
}


def _annotation_key(row: dict[str, Any]) -> str:
    """Cle stable d'une reco pour les annotations (position prise / note)."""
    scope = (
        f"f{row['fixture_id']}" if row.get("fixture_id")
        else f"o{row['outright_market_id']}"
    )
    return f"{row['bet_kind']}|{scope}|{row['market_code']}|{row['selection_code']}"


def load_back_data(filters: dict[str, str]) -> dict[str, Any]:
    """Track record filtre + statistiques + serie pour le graphe.

    Regle d'abord les matchs termines depuis le dernier passage (idempotent :
    ne traite que les nouveaux resultats) — c'est le "tu verifies a chaque
    actualisation" demande. Un echec de reglement ne bloque jamais la page.
    """
    try:
        settle_connection = connect_db(DatabaseSettings.from_env())
        try:
            settle_pending_bets(settle_connection)
            # Les resultats des paris PRIS impactent la bankroll de chaque
            # utilisateur (BET_WON/BET_LOST/BET_VOID au ledger, idempotent).
            reconcile_bankroll_settlements(settle_connection)
        finally:
            settle_connection.close()
    except Exception:
        pass

    connection = connect_db(DatabaseSettings.from_env())
    try:
        with connection.cursor() as cursor:
            # Tri par DATE DU MATCH (kickoff) — le plus important selon
            # l'utilisateur. asc = du plus ancien au plus recent.
            sort_asc = filters.get("sort", "date_desc") == "date_asc"
            order_clause = (
                "ORDER BY kickoff_utc ASC NULLS LAST"
                if sort_asc
                else "ORDER BY kickoff_utc DESC NULLS LAST"
            )
            rows = fetch_dicts(
                cursor,
                f"""
                SELECT tr.bet_kind, tr.fixture_id, tr.outright_market_id, tr.league_name,
                       tr.home_team_name, tr.away_team_name, tr.kickoff_utc,
                       tr.market_code, tr.selection_code,
                       (
                           SELECT vb_line.line
                           FROM model.value_bets vb_line
                           WHERE vb_line.fixture_id = tr.fixture_id
                             AND vb_line.market_code = tr.market_code
                             AND vb_line.selection_code = tr.selection_code
                           ORDER BY vb_line.detected_at DESC, vb_line.value_bet_id DESC
                           LIMIT 1
                       ) AS line,
                       tr.subject_label,
                       tr.model_probability, tr.model_fair_odd, tr.market_odd, tr.edge_probability,
                       tr.result_code, tr.profit_units, tr.settled_at, tr.actual_outcome,
                       (COALESCE(user_pos.position_count, 0) > 0) AS taken,
                       COALESCE(user_pos.position_count, 0) AS position_count,
                       user_pos.avg_taken_odd AS taken_odd,
                       user_pos.total_stake AS stake_amount,
                       user_pos.last_taken_at AS taken_at,
                       CASE
                         WHEN tr.result_code = 'WON' THEN ROUND((user_pos.total_stake * (user_pos.avg_taken_odd - 1))::numeric, 2)
                         WHEN tr.result_code = 'LOST' THEN -user_pos.total_stake
                         WHEN tr.result_code IN ('VOID', 'PUSH') THEN 0
                         ELSE NULL
                       END AS taken_profit_units,
                       ann.note
                FROM reporting.v_track_record tr
                LEFT JOIN LATERAL (
                    SELECT COUNT(*)::integer AS position_count,
                           ROUND(SUM(p.stake_amount)::numeric, 2) AS total_stake,
                           ROUND((SUM(p.stake_amount * p.taken_odd) / NULLIF(SUM(p.stake_amount), 0))::numeric, 4) AS avg_taken_odd,
                           MAX(p.taken_at) AS last_taken_at,
                           COUNT(*) FILTER (WHERE p.cashed_out_at IS NOT NULL)::integer AS cashed_count,
                           ROUND(SUM(p.cashout_amount) FILTER (WHERE p.cashed_out_at IS NOT NULL)::numeric, 2) AS cashed_total,
                           ROUND(SUM(p.stake_amount) FILTER (WHERE p.cashed_out_at IS NOT NULL)::numeric, 2) AS cashed_stake,
                           ROUND(SUM(p.stake_amount) FILTER (WHERE p.cashed_out_at IS NULL)::numeric, 2) AS live_stake
                    FROM model.user_bet_positions p
                    WHERE p.user_id = %(user_id)s
                      AND p.deleted_at IS NULL
                      AND p.bet_kind = tr.bet_kind
                      AND p.fixture_id IS NOT DISTINCT FROM tr.fixture_id
                      AND p.outright_market_id IS NOT DISTINCT FROM tr.outright_market_id
                      AND p.market_code = tr.market_code
                      AND p.selection_code = tr.selection_code
                      AND (tr.market_code <> 'HANDICAP' OR p.line IS NOT DISTINCT FROM (
                           SELECT vb_line.line
                           FROM model.value_bets vb_line
                           WHERE vb_line.fixture_id = tr.fixture_id
                             AND vb_line.market_code = tr.market_code
                             AND vb_line.selection_code = tr.selection_code
                           ORDER BY vb_line.detected_at DESC, vb_line.value_bet_id DESC
                           LIMIT 1
                      ))
                ) user_pos ON true
                LEFT JOIN model.user_bet_annotations ann
                  ON ann.user_id = %(user_id)s
                 AND ann.bet_kind = tr.bet_kind
                 AND ann.fixture_id IS NOT DISTINCT FROM tr.fixture_id
                 AND ann.outright_market_id IS NOT DISTINCT FROM tr.outright_market_id
                 AND ann.market_code = tr.market_code
                 AND ann.selection_code = tr.selection_code
                -- Type simplifie : DEAL englobe le long terme (les strategies
                -- sont des deals dimensionnes) ; PRONOSTIC et COMBINE a part.
                WHERE (
                      %(kind)s = 'all'
                      OR (%(kind)s = 'DEAL' AND tr.bet_kind IN ('DEAL', 'OUTRIGHT'))
                      OR tr.bet_kind = %(kind)s
                  )
                  -- Marches : MULTI-selection (liste vide = tous).
                  AND (%(markets_all)s OR tr.market_code = ANY(%(markets)s))
                  AND (%(league)s = 'all' OR tr.league_name = %(league)s)
                  AND (%(taken)s = 'all' OR COALESCE(user_pos.position_count, 0) > 0)
                  -- Statuts : MULTI-selection (OR entre statuts coches).
                  AND (
                      %(statuses_all)s
                      OR (%(w_pending)s AND tr.result_code IS NULL)
                      OR (%(w_settled)s AND tr.result_code IS NOT NULL)
                      OR (%(w_won)s AND tr.result_code = 'WON')
                      OR (%(w_lost)s AND tr.result_code = 'LOST')
                      OR (%(w_lost_draw)s
                          AND tr.result_code = 'LOST' AND tr.actual_outcome = 'DRAW'
                          AND tr.selection_code IN ('HOME', 'AWAY'))
                  )
                  -- Calendrier : un jour precis, sur la date du MATCH ou de la PRISE.
                  AND (
                      %(day)s = ''
                      OR (%(date_kind)s = 'prise' AND user_pos.last_taken_at::date = %(day)s::date)
                      OR (%(date_kind)s <> 'prise' AND tr.kickoff_utc::date = %(day)s::date)
                  )
                  AND (%(is_admin)s OR COALESCE(user_pos.position_count, 0) > 0)
                {order_clause}
                LIMIT 300
                """,
                {
                    "user_id": int(filters.get("user_id") or 1),
                    "kind": filters.get("kind", "all"),
                    "markets_all": not filters.get("markets"),
                    "markets": list(filters.get("markets") or []) or ["__none__"],
                    "league": filters.get("league", "all"),
                    "taken": filters.get("taken", "all"),
                    "statuses_all": not filters.get("statuses"),
                    "w_pending": "pending" in (filters.get("statuses") or []),
                    "w_settled": "settled" in (filters.get("statuses") or []),
                    "w_won": "won" in (filters.get("statuses") or []),
                    "w_lost": "lost" in (filters.get("statuses") or []),
                    "w_lost_draw": "lost_draw" in (filters.get("statuses") or []),
                    "day": filters.get("day", "") or "",
                    "date_kind": filters.get("date_kind", "match"),
                    "is_admin": filters.get("is_admin") == "1",
                },
            )
            league_options = [
                str(r["league_name"]) for r in fetch_dicts(
                    cursor,
                    "SELECT DISTINCT league_name FROM reporting.v_track_record "
                    "WHERE league_name IS NOT NULL ORDER BY league_name",
                )
            ]
            if filters.get("sport", "all") in ("all", "golf"):
                golf_competitions = [
                    str(r["tournament_name"]) for r in fetch_dicts(
                        cursor,
                        """
                        SELECT DISTINCT tournament_name
                        FROM core.golf_tournaments
                        WHERE tournament_name IS NOT NULL
                        ORDER BY tournament_name
                        """,
                    )
                ]
                league_options = sorted(set(league_options + golf_competitions))
            if filters.get("sport", "all") == "golf":
                rows = []
            # Tickets individuels (un pari peut etre pris plusieurs fois) pour
            # l'affichage detaille + suppression par ticket dans le Back.
            ticket_rows = fetch_dicts(
                cursor,
                """
                SELECT position_id, bet_kind, fixture_id, outright_market_id,
                       market_code, selection_code, selection_label, line,
                       taken_odd, stake_amount, taken_at,
                       cashout_amount, cashed_out_at
                FROM model.user_bet_positions
                WHERE user_id = %s AND deleted_at IS NULL
                ORDER BY taken_at NULLS LAST, position_id
                """,
                (int(filters.get("user_id") or 1),),
            )
            # Track record GOLF (modele DataGolf) : deals outright + matchups.
            # Les filtres du Back doivent aussi s'appliquer ici, sinon
            # "prises seulement" peut afficher des lignes non prises.
            golf_rows: list[dict[str, Any]]
            if filters.get("sport", "all") == "football" or filters.get("kind", "all") not in ("all", "DEAL"):
                golf_rows = []
            else:
                golf_where_parts = ["TRUE"]
                golf_params: dict[str, Any] = {
                    "markets_all": not filters.get("markets"),
                    "markets": list(filters.get("markets") or []) or ["__none__"],
                    "league": filters.get("league", "all"),
                    "taken": filters.get("taken", "all"),
                    "statuses_all": not filters.get("statuses"),
                    "w_pending": "pending" in (filters.get("statuses") or []),
                    "w_settled": "settled" in (filters.get("statuses") or []),
                    "w_won": "won" in (filters.get("statuses") or []),
                    "w_lost": "lost" in (filters.get("statuses") or []),
                    "day": filters.get("day", "") or "",
                    "date_kind": filters.get("date_kind", "match"),
                    "user_id": int(filters.get("user_id") or 1),
                }
                golf_where_parts.append("(%(markets_all)s OR g.market_code = ANY(%(markets)s))")
                golf_where_parts.append("(%(league)s = 'all' OR g.tournament_name = %(league)s)")
                golf_where_parts.append("(%(taken)s = 'all' OR COALESCE(pos.position_count, 0) > 0)")
                golf_where_parts.append(
                    """
                    (
                        %(statuses_all)s
                        OR (%(w_pending)s AND g.result_code IS NULL)
                        OR (%(w_settled)s AND g.result_code IS NOT NULL)
                        OR (%(w_won)s AND g.result_code = 'WON')
                        OR (%(w_lost)s AND g.result_code = 'LOST')
                    )
                    """
                )
                golf_where_parts.append(
                    """
                    (
                        %(day)s = ''
                        OR (%(date_kind)s = 'prise' AND pos.last_taken_at::date = %(day)s::date)
                        OR (%(date_kind)s <> 'prise' AND g.event_date = %(day)s::date)
                    )
                    """
                )
                golf_order = (
                    "ORDER BY pos.last_taken_at ASC NULLS LAST"
                    if filters.get("date_kind") == "prise" and filters.get("sort") == "date_asc"
                    else "ORDER BY pos.last_taken_at DESC NULLS LAST"
                    if filters.get("date_kind") == "prise"
                    else "ORDER BY g.event_date ASC NULLS LAST, g.detected_at ASC"
                    if filters.get("sort") == "date_asc"
                    else "ORDER BY g.event_date DESC NULLS LAST, g.detected_at DESC"
                )
                golf_where = " AND ".join(golf_where_parts)
                golf_rows = fetch_dicts(
                    cursor,
                    f"""
                SELECT g.*, COALESCE(pos.position_count, 0) AS position_count,
                       pos.total_stake, pos.avg_taken_odd, pos.last_taken_at,
                       pos.cashed_count, pos.cashed_total, pos.cashed_stake,
                       -- Profit REEL : resultat sur les mises non-cashout
                       -- + P&L des cashout (recu - mise), toujours connu.
                       ROUND((COALESCE(
                         CASE
                           WHEN g.result_code = 'WON' THEN pos.live_stake * (pos.avg_taken_odd - 1)
                           WHEN g.result_code = 'LOST' THEN -pos.live_stake
                           WHEN g.result_code IN ('VOID', 'PUSH') THEN 0
                           ELSE NULL
                         END, 0) + COALESCE(pos.cashed_total - pos.cashed_stake, 0))::numeric, 2) AS taken_profit
                FROM (
                    SELECT 'OUTRIGHT' AS deal_type, gd.golf_deal_id, NULL::bigint AS golf_matchup_deal_id,
                           gt.tournament_name, gt.tour_code,
                           COALESCE(gt.date_start, gt.commence_time::date) AS event_date,
                           COALESCE(p.player_name, gd.selection_name) AS subject,
                           gd.market_code, gd.market_odd, gd.edge_probability,
                           gd.result_code, gd.profit_units, gd.settled_at, gd.detected_at
                    FROM model.golf_deals gd
                    JOIN core.golf_tournaments gt ON gt.golf_tournament_id = gd.golf_tournament_id
                    LEFT JOIN core.golf_players p ON p.golf_player_id = gd.golf_player_id
                    WHERE gd.status_code = 'ACTIVE'
                    UNION ALL
                    SELECT 'MATCHUP', NULL::bigint, gmd.golf_matchup_deal_id,
                           gt.tournament_name, gt.tour_code,
                           COALESCE(gt.date_start, gt.commence_time::date) AS event_date,
                           pk.player_name || ' vs ' || opp.player_name,
                           gmd.market_code, gmd.market_odd, gmd.edge_probability,
                           gmd.result_code, gmd.profit_units, gmd.settled_at, gmd.detected_at
                    FROM model.golf_matchup_deals gmd
                    JOIN core.golf_tournaments gt ON gt.golf_tournament_id = gmd.golf_tournament_id
                    JOIN core.golf_players pk ON pk.golf_player_id = gmd.pick_golf_player_id
                    JOIN core.golf_players opp ON opp.golf_player_id = gmd.opponent_golf_player_id
                    WHERE gmd.status_code = 'ACTIVE'
                ) g
                LEFT JOIN LATERAL (
                    SELECT COUNT(*)::integer AS position_count,
                           ROUND(SUM(p.stake_amount)::numeric, 2) AS total_stake,
                           ROUND((SUM(p.stake_amount * p.taken_odd) / NULLIF(SUM(p.stake_amount), 0))::numeric, 4) AS avg_taken_odd,
                           MAX(p.taken_at) AS last_taken_at,
                           COUNT(*) FILTER (WHERE p.cashed_out_at IS NOT NULL)::integer AS cashed_count,
                           ROUND(SUM(p.cashout_amount) FILTER (WHERE p.cashed_out_at IS NOT NULL)::numeric, 2) AS cashed_total,
                           ROUND(SUM(p.stake_amount) FILTER (WHERE p.cashed_out_at IS NOT NULL)::numeric, 2) AS cashed_stake,
                           ROUND(SUM(p.stake_amount) FILTER (WHERE p.cashed_out_at IS NULL)::numeric, 2) AS live_stake
                    FROM model.user_bet_positions p
                    WHERE p.bet_kind = 'GOLF'
                      AND p.user_id = %(user_id)s
                      AND p.deleted_at IS NULL
                      AND (
                        (g.golf_deal_id IS NOT NULL AND p.golf_deal_id = g.golf_deal_id)
                        OR (g.golf_matchup_deal_id IS NOT NULL AND p.golf_matchup_deal_id = g.golf_matchup_deal_id)
                      )
                ) pos ON true
                WHERE {golf_where}
                {golf_order}
                LIMIT 200
                """,
                    golf_params,
                )
    finally:
        connection.close()

    golf_settled = [r for r in golf_rows if r["result_code"] in ("WON", "LOST")]
    golf_profit = sum(float(r["profit_units"] or 0) for r in golf_settled)
    golf_taken = [r for r in golf_rows if int(r.get("position_count") or 0) > 0]
    golf_taken_profit = sum(float(r["taken_profit"] or 0) for r in golf_taken
                            if r.get("taken_profit") is not None)
    golf_taken_staked = sum(float(r["total_stake"] or 0) for r in golf_taken
                            if r["result_code"] in ("WON", "LOST"))
    golf_stats = {
        "total": len(golf_rows),
        "pending": sum(1 for r in golf_rows if r["result_code"] is None),
        "settled": len(golf_settled),
        "won": sum(1 for r in golf_settled if r["result_code"] == "WON"),
        "profit_units": round(golf_profit, 2),
        "roi_pct": round(100.0 * golf_profit / len(golf_settled), 1) if golf_settled else None,
        "taken_count": sum(int(r.get("position_count") or 0) for r in golf_rows),
        "taken_profit": round(golf_taken_profit, 2),
        "taken_roi_pct": round(100.0 * golf_taken_profit / golf_taken_staked, 1)
        if golf_taken_staked else None,
    }
    golf_chronological = sorted(
        golf_settled,
        key=lambda r: r["settled_at"] or r["detected_at"],
    )
    golf_equity_series: list[tuple[float, float]] = [(0.0, 0.0)]
    golf_running = 0.0
    for i, r in enumerate(golf_chronological, start=1):
        golf_running += float(r["profit_units"] or 0)
        golf_equity_series.append((float(i), round(golf_running, 4)))

    tickets_by_bet: dict[tuple, list[dict[str, Any]]] = {}
    for t in ticket_rows:
        identity = (
            t["bet_kind"], t.get("fixture_id"), t.get("outright_market_id"),
            t["market_code"], t["selection_code"],
        )
        tickets_by_bet.setdefault(identity, []).append(t)

    # Un meme pari (fixture, marche, selection) peut venir de 2 bookmakers ->
    # 2 lignes identiques dans la vue. On mise UNE fois par pari : on
    # deduplique (meilleure cote affichee) pour ne PAS double-compter le
    # profit reel des positions prises.
    deduped: dict[tuple, dict[str, Any]] = {}
    for r in rows:
        identity = (
            r["bet_kind"], r.get("fixture_id"), r.get("outright_market_id"),
            r["market_code"], r["selection_code"],
        )
        existing = deduped.get(identity)
        if existing is None:
            deduped[identity] = r
        else:
            # Garde la meilleure cote marche pour l'affichage.
            if _to_float(r.get("market_odd")) and (
                (_to_float(existing.get("market_odd")) or 0) < (_to_float(r.get("market_odd")) or 0)
            ):
                deduped[identity] = r
    rows = list(deduped.values())

    # Combines pris : lignes PARLAY fusionnees dans le track record. Un
    # combine est toujours "pris" (on ne l'enregistre qu'a la prise). On
    # respecte les filtres kind/status/league/market.
    kind_f = filters.get("kind", "all")
    market_f = filters.get("market", "all")
    league_f = filters.get("league", "all")
    status_f = filters.get("status", "all")
    if kind_f in ("all", "PARLAY") and market_f == "all" and league_f == "all":
        connection2 = connect_db(DatabaseSettings.from_env())
        try:
            with connection2.cursor() as cursor:
                parlay_rows = fetch_dicts(
                    cursor,
                    """
                    SELECT vpb.parlay_id, vpb.combined_odd, vpb.stake_amount,
                           vpb.leg_count, vpb.legs_summary, vpb.taken_at,
                           vpb.last_kickoff, vpb.result_code, vpb.profit_units, vpb.note
                    FROM reporting.v_parlay_board vpb
                    JOIN model.parlay_tickets pt ON pt.parlay_id = vpb.parlay_id
                    WHERE pt.user_id = %s
                      AND pt.deleted_at IS NULL
                    ORDER BY COALESCE(vpb.last_kickoff, vpb.taken_at) DESC
                    """,
                    (int(filters.get("user_id") or 1),),
                )
        finally:
            connection2.close()
        for p in parlay_rows:
            rc = p["result_code"]
            if status_f == "pending" and rc is not None:
                continue
            if status_f == "settled" and rc is None:
                continue
            if status_f == "won" and rc != "WON":
                continue
            if status_f in ("lost", "lost_draw") and rc != "LOST":
                continue
            rows.append({
                "bet_kind": "PARLAY",
                "parlay_id": int(p["parlay_id"]),
                "fixture_id": None, "outright_market_id": None,
                "league_name": None, "home_team_name": None, "away_team_name": None,
                "subject_label": p["legs_summary"],
                "kickoff_utc": p["last_kickoff"],
                "market_code": "PARLAY",
                "selection_code": f"{int(p['leg_count'])} jambes",
                "model_probability": None, "model_fair_odd": None,
                "line": None,
                "market_odd": float(p["combined_odd"]),
                "taken_odd": float(p["combined_odd"]),
                "stake_amount": float(p["stake_amount"]),
                "edge_probability": None,
                "result_code": rc,
                "profit_units": None,
                "taken_profit_units": float(p["profit_units"]) if p["profit_units"] is not None else None,
                "settled_at": None,
                "actual_outcome": None,
                "position_count": 1,
                "note": p["note"],
                "tickets": [],
                "legs_summary": p["legs_summary"],
            })

    # Statistiques sur le sous-ensemble filtre (regles uniquement).
    settled = [r for r in rows if r["result_code"] in ("WON", "LOST")]
    deals_settled = [r for r in settled if r["bet_kind"] in ("DEAL", "OUTRIGHT")]
    won = sum(1 for r in settled if r["result_code"] == "WON")
    profit = sum(float(r["profit_units"] or 0) for r in deals_settled)
    staked = len(deals_settled)  # mise plate 1 unite
    taken_rows = [r for r in rows if int(r.get("position_count") or 0) > 0]
    taken_settled = [r for r in taken_rows if r["result_code"] in ("WON", "LOST")]
    taken_profit = sum(float(r["taken_profit_units"] or 0) for r in taken_settled)
    taken_staked = sum(float(r["stake_amount"] or 1) for r in taken_settled)
    position_count = sum(int(r.get("position_count") or 0) for r in rows)

    stats = {
        "total": len(rows),
        "settled": len(settled),
        "pending": len(rows) - len(settled),
        "won": won,
        "accuracy_pct": round(100.0 * won / len(settled), 1) if settled else None,
        "profit_units": round(profit, 2),
        "roi_pct": round(100.0 * profit / staked, 1) if staked else None,
        "taken_count": position_count,
        "taken_profit_units": round(taken_profit, 2),
        "taken_roi_pct": round(100.0 * taken_profit / taken_staked, 1) if taken_staked else None,
    }

    # Serie du graphe : profit cumule (deals/outright regles, mise plate),
    # sur le sous-ensemble filtre, du plus ancien au plus recent.
    chronological = sorted(
        deals_settled,
        key=lambda r: r["settled_at"] or r["kickoff_utc"],
    )
    equity_series: list[tuple[float, float]] = [(0.0, 0.0)]
    running = 0.0
    for i, r in enumerate(chronological, start=1):
        running += float(r["profit_units"] or 0)
        equity_series.append((float(i), round(running, 4)))

    # Attache les tickets a chaque ligne (pour l'expansion + suppression).
    for r in rows:
        identity = (
            r["bet_kind"], r.get("fixture_id"), r.get("outright_market_id"),
            r["market_code"], r["selection_code"],
        )
        r["tickets"] = tickets_by_bet.get(identity, [])

    return {
        "rows": rows,
        "stats": stats,
        "equity_series": equity_series,
        "equity_count": len(chronological),
        "league_options": league_options,
        "golf_rows": golf_rows,
        "golf_stats": golf_stats,
        "golf_equity_series": golf_equity_series,
        "golf_equity_count": len(golf_chronological),
    }


def render_back_page(
    data: dict[str, Any],
    filters: dict[str, str],
    user: UserContext | None = None,
) -> str:
    current_sport = (filters.get("sport") or "football").strip().lower()
    if current_sport not in ("football", "golf"):
        current_sport = "football"
    can_edit_back = can_manage_positions(user)
    can_refresh_back = can_refresh_validation(user)
    stats = data["stats"]
    rows = data["rows"]
    validation_notice = ""
    if filters.get("action_error"):
        validation_notice = (
            "<div class='report error'><strong>Action refusee.</strong> "
            f"{escape(str(filters.get('action_error') or ''))}</div>"
        )
    elif filters.get("validation_error") == "1":
        validation_notice = (
            "<div class='report error'><strong>Validation Back echouee.</strong> "
            "Le cycle complet n'a pas ete lance; verifie les cles API ou les logs.</div>"
        )
    elif filters.get("validated") == "1":
        validation_notice = (
            "<div class='report success'><strong>Validation legere terminee.</strong> "
            f"Scores foot mis a jour: {escape(str(filters.get('vf', '0')))} · "
            f"Resultats golf mis a jour: {escape(str(filters.get('vg', '0')))} · "
            f"Tickets regles: {escape(str(filters.get('vs', '0')))}. "
            "Aucune prediction ni cote bookmaker n'a ete recalculee.</div>"
        )

    def _select(label: str, param: str, current: str, options: list[tuple[str, str]]) -> str:
        opts = "".join(
            f"<option value='{escape(val)}'{' selected' if val == current else ''}>"
            f"{escape(opt_label)}</option>"
            for val, opt_label in options
        )
        return (
            "<label>"
            f"{escape(label)}"
            f"<select name='{escape(param)}' onchange='this.form.submit()'>{opts}</select></label>"
        )

    day_val = str(filters.get("day", "") or "")
    market_current = (filters.get("markets") or ["all"])[0] if filters.get("markets") else "all"
    status_current = (filters.get("statuses") or ["all"])[0] if filters.get("statuses") else "all"
    football_market_options = [
        ("1X2", "1X2"),
        ("OU15", "+/-1,5 but"),
        ("OU25", "+/-2,5 buts"),
        ("OU35", "+/-3,5 buts"),
        ("BTTS", "BTTS"),
        ("DNB", "Remb. si nul"),
        ("DOUBLE_CHANCE", "Double chance"),
        ("HANDICAP", "Handicap"),
        ("EXACT_SCORE", "Score exact"),
        ("LEAGUE_WINNER", "Champion"),
    ]
    golf_market_options = (
        [(code, _golf_market_label(code)) for code in GOLF_MARKET_LABELS]
        + [(code, _golf_matchup_label(code)) for code in GOLF_MATCHUP_LABELS]
    )
    market_options = golf_market_options if current_sport == "golf" else football_market_options
    reset_href = f"/back?sport={quote(current_sport)}"
    validation_form = (
        "<form method='post' action='/back' class='validation-form'>"
        "<input type='hidden' name='action' value='validate_back'>"
        f"<input type='hidden' name='sport' value='{escape(current_sport)}'>"
        f"<input type='hidden' name='qs' value='{escape(_back_qs(filters))}'>"
        "<button type='submit' class='button-primary'>Valider mes paris</button>"
        "<span class='muted'>Recupere seulement les scores/resultats lies aux positions ouvertes.</span>"
        "</form>"
        if can_refresh_back else ""
    )
    read_only_notice = (
        "<p class='client-hint'><strong>Mode lecture seule :</strong> "
        "validation autorisee, mais la modification des tickets, notes et cashouts est reservee a l'administration.</p>"
        if not can_edit_back else ""
    )

    form_html = (
        "<form method='get' action='/back' class='table-wrap filterbar'>"
        f"<input type='hidden' name='sport' value='{escape(current_sport)}'>"
        # Type simplifie : les strategies sont des deals dimensionnes.
        + _select("Type", "kind", filters.get("kind", "all"), [
            ("all", "Tous"), ("PRONOSTIC", "Pronostics"),
            ("DEAL", "Deals (inclut long terme)"), ("PARLAY", "Combines"),
        ])
        + _select(
            "Competition", "league", filters.get("league", "all"),
            [("all", "Toutes")]
            + [(league, league) for league in data.get("league_options", [])],
        )
        + _select("Positions", "positions", filters.get("taken", "all"), [
            ("all", "Toutes"), ("1", "Prises seulement"),
        ])
        + _select("Tri", "sort", filters.get("sort", "date_desc"), [
            ("date_desc", "Match recent -> ancien"), ("date_asc", "Match ancien -> recent"),
        ])
        # Calendrier : UN jour precis, sur la date du match ou de la prise.
        + "<label>Jour (calendrier)"
        f"<input type='date' name='day' value='{escape(day_val)}' /></label>"
        + _select("Date appliquee a", "date_kind", filters.get("date_kind", "match"), [
            ("match", "Date du match"), ("prise", "Date de la prise"),
        ])
        + _select("Marche", "market", market_current, [
            ("all", "Tous"),
        ] + market_options)
        + _select("Statut", "status", status_current, [
            ("all", "Tous"), ("pending", "En attente"), ("settled", "Regles"),
            ("won", "Gagnes"), ("lost", "Perdus"), ("lost_draw", "Perdus / match nul"),
        ])
        + "<div class='filter-actions'>"
        "<button type='submit' class='button-primary'>Filtrer</button>"
        f"<a class='detail-link' href='{reset_href}'>Reinitialiser</a>"
        "</div>"
        "</form>"
        + validation_form
        + read_only_notice
    )

    strip = (
        "<div class='verdict-strip'>"
        f"<div class='cell'><div class='v'>{stats['total']}</div><div class='l'>Recommandations</div></div>"
        f"<div class='cell'><div class='v'>{stats['settled']}</div><div class='l'>Reglees</div></div>"
        f"<div class='cell'><div class='v'>{stats['accuracy_pct'] if stats['accuracy_pct'] is not None else '-'}%</div><div class='l'>Justesse</div></div>"
        f"<div class='cell'><div class='v'>{stats['profit_units']:+.2f}</div><div class='l'>Profit (unites)</div></div>"
        f"<div class='cell'><div class='v'>{stats['roi_pct'] if stats['roi_pct'] is not None else '-'}%</div><div class='l'>ROI</div></div>"
        f"<div class='cell'><div class='v'>{stats['taken_count']}</div><div class='l'>Positions prises</div></div>"
        f"<div class='cell'><div class='v'>{stats['taken_profit_units']:+.2f}</div><div class='l'>Profit reel prises</div></div>"
        f"<div class='cell'><div class='v'>{stats['taken_roi_pct'] if stats['taken_roi_pct'] is not None else '-'}%</div><div class='l'>ROI reel prises</div></div>"
        "</div>"
    )

    # Graphe : profit cumule sur le sous-ensemble filtre.
    graph_html = ""
    if len(data["equity_series"]) >= 3:
        graph_html = (
            "<h3 class='sub-head'>Profit cumule - "
            f"{data['equity_count']} paris regles (mise plate 1 unite)</h3>"
            + svg_line_chart(
                [("Profit cumule", "#0E1B2E", data["equity_series"])],
                height=220, y_format="{:+.2f}",
                x_labels=("pari 1", f"pari {data['equity_count']}"),
            )
        )
    graph_section = render_section(
        "01", "Performance",
        strip + graph_html,
        note="Justesse = pronostics/paris corrects sur les 90 minutes reglementaires. ROI = profit en mise plate d'1 unite. Le graphe suit le filtre courant.",
    )

    # Tableau historique : lecture des positions prises sur le board live.
    if rows:
        body_rows = []
        for r in rows:
            key = _annotation_key(r)
            if r["bet_kind"] in ("OUTRIGHT", "PARLAY"):
                match_label = escape(str(r["subject_label"] or "-"))
                sub = (
                    "<span class='pick lead'>COMBINE</span>"
                    if r["bet_kind"] == "PARLAY" else escape(str(r["league_name"] or ""))
                )
            else:
                match_label = (
                    f"{escape(str(r['home_team_name'] or '-'))}"
                    f" <span class='muted'>vs</span> "
                    f"{escape(str(r['away_team_name'] or '-'))}"
                )
                sub = escape(str(r["league_name"] or ""))
            result = r["result_code"]
            if result == "WON":
                result_cell = "<td class='sig'><strong>GAGNE</strong></td>"
            elif result == "LOST":
                # Perdu SUR NUL : le match a fini nul et on jouait HOME/AWAY —
                # exactement les cas ou "nul tres possible" prevenait.
                if r.get("actual_outcome") == "DRAW" and r["selection_code"] in ("HOME", "AWAY"):
                    result_cell = "<td class='muted'>Perdu <strong>(nul)</strong></td>"
                else:
                    result_cell = "<td class='muted'>Perdu</td>"
            elif result == "VOID":
                result_cell = "<td class='muted'>Annule</td>"
            else:
                result_cell = "<td class='muted'>En attente</td>"
            model_odd_value = r.get("market_odd") or r.get("model_fair_odd")
            odd = f"{float(model_odd_value):.2f}" if model_odd_value else "-"
            profit = (
                f"{float(r['profit_units']):+.2f}" if r.get("profit_units") is not None else "-"
            )
            taken_odd = f"{float(r['taken_odd']):.2f}" if r.get("taken_odd") else "-"
            stake_amount = f"{float(r['stake_amount']):.2f}" if r.get("stake_amount") else "-"
            taken_profit_cell = (
                f"{float(r['taken_profit_units']):+.2f}"
                if r.get("taken_profit_units") is not None else "-"
            )
            position_count = int(r.get("position_count") or 0)
            position_badge = (
                f"<span class='pick lead'>Pris x{position_count}</span>"
                if position_count > 0 else "<span class='muted'>Non pris</span>"
            )
            note_val = escape(str(r["note"] or ""))
            if r["bet_kind"] == "PARLAY":
                fixture_href = (
                    (
                        "<form method='post' action='/back' class='inline-form'>"
                        "<input type='hidden' name='action' value='delete_parlay'>"
                        f"<input type='hidden' name='parlay_id' value='{int(r['parlay_id'])}'>"
                        f"<input type='hidden' name='qs' value='{escape(_back_qs(filters))}'>"
                        "<button type='submit' title='Supprimer ce combine' class='button-danger'>Supprimer</button></form>"
                    )
                    if can_edit_back else "<span class='muted'>Lecture seule</span>"
                )
            elif r.get("fixture_id"):
                fixture_href = f"<a class='detail-link' href='/match/{int(r['fixture_id'])}'>Analyse</a>"
            else:
                fixture_href = "-"
            note_cell = (
                f"<form method='post' action='/back' class='note-form'>"
                f"<input type='hidden' name='action' value='save_note'>"
                f"<input type='hidden' name='key' value='{escape(key)}'>"
                f"<input type='hidden' name='qs' value='{escape(_back_qs(filters))}'>"
                f"<input type='text' name='note' value='{note_val}' placeholder='ma note...' "
                ">"
                "<button type='submit' class='button-compact'>OK</button>"
                "</form>"
                if can_edit_back
                else (f"<span>{note_val}</span>" if note_val else "<span class='muted'>Lecture seule</span>")
            )
            # Les combines n'ont pas de selection unique : afficher le nb de jambes.
            selection_cell = (
                f"<span class='pick'>{escape(str(r['selection_code']))}</span>"
                if r["bet_kind"] == "PARLAY" else _pick_chip(
                    r["selection_code"], market_code=r.get("market_code"), line=r.get("line")
                )
            )
            body_rows.append(
                ("<tr class='taken-row'>" if int(r.get("position_count") or 0) else "<tr>")
                + f"<td>{match_label}<div class='muted subline'>{sub}</div></td>"
                f"<td class='muted'>{format_timestamp(r['kickoff_utc'])}</td>"
                f"<td>{escape(BACK_KIND_LABELS.get(r['bet_kind'], r['bet_kind']))}</td>"
                f"<td>{position_badge}</td>"
                f"<td>{selection_cell}</td>"
                f"<td class='num'>{odd}</td>"
                f"<td class='num'>{taken_odd}</td>"
                f"<td class='num'>{stake_amount}</td>"
                + result_cell +
                f"<td class='num'>{profit}</td>"
                f"<td class='num'>{taken_profit_cell}</td>"
                f"<td>{note_cell}</td>"
                f"<td>{fixture_href}</td>"
                "</tr>"
            )
            # Sous-ligne : detail des tickets pris (chacun supprimable).
            tickets = r.get("tickets") or []
            if tickets:
                chips = []
                for i, t in enumerate(tickets, start=1):
                    t_odd = f"@{float(t['taken_odd']):.2f}" if t.get("taken_odd") else ""
                    t_stake = f"{float(t['stake_amount']):.2f}u" if t.get("stake_amount") else ""
                    t_label = t.get("selection_label") or _selection_display(
                        str(t.get("selection_code") or ""), str(t.get("market_code") or ""), t.get("line")
                    )
                    if t.get("cashed_out_at") is not None:
                        c_amt = float(t.get("cashout_amount") or 0)
                        c_stake = float(t.get("stake_amount") or 0)
                        cash_html = (f" <span class='pick sig' title='Pari coupe en cours'>"
                                     f"CASHOUT {c_amt:g}$ ({c_amt - c_stake:+.2f}$)</span>")
                        del_html = ""
                    elif not can_edit_back:
                        cash_html = ""
                        del_html = ""
                    else:
                        cash_html = (
                            "<form method='post' action='/back' class='inline-form' "
                            "onsubmit=\"return confirm('Confirmer le cashout ?');\">"
                            "<input type='hidden' name='action' value='cashout_position'>"
                            f"<input type='hidden' name='position_id' value='{int(t['position_id'])}'>"
                            f"<input type='hidden' name='qs' value='{escape(_back_qs(filters))}'>"
                            "<input type='number' name='cashout_amount' step='any' min='0' placeholder='recu $' "
                            "style='width:64px;padding:2px 4px;border:1px solid var(--rule);border-radius:6px;font:inherit;font-size:11px'>"
                            "<button type='submit' title='Couper ce pari (cashout)' class='button-compact'>Cashout</button>"
                            "</form>"
                        )
                        del_html = (
                            "<form method='post' action='/back' class='inline-form'>"
                            "<input type='hidden' name='action' value='delete_ticket'>"
                            f"<input type='hidden' name='position_id' value='{int(t['position_id'])}'>"
                            f"<input type='hidden' name='qs' value='{escape(_back_qs(filters))}'>"
                            "<button type='submit' title='Supprimer ce ticket' class='button-ghost-danger'>X</button>"
                            "</form>"
                        )
                    chips.append(
                        "<span class='ticket-chip'>"
                        f"<span class='muted'>#{i}</span> {escape(str(t_label))} {escape(t_odd)} {escape(t_stake)}"
                        + cash_html + del_html + "</span>"
                    )
                body_rows.append(
                    "<tr class='explain-row'><td colspan='13'>"
                    "<span class='muted tiny-label'>Tickets pris :</span>"
                    + "".join(chips) + "</td></tr>"
                )
        history_html = (
            "<div class='table-wrap'><table>"
            "<thead><tr><th>Match / Sujet</th><th>Echeance</th>"
            "<th>Type</th><th>Position</th><th>Pari</th><th class='num'>Cote modele</th>"
            "<th class='num'>Cote prise</th><th class='num'>Mise</th>"
            "<th>Resultat</th><th class='num'>Profit modele</th>"
            "<th class='num'>Profit reel</th><th>Ma note</th><th>Detail</th></tr></thead>"
            f"<tbody>{''.join(body_rows)}</tbody></table></div>"
        )
    else:
        history_html = "<p class='empty'>Aucune recommandation pour ce filtre.</p>"

    history_section = render_section(
        "02", "Historique verifie",
        history_html,
        note="Legende : Pris xN = nombre de tickets reels saisis. Non pris = recommandation non jouee. Les pronostics peuvent maintenant porter 1X2, BTTS, +/-2,5 buts ou score exact.",
    )

    # --- Track record GOLF (DataGolf) ---------------------------------------
    golf_rows_for_render = list(data.get("golf_rows") or [])
    if filters.get("sport", "all") == "football":
        golf_rows_for_render = []
    if filters.get("taken", "all") != "all":
        golf_rows_for_render = [
            r for r in golf_rows_for_render
            if int(r.get("position_count") or 0) > 0
        ]
    if filters.get("markets"):
        allowed_markets = set(filters.get("markets") or [])
        golf_rows_for_render = [
            r for r in golf_rows_for_render
            if str(r.get("market_code") or "") in allowed_markets
        ]
    if filters.get("league", "all") != "all":
        golf_rows_for_render = [
            r for r in golf_rows_for_render
            if str(r.get("tournament_name") or "") == filters.get("league")
        ]
    gs = data.get("golf_stats", {})
    golf_strip = (
        "<div class='verdict-strip'>"
        f"<div class='cell'><div class='v'>{gs.get('total', 0)}</div><div class='l'>Deals golf</div></div>"
        f"<div class='cell'><div class='v'>{gs.get('settled', 0)}</div><div class='l'>Regles</div></div>"
        f"<div class='cell'><div class='v'>{gs.get('won', 0)}</div><div class='l'>Gagnes</div></div>"
        f"<div class='cell'><div class='v'>{(gs.get('profit_units') or 0):+.2f}</div><div class='l'>Profit (unites)</div></div>"
        f"<div class='cell'><div class='v'>{gs['roi_pct'] if gs.get('roi_pct') is not None else '-'}%</div><div class='l'>ROI</div></div>"
        f"<div class='cell'><div class='v'>{gs.get('taken_count', 0)}</div><div class='l'>Positions prises</div></div>"
        f"<div class='cell'><div class='v'>{(gs.get('taken_profit') or 0):+.2f}$</div><div class='l'>Profit reel prises</div></div>"
        f"<div class='cell'><div class='v'>{gs['taken_roi_pct'] if gs.get('taken_roi_pct') is not None else '-'}%</div><div class='l'>ROI reel prises</div></div>"
        "</div>"
    )
    golf_graph_html = ""
    if len(data.get("golf_equity_series", [])) >= 3:
        golf_graph_html = (
            "<h3 class='sub-head'>Profit cumule golf - "
            f"{data.get('golf_equity_count', 0)} deals regles (mise plate 1 unite)</h3>"
            + svg_line_chart(
                [("Profit golf", "#E4002B", data["golf_equity_series"])],
                height=220, y_format="{:+.2f}",
                x_labels=("deal 1", f"deal {data.get('golf_equity_count', 0)}"),
            )
        )
    if golf_rows_for_render:
        grows = []
        for r in golf_rows_for_render:
            res = r["result_code"]
            if res is None:
                res_cell = "<td class='muted'>en cours</td>"
            else:
                cls = "sig" if res == "WON" else "muted"
                res_cell = f"<td><span class='pick {cls}'>{escape(str(res))}</span></td>"
            profit = r["profit_units"]
            profit_cell = ("<td class='num muted'>-</td>" if profit is None
                           else f"<td class='num'>{float(profit):+.2f}</td>")
            # Prise reelle : nombre de tickets, mise cumulee, cote moyenne,
            # DATE de prise et profit reel en $.
            pos_n = int(r.get("position_count") or 0)
            if pos_n:
                taken_date = format_timestamp(r.get("last_taken_at"))[:16] if r.get("last_taken_at") else "-"
                cashed_n = int(r.get("cashed_count") or 0)
                cash_badge = ""
                if cashed_n:
                    c_tot = float(r.get("cashed_total") or 0)
                    c_stk = float(r.get("cashed_stake") or 0)
                    cash_badge = (f" <span class='pick sig' title='Pari(s) coupe(s) en cours'>"
                                  f"CASHOUT {c_tot:g}$ ({c_tot - c_stk:+.2f}$)</span>")
                taken_cell = (
                    f"<td><span class='pick sig'>PRIS x{pos_n}</span>{cash_badge}"
                    f"<div class='muted' style='font-size:11px'>{float(r.get('total_stake') or 0):g}$ "
                    f"@ {float(r.get('avg_taken_odd') or 0):.2f} · {escape(taken_date)}</div></td>"
                )
                tp = r.get("taken_profit")
                taken_profit_cell = ("<td class='num muted'>-</td>" if tp is None
                                     else f"<td class='num'><strong>{float(tp):+.2f}$</strong></td>")
            else:
                taken_cell = "<td class='muted'>non pris</td>"
                taken_profit_cell = "<td class='num muted'>-</td>"
            grows.append(
                ("<tr class='taken-row'>" if pos_n else "<tr>")
                + f"<td>{escape(str(r['tournament_name']))}"
                f"<div class='muted' style='font-size:11px'>{escape(str(r['tour_code'] or '').upper())} · {escape(str(r['deal_type']))}</div></td>"
                f"<td><strong>{escape(_golf_back_bet_sentence(r))}</strong></td>"
                f"<td>{escape(_golf_matchup_label(r['market_code']) if str(r['deal_type']) == 'MATCHUP' else _golf_market_label(r['market_code']))}</td>"
                f"<td class='num'>{float(r['market_odd']):.2f}</td>"
                + res_cell + profit_cell + taken_cell + taken_profit_cell + "</tr>"
            )
        golf_table = (
            "<div class='table-wrap'><table><thead><tr><th>Competition</th><th>Pari</th>"
            "<th>Type de pari</th><th class='num'>Cote</th><th>Resultat</th>"
            "<th class='num'>Profit</th><th>Prise (mise / date)</th>"
            "<th class='num'>Profit reel</th></tr></thead>"
            f"<tbody>{''.join(grows)}</tbody></table></div>"
        )
    else:
        golf_table = ("<p class='empty'>Aucun deal golf en base. Les deals se reglent "
                      "automatiquement a la fin du tournoi (positions finales DataGolf).</p>")
    golf_section = render_section(
        "03", "Track record golf",
        golf_strip + golf_graph_html + golf_table,
        note="Deals golf (modele DataGolf) : outrights + matchups. Regles automatiquement sur les positions finales du tournoi. Mise plate 1 unite.",
        anchor="golf",
    )

    hero_title = (
        "Back golf - performance verifiee"
        if current_sport == "golf"
        else "Back football - performance verifiee"
    )
    hero_meta = (
        "Deals golf suivis par tournoi, matchups et resultats DataGolf."
        if current_sport == "golf"
        else "Pronostics, deals football et combines verifies a la fin de chaque match."
    )

    return f"""<!DOCTYPE html>
<html lang="fr">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Back - performance verifiee</title>
  <style>{BASE_CSS}</style>
</head>
<body>
  <main class="shell">
    {render_sport_nav(current_sport, "back", user)}
    <header class="matchline">
      <div>
        <h1>{escape(hero_title)}</h1>
        <p class="meta">{escape(hero_meta)}</p>
      </div>
      <div class="hero-visual {'golf' if current_sport == 'golf' else 'football'}" aria-hidden="true"><span class="ball"></span></div>
    </header>
    {validation_notice}
    {form_html}
    {graph_section if current_sport != "golf" else ""}
    {history_section if current_sport != "golf" else ""}
    {golf_section if current_sport != "football" else ""}
    {render_product_footer(current_sport)}
  </main>
{dev_reload_script()}
</body>
</html>"""


def _ensure_user_bankroll_cursor(
    cursor: Any,
    user_id: int,
    default_amount: float = 1000.0,
) -> tuple[int, float]:
    cursor.execute(
        """
        INSERT INTO model.user_bankrolls (user_id, current_amount)
        VALUES (%s, %s)
        ON CONFLICT (user_id) WHERE status_code = 'ACTIVE'
        DO NOTHING
        """,
        (user_id, default_amount),
    )
    cursor.execute(
        """
        SELECT bankroll_id, current_amount
        FROM model.user_bankrolls
        WHERE user_id = %s AND status_code = 'ACTIVE'
        ORDER BY bankroll_id DESC
        LIMIT 1
        """,
        (user_id,),
    )
    row = cursor.fetchone()
    if not row:
        raise ValueError("bankroll introuvable")
    return int(row[0]), float(row[1])


def apply_bankroll_delta(
    cursor: Any,
    user_id: int,
    actor_user_id: int,
    event_type: str,
    amount_delta: float,
    reason: str,
    position_id: int | None = None,
    parlay_id: int | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    bankroll_id, current_amount = _ensure_user_bankroll_cursor(cursor, user_id)
    next_amount = round(current_amount + float(amount_delta), 2)
    if next_amount < 0:
        raise ValueError("bankroll insuffisante pour enregistrer cette prise")
    cursor.execute(
        """
        UPDATE model.user_bankrolls
        SET current_amount = %s, updated_at = now()
        WHERE bankroll_id = %s
        """,
        (next_amount, bankroll_id),
    )
    cursor.execute(
        """
        INSERT INTO model.user_bankroll_events (
            bankroll_id, user_id, actor_user_id, event_type,
            amount_delta, resulting_amount, reason, position_id, parlay_id, metadata
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
        """,
        (
            bankroll_id, user_id, actor_user_id, event_type,
            round(float(amount_delta), 2), next_amount, reason,
            position_id, parlay_id, json.dumps(metadata or {}),
        ),
    )
    cursor.execute(
        """
        INSERT INTO app_auth.audit_log (
            actor_user_id, target_user_id, action_code, entity_type, entity_id, metadata
        )
        VALUES (%s, %s, %s, 'BANKROLL', %s, %s::jsonb)
        """,
        (
            actor_user_id, user_id, event_type,
            str(bankroll_id),
            json.dumps({
                "delta": round(float(amount_delta), 2),
                "resulting_amount": next_amount,
                **(metadata or {}),
            }),
        ),
    )


def _position_settlement_credit(
    result_code: str, stake: float, taken_odd: float,
    market_code: str | None = None,
    profit_units: float | None = None,
    market_odd: float | None = None,
) -> tuple[float, str]:
    """Credit bankroll d'une position reglee (la mise a ete debitee a la prise).

    WON -> retour complet mise x cote prise ; LOST -> 0 ; VOID/PUSH -> mise
    remboursee. HANDICAP asiatique : demi-gain/demi-perte reconstruits depuis
    profit_units du pari (calcule a la cote marche) re-echelonnes a la cote
    PRISE — le ledger reflete l'argent reel."""
    result = str(result_code or "").upper()
    if result in ("VOID", "PUSH"):
        return stake, "BET_VOID"
    if result == "WON":
        if (market_code == "HANDICAP" and profit_units is not None
                and market_odd is not None and float(market_odd) > 1.0):
            ratio = max(0.0, float(profit_units) / (float(market_odd) - 1.0))  # 1 ou 0.5
            return stake * (1.0 + ratio * (taken_odd - 1.0)), "BET_WON"
        return stake * taken_odd, "BET_WON"
    if result == "LOST":
        if market_code == "HANDICAP" and profit_units is not None and float(profit_units) > -1.0:
            # Demi-perte asiatique : une partie de la mise revient.
            return stake * (1.0 + float(profit_units)), "BET_LOST"
        return 0.0, "BET_LOST"
    return 0.0, "BET_LOST"


_SETTLEMENT_EVENT_TYPES = ("BET_WON", "BET_LOST", "BET_VOID", "CASHOUT")
SPORT_ALLOCATION_DEFAULTS = {"football": 0.50, GOLF_SPORT_PARAM: 0.50}


def reconcile_bankroll_settlements(connection) -> dict[str, int]:
    """Impacte la bankroll de CHAQUE utilisateur avec les resultats des paris
    pris (BET_WON / BET_LOST / BET_VOID au ledger). Idempotent : une position
    n'est creditee qu'une fois (NOT EXISTS sur les evenements de reglement) ;
    les positions cashout ou supprimees sont exclues."""
    report = {"positions_settled": 0, "parlays_settled": 0}
    with connection.cursor() as cursor:
        rows: list = []
        # DEAL football : jointure value_bets par identite (ligne comprise).
        cursor.execute(
            """
            SELECT DISTINCT ON (p.position_id)
                p.position_id, p.user_id, p.stake_amount, p.taken_odd,
                vb.result_code, p.market_code, vb.profit_units, vb.market_odd,
                p.selection_label
            FROM model.user_bet_positions p
            JOIN model.value_bets vb
              ON vb.fixture_id = p.fixture_id
             AND vb.market_code = p.market_code
             AND vb.selection_code = p.selection_code
             AND COALESCE(vb.line, -999) = COALESCE(p.line, -999)
            WHERE p.bet_kind = 'DEAL' AND p.fixture_id IS NOT NULL
              AND p.deleted_at IS NULL AND p.cashed_out_at IS NULL
              AND vb.status_code = 'ACTIVE' AND vb.result_code IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM model.user_bankroll_events e
                              WHERE e.position_id = p.position_id
                                AND e.event_type = ANY(%(ev)s))
            ORDER BY p.position_id, vb.settled_at DESC NULLS LAST
            """,
            {"ev": list(_SETTLEMENT_EVENT_TYPES)},
        )
        rows.extend(cursor.fetchall())
        # PRONOSTIC + OUTRIGHT : via le track record unifie (meme identite).
        cursor.execute(
            """
            SELECT DISTINCT ON (p.position_id)
                p.position_id, p.user_id, p.stake_amount, p.taken_odd,
                tr.result_code, p.market_code, NULL::numeric, NULL::numeric,
                p.selection_label
            FROM model.user_bet_positions p
            JOIN reporting.v_track_record tr
              ON tr.bet_kind = p.bet_kind
             AND COALESCE(tr.fixture_id, -1) = COALESCE(p.fixture_id, -1)
             AND COALESCE(tr.outright_market_id, -1) = COALESCE(p.outright_market_id, -1)
             AND tr.market_code = p.market_code
             AND tr.selection_code = p.selection_code
            WHERE p.bet_kind IN ('PRONOSTIC', 'OUTRIGHT')
              AND p.deleted_at IS NULL AND p.cashed_out_at IS NULL
              AND tr.result_code IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM model.user_bankroll_events e
                              WHERE e.position_id = p.position_id
                                AND e.event_type = ANY(%(ev)s))
            ORDER BY p.position_id
            """,
            {"ev": list(_SETTLEMENT_EVENT_TYPES)},
        )
        rows.extend(cursor.fetchall())
        # GOLF : resultat par reference directe au deal.
        cursor.execute(
            """
            SELECT p.position_id, p.user_id, p.stake_amount, p.taken_odd,
                   COALESCE(gd.result_code, gmd.result_code), p.market_code,
                   NULL::numeric, NULL::numeric, p.selection_label
            FROM model.user_bet_positions p
            LEFT JOIN model.golf_deals gd ON gd.golf_deal_id = p.golf_deal_id
            LEFT JOIN model.golf_matchup_deals gmd
              ON gmd.golf_matchup_deal_id = p.golf_matchup_deal_id
            WHERE p.bet_kind = 'GOLF'
              AND p.deleted_at IS NULL AND p.cashed_out_at IS NULL
              AND COALESCE(gd.result_code, gmd.result_code) IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM model.user_bankroll_events e
                              WHERE e.position_id = p.position_id
                                AND e.event_type = ANY(%(ev)s))
            """,
            {"ev": list(_SETTLEMENT_EVENT_TYPES)},
        )
        rows.extend(cursor.fetchall())

        for (position_id, user_id, stake, taken_odd, result, market_code,
             profit_units, market_odd, label) in rows:
            credit, event_type = _position_settlement_credit(
                str(result), float(stake), float(taken_odd), market_code,
                float(profit_units) if profit_units is not None else None,
                float(market_odd) if market_odd is not None else None,
            )
            apply_bankroll_delta(
                cursor, int(user_id), int(user_id), event_type,
                round(credit, 2),
                f"Reglement {str(result)} - {str(label or '')[:80]}",
                position_id=int(position_id),
                metadata={"result": str(result), "taken_odd": float(taken_odd)},
            )
            report["positions_settled"] += 1

        # COMBINES : tickets regles all-legs (retour = mise x cote combinee).
        cursor.execute(
            """
            SELECT pt.parlay_id, pt.user_id, pt.stake_amount, pt.combined_odd,
                   pt.result_code, pt.label
            FROM model.parlay_tickets pt
            WHERE pt.deleted_at IS NULL AND pt.result_code IS NOT NULL
              AND pt.stake_amount IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM model.user_bankroll_events e
                              WHERE e.parlay_id = pt.parlay_id
                                AND e.event_type = ANY(%(ev)s))
            """,
            {"ev": list(_SETTLEMENT_EVENT_TYPES)},
        )
        for parlay_id, user_id, stake, combined_odd, result, label in cursor.fetchall():
            credit, event_type = _position_settlement_credit(
                str(result), float(stake), float(combined_odd or 0) or 1.0)
            apply_bankroll_delta(
                cursor, int(user_id), int(user_id), event_type,
                round(credit, 2),
                f"Reglement combine {str(result)} - {str(label or '')[:70]}",
                parlay_id=int(parlay_id),
                metadata={"result": str(result)},
            )
            report["parlays_settled"] += 1
    connection.commit()
    return report


def save_bet_annotation(
    key: str,
    user_id: int = 1,
    toggle_taken: bool = False,
    note: str | None = None,
    take_position: bool = False,
    clear_position: bool = False,
    taken_odd: float | None = None,
    stake_amount: float | None = None,
    model_probability: float | None = None,
    model_fair_odd: float | None = None,
    selection_label: str | None = None,
    line: float | None = None,
) -> None:
    """Enregistre l'annotation utilisateur (position prise / note) d'une reco.

    key = "BET_KIND|scope|market_code|selection_code" (voir _annotation_key),
    scope = "f<fixture_id>" ou "o<outright_market_id>".
    """
    parts = key.split("|")
    if len(parts) != 4:
        return
    bet_kind, scope, market_code, selection_code = parts
    fixture_id = int(scope[1:]) if scope.startswith("f") and scope[1:].isdigit() else None
    outright_market_id = int(scope[1:]) if scope.startswith("o") and scope[1:].isdigit() else None
    if fixture_id is None and outright_market_id is None:
        return
    if bet_kind not in ("DEAL", "PRONOSTIC", "OUTRIGHT"):
        return

    connection = connect_db(DatabaseSettings.from_env())
    try:
        with connection.cursor() as cursor:
            # Upsert de la ligne d'annotation.
            cursor.execute(
                """
                INSERT INTO model.user_bet_annotations (
                    user_id, bet_kind, fixture_id, outright_market_id, market_code, selection_code
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT
                DO NOTHING
                """,
                (user_id, bet_kind, fixture_id, outright_market_id, market_code, selection_code),
            )
            cursor.execute(
                """
                SELECT annotation_id
                FROM model.user_bet_annotations
                WHERE user_id = %s
                  AND fixture_id IS NOT DISTINCT FROM %s
                  AND outright_market_id IS NOT DISTINCT FROM %s
                  AND market_code = %s AND selection_code = %s
                  AND bet_kind = %s
                """,
                (user_id, fixture_id, outright_market_id, market_code, selection_code, bet_kind),
            )
            annotation_row = cursor.fetchone()
            annotation_id = int(annotation_row[0]) if annotation_row else None
            if toggle_taken:
                cursor.execute(
                    """
                    UPDATE model.user_bet_annotations
                    SET taken = NOT taken, updated_at = now()
                    WHERE user_id = %s
                      AND fixture_id IS NOT DISTINCT FROM %s
                      AND outright_market_id IS NOT DISTINCT FROM %s
                      AND market_code = %s AND selection_code = %s
                      AND bet_kind = %s
                    """,
                    (user_id, fixture_id, outright_market_id, market_code, selection_code, bet_kind),
                )
            if take_position:
                if taken_odd is None or taken_odd <= 1.0:
                    return
                safe_stake = stake_amount if stake_amount is not None and stake_amount > 0 else 1.0
                enforce_user_bet_limits(cursor, user_id, safe_stake)
                cursor.execute(
                    """
                    INSERT INTO model.user_bet_positions (
                        user_id, annotation_id, bet_kind, fixture_id, outright_market_id,
                        market_code, selection_code, selection_label,
                        line, model_probability, model_fair_odd, taken_odd, stake_amount
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING position_id
                    """,
                    (
                        user_id,
                        annotation_id,
                        bet_kind,
                        fixture_id,
                        outright_market_id,
                        market_code,
                        selection_code,
                        (selection_label or "").strip() or None,
                        line,
                        model_probability if model_probability is not None and 0 <= model_probability <= 1 else None,
                        model_fair_odd if model_fair_odd is not None and model_fair_odd > 1 else None,
                        taken_odd,
                        safe_stake,
                    ),
                )
                position_id = int(cursor.fetchone()[0])
                apply_bankroll_delta(
                    cursor,
                    user_id=user_id,
                    actor_user_id=user_id,
                    event_type="STAKE_PLACED",
                    amount_delta=-float(safe_stake),
                    reason=f"Prise {bet_kind} {market_code}/{selection_code}",
                    position_id=position_id,
                    metadata={
                        "bet_kind": bet_kind,
                        "fixture_id": fixture_id,
                        "outright_market_id": outright_market_id,
                        "market_code": market_code,
                        "selection_code": selection_code,
                        "taken_odd": taken_odd,
                    },
                )
                cursor.execute(
                    """
                    UPDATE model.user_bet_annotations
                    SET taken = true,
                        taken_odd = %s,
                        stake_amount = COALESCE(stake_amount, 0) + %s,
                        taken_at = COALESCE(taken_at, now()),
                        updated_at = now()
                    WHERE annotation_id = %s
                    """,
                    (taken_odd, safe_stake, annotation_id),
                )
            if clear_position:
                cursor.execute(
                    """
                    SELECT position_id, stake_amount
                    FROM model.user_bet_positions
                    WHERE user_id = %s
                      AND fixture_id IS NOT DISTINCT FROM %s
                      AND outright_market_id IS NOT DISTINCT FROM %s
                      AND market_code = %s AND selection_code = %s
                      AND bet_kind = %s
                      AND deleted_at IS NULL
                    """,
                    (user_id, fixture_id, outright_market_id, market_code, selection_code, bet_kind),
                )
                active_positions = cursor.fetchall()
                cursor.execute(
                    """
                    UPDATE model.user_bet_positions
                    SET deleted_at = now(), deleted_by = %s, delete_reason = 'clear_position'
                    WHERE user_id = %s
                      AND fixture_id IS NOT DISTINCT FROM %s
                      AND outright_market_id IS NOT DISTINCT FROM %s
                      AND market_code = %s AND selection_code = %s
                      AND bet_kind = %s
                      AND deleted_at IS NULL
                    """,
                    (user_id, user_id, fixture_id, outright_market_id, market_code, selection_code, bet_kind),
                )
                for pos_id, pos_stake in active_positions:
                    apply_bankroll_delta(
                        cursor,
                        user_id=user_id,
                        actor_user_id=user_id,
                        event_type="STAKE_VOIDED",
                        amount_delta=float(pos_stake or 0),
                        reason="Suppression position football",
                        position_id=int(pos_id),
                        metadata={"mode": "clear_position"},
                    )
                cursor.execute(
                    """
                    UPDATE model.user_bet_annotations
                    SET taken = false,
                        taken_odd = NULL,
                        stake_amount = NULL,
                        taken_at = NULL,
                        updated_at = now()
                    WHERE user_id = %s
                      AND fixture_id IS NOT DISTINCT FROM %s
                      AND outright_market_id IS NOT DISTINCT FROM %s
                      AND market_code = %s AND selection_code = %s
                      AND bet_kind = %s
                    """,
                    (user_id, fixture_id, outright_market_id, market_code, selection_code, bet_kind),
                )
            if note is not None:
                cursor.execute(
                    """
                    UPDATE model.user_bet_annotations
                    SET note = %s, updated_at = now()
                    WHERE user_id = %s
                      AND fixture_id IS NOT DISTINCT FROM %s
                      AND outright_market_id IS NOT DISTINCT FROM %s
                      AND market_code = %s AND selection_code = %s
                      AND bet_kind = %s
                    """,
                    (note.strip() or None, user_id, fixture_id, outright_market_id, market_code, selection_code, bet_kind),
                )
        connection.commit()
    finally:
        connection.close()


def load_user_limit_snapshot(cursor, user_id: int) -> dict[str, Any]:
    cursor.execute(
        """
        SELECT
            COALESCE(open_total_stake, 0) AS open_total_stake,
            max_single_bet,
            max_daily_stake,
            max_open_exposure,
            COALESCE(requires_manual_review, false) AS requires_manual_review
        FROM reporting.v_user_admin_profile
        WHERE user_id = %s
        """,
        (user_id,),
    )
    row = cursor.fetchone()
    if not row:
        return {
            "open_total_stake": 0.0,
            "max_single_bet": None,
            "max_daily_stake": None,
            "max_open_exposure": None,
            "requires_manual_review": False,
        }
    return {
        "open_total_stake": float(row[0] or 0),
        "max_single_bet": _to_float(row[1]),
        "max_daily_stake": _to_float(row[2]),
        "max_open_exposure": _to_float(row[3]),
        "requires_manual_review": bool(row[4]),
    }


def enforce_user_bet_limits(cursor, user_id: int, stake_amount: float) -> None:
    stake = round(float(stake_amount or 0), 2)
    if stake <= 0:
        raise ValueError("mise invalide")
    limits = load_user_limit_snapshot(cursor, user_id)
    if limits["requires_manual_review"]:
        raise ValueError("ce compte est en revue manuelle : nouvelles prises bloquees")
    cursor.execute(
        """
        SELECT COALESCE(SUM(-amount_delta), 0)
        FROM model.user_bankroll_events
        WHERE user_id = %s
          AND event_type = 'STAKE_PLACED'
          AND created_at >= date_trunc('day', now())
        """,
        (user_id,),
    )
    daily_stake = float((cursor.fetchone() or [0])[0] or 0)
    max_single = limits["max_single_bet"]
    if max_single is not None and stake > max_single + 0.009:
        raise ValueError(f"mise refusee : maximum par pari {max_single:.2f}$")
    max_daily = limits["max_daily_stake"]
    if max_daily is not None and daily_stake + stake > max_daily + 0.009:
        raise ValueError(f"mise refusee : plafond journalier {max_daily:.2f}$ depasse")
    max_open = limits["max_open_exposure"]
    if max_open is not None and float(limits["open_total_stake"]) + stake > max_open + 0.009:
        raise ValueError(f"mise refusee : exposition ouverte max {max_open:.2f}$ depassee")


def save_golf_position(
    deal_type: str,
    deal_id: int,
    taken_odd: float | None,
    stake_amount: float | None,
    user_id: int = 1,
) -> None:
    """Prise de position sur un deal GOLF (outright ou matchup).

    Enregistre TOUJOURS cote reelle + mise + date (NOT NULL en base) —
    le suivi argent reel exige un ticket complet. Le libelle et les codes
    marche/selection sont copies du deal pour que le ticket soit autonome."""
    if taken_odd is None or taken_odd <= 1.0 or stake_amount is None or stake_amount <= 0:
        raise ValueError("cote (>1) et mise (>0) obligatoires pour une prise golf")
    connection = connect_db(DatabaseSettings.from_env())
    try:
        with connection.cursor() as cursor:
            enforce_user_bet_limits(cursor, user_id, float(stake_amount))
            if deal_type == "MATCHUP":
                cursor.execute(
                    """
                    SELECT gmd.market_code,
                           pk.player_name || ' vs ' || opp.player_name AS label,
                           gmd.model_probability
                    FROM model.golf_matchup_deals gmd
                    JOIN core.golf_players pk ON pk.golf_player_id = gmd.pick_golf_player_id
                    JOIN core.golf_players opp ON opp.golf_player_id = gmd.opponent_golf_player_id
                    WHERE gmd.golf_matchup_deal_id = %s
                    """,
                    (deal_id,),
                )
                row = cursor.fetchone()
                if not row:
                    raise ValueError(f"deal matchup golf introuvable: {deal_id}")
                market_code, label, model_p = row
                cursor.execute(
                    """
                    INSERT INTO model.user_bet_positions (
                        user_id, bet_kind, golf_matchup_deal_id, market_code, selection_code,
                        selection_label, model_probability, taken_odd, stake_amount, taken_at
                    )
                    VALUES (%s, 'GOLF', %s, %s, 'PICK', %s, %s, %s, %s, now())
                    RETURNING position_id
                    """,
                    (user_id, deal_id, str(market_code), str(label),
                     float(model_p) if model_p is not None else None,
                     float(taken_odd), float(stake_amount)),
                )
                position_id = int(cursor.fetchone()[0])
                apply_bankroll_delta(
                    cursor,
                    user_id=user_id,
                    actor_user_id=user_id,
                    event_type="STAKE_PLACED",
                    amount_delta=-float(stake_amount),
                    reason=f"Prise golf matchup {market_code}",
                    position_id=position_id,
                    metadata={"deal_type": "MATCHUP", "deal_id": deal_id, "taken_odd": taken_odd},
                )
            else:
                cursor.execute(
                    """
                    SELECT gd.market_code, COALESCE(p.player_name, gd.selection_name) AS label,
                           gd.model_probability
                    FROM model.golf_deals gd
                    LEFT JOIN core.golf_players p ON p.golf_player_id = gd.golf_player_id
                    WHERE gd.golf_deal_id = %s
                    """,
                    (deal_id,),
                )
                row = cursor.fetchone()
                if not row:
                    raise ValueError(f"deal golf introuvable: {deal_id}")
                market_code, label, model_p = row
                cursor.execute(
                    """
                    INSERT INTO model.user_bet_positions (
                        user_id, bet_kind, golf_deal_id, market_code, selection_code,
                        selection_label, model_probability, taken_odd, stake_amount, taken_at
                    )
                    VALUES (%s, 'GOLF', %s, %s, 'PICK', %s, %s, %s, %s, now())
                    RETURNING position_id
                    """,
                    (user_id, deal_id, str(market_code), str(label),
                     float(model_p) if model_p is not None else None,
                     float(taken_odd), float(stake_amount)),
                )
                position_id = int(cursor.fetchone()[0])
                apply_bankroll_delta(
                    cursor,
                    user_id=user_id,
                    actor_user_id=user_id,
                    event_type="STAKE_PLACED",
                    amount_delta=-float(stake_amount),
                    reason=f"Prise golf outright {market_code}",
                    position_id=position_id,
                    metadata={"deal_type": "OUTRIGHT", "deal_id": deal_id, "taken_odd": taken_odd},
                )
        connection.commit()
    finally:
        connection.close()


def cashout_position_ticket(position_id: int, cashout_amount: float,
                            user: "UserContext | None" = None) -> None:
    """CASHOUT d'une position en cours de match : le client a coupe le pari
    chez son bookmaker et recupere `cashout_amount`. La position sort du
    reglement par resultat (P&L = cashout - mise) et la bankroll est creditee
    immediatement (evenement CASHOUT au ledger). Refuse si la position est
    deja reglee, deja cashout, supprimee, ou n'appartient pas au client."""
    if cashout_amount is None or float(cashout_amount) < 0:
        raise ValueError("montant de cashout invalide (>= 0 requis)")
    connection = connect_db(DatabaseSettings.from_env())
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT user_id, stake_amount, selection_label, cashed_out_at, deleted_at
                FROM model.user_bet_positions
                WHERE position_id = %s
                FOR UPDATE
                """,
                (position_id,),
            )
            row = cursor.fetchone()
            if not row:
                raise ValueError("position introuvable")
            owner_id, stake, label, cashed_at, deleted_at = row
            if deleted_at is not None:
                raise ValueError("position supprimee")
            if cashed_at is not None:
                raise ValueError("position deja cashout")
            if user is not None and not has_permission(user, "BACK_VIEW_ANY") \
                    and int(owner_id) != int(user.user_id):
                raise ValueError("cette position ne vous appartient pas")
            cursor.execute(
                """
                SELECT 1 FROM model.user_bankroll_events
                WHERE position_id = %s AND event_type = ANY(%s)
                """,
                (position_id, list(_SETTLEMENT_EVENT_TYPES)),
            )
            if cursor.fetchone():
                raise ValueError("position deja reglee — cashout impossible")
            cursor.execute(
                """
                UPDATE model.user_bet_positions
                SET cashout_amount = %s, cashed_out_at = now()
                WHERE position_id = %s
                """,
                (round(float(cashout_amount), 2), position_id),
            )
            actor_id = int(user.user_id) if user is not None else int(owner_id)
            pnl = round(float(cashout_amount) - float(stake), 2)
            apply_bankroll_delta(
                cursor, int(owner_id), actor_id, "CASHOUT",
                round(float(cashout_amount), 2),
                f"Cashout {pnl:+.2f}$ - {str(label or '')[:80]}",
                position_id=int(position_id),
                metadata={"stake": float(stake), "pnl": pnl},
            )
        connection.commit()
    finally:
        connection.close()


def delete_position_ticket(position_id: int, user: UserContext | None = None) -> None:
    """Supprime UN ticket de prise (un pari peut etre pris plusieurs fois).

    Recale l'annotation : si c'etait le dernier ticket, la reco redevient
    "non prise" ; sinon on garde la derniere cote/mise connue.
    """
    connection = connect_db(DatabaseSettings.from_env())
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT annotation_id, user_id, stake_amount, bet_kind
                FROM model.user_bet_positions
                WHERE position_id = %s
                  AND deleted_at IS NULL
                  AND (%s OR user_id = %s)
                """,
                (position_id, bool(user and user.is_admin), int(user.user_id if user else 1)),
            )
            row = cursor.fetchone()
            if not row:
                return
            annotation_id = row[0]
            owner_user_id = int(row[1])
            stake_amount = float(row[2] or 0)
            bet_kind = str(row[3] or "")
            cursor.execute(
                """
                UPDATE model.user_bet_positions
                SET deleted_at = now(), deleted_by = %s, delete_reason = 'ticket_deleted'
                WHERE position_id = %s AND deleted_at IS NULL
                """,
                (int(user.user_id if user else owner_user_id), position_id),
            )
            apply_bankroll_delta(
                cursor,
                user_id=owner_user_id,
                actor_user_id=int(user.user_id if user else owner_user_id),
                event_type="STAKE_VOIDED",
                amount_delta=stake_amount,
                reason=f"Suppression ticket {bet_kind}",
                position_id=position_id,
                metadata={"bet_kind": bet_kind},
            )
            if annotation_id is None:
                connection.commit()
                return
            # Reste-t-il des tickets sur cette annotation ?
            cursor.execute(
                "SELECT taken_odd, stake_amount FROM model.user_bet_positions "
                "WHERE annotation_id = %s AND user_id = %s AND deleted_at IS NULL "
                "ORDER BY created_at DESC LIMIT 1",
                (annotation_id, owner_user_id),
            )
            remaining = cursor.fetchone()
            if remaining is None:
                cursor.execute(
                    """
                    UPDATE model.user_bet_annotations
                    SET taken = false, taken_odd = NULL, stake_amount = NULL,
                        taken_at = NULL, updated_at = now()
                    WHERE annotation_id = %s
                    """,
                    (annotation_id,),
                )
            else:
                cursor.execute(
                    """
                    UPDATE model.user_bet_annotations
                    SET taken_odd = %s,
                        stake_amount = (
                            SELECT SUM(stake_amount) FROM model.user_bet_positions
                            WHERE annotation_id = %s AND deleted_at IS NULL
                        ),
                        updated_at = now()
                    WHERE annotation_id = %s
                    """,
                    (remaining[0], annotation_id, annotation_id),
                )
        connection.commit()
    finally:
        connection.close()


def save_parlay(parlay_json: str, combined_odd: float | None, stake_amount: float | None, user_id: int = 1) -> None:
    """Enregistre un combine pris. parlay_json = liste de jambes
    [{"fixture_id","market_code","selection_code","odd","label"}, ...]."""
    try:
        legs = json.loads(parlay_json)
    except (TypeError, ValueError):
        return
    if not isinstance(legs, list) or len(legs) < 2:
        return
    if combined_odd is None or combined_odd <= 1.0:
        return
    stake = stake_amount if stake_amount is not None and stake_amount > 0 else 1.0

    connection = connect_db(DatabaseSettings.from_env())
    try:
        with connection.cursor() as cursor:
            enforce_user_bet_limits(cursor, user_id, stake)
            label = " + ".join(str(l.get("label") or "?")[:24] for l in legs)
            cursor.execute(
                """
                INSERT INTO model.parlay_tickets (
                    user_id, combined_odd, stake_amount, leg_count, source, label
                )
                VALUES (%s, %s, %s, %s, 'STRATEGIE', %s)
                RETURNING parlay_id
                """,
                (user_id, combined_odd, stake, len(legs), label[:200]),
            )
            parlay_id = int(cursor.fetchone()[0])
            apply_bankroll_delta(
                cursor,
                user_id=user_id,
                actor_user_id=user_id,
                event_type="STAKE_PLACED",
                amount_delta=-float(stake),
                reason="Prise combine",
                parlay_id=parlay_id,
                metadata={"leg_count": len(legs), "combined_odd": combined_odd},
            )
            for leg in legs:
                fixture_id = leg.get("fixture_id")
                cursor.execute(
                    """
                    INSERT INTO model.parlay_legs (
                        parlay_id, fixture_id, market_code, selection_code, taken_odd, label
                    )
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        parlay_id,
                        int(fixture_id) if fixture_id else None,
                        str(leg.get("market_code") or "1X2"),
                        str(leg.get("selection_code") or "-"),
                        _to_float(leg.get("odd")),
                        str(leg.get("label") or "")[:120] or None,
                    ),
                )
        connection.commit()
    finally:
        connection.close()


def delete_parlay(parlay_id: int, user: UserContext | None = None) -> None:
    connection = connect_db(DatabaseSettings.from_env())
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT user_id, stake_amount
                FROM model.parlay_tickets
                WHERE parlay_id = %s
                  AND deleted_at IS NULL
                  AND (%s OR user_id = %s)
                """,
                (parlay_id, bool(user and user.is_admin), int(user.user_id if user else 1)),
            )
            row = cursor.fetchone()
            if not row:
                return
            owner_user_id = int(row[0])
            stake_amount = float(row[1] or 0)
            cursor.execute(
                """
                UPDATE model.parlay_tickets
                SET deleted_at = now(), deleted_by = %s, delete_reason = 'parlay_deleted'
                WHERE parlay_id = %s AND deleted_at IS NULL
                """,
                (int(user.user_id if user else owner_user_id), parlay_id),
            )
            apply_bankroll_delta(
                cursor,
                user_id=owner_user_id,
                actor_user_id=int(user.user_id if user else owner_user_id),
                event_type="STAKE_VOIDED",
                amount_delta=stake_amount,
                reason="Suppression combine",
                parlay_id=parlay_id,
                metadata={"parlay_id": parlay_id},
            )
        connection.commit()
    finally:
        connection.close()


def ensure_user_bankroll(user_id: int, default_amount: float = 1000.0) -> dict[str, Any]:
    connection = connect_db(DatabaseSettings.from_env())
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO model.user_bankrolls (user_id, current_amount)
                VALUES (%s, %s)
                ON CONFLICT (user_id) WHERE status_code = 'ACTIVE'
                DO NOTHING
                """,
                (user_id, default_amount),
            )
            cursor.execute(
                """
                SELECT bankroll_id, user_id, currency_code, current_amount, updated_at
                FROM model.user_bankrolls
                WHERE user_id = %s AND status_code = 'ACTIVE'
                ORDER BY bankroll_id DESC
                LIMIT 1
                """,
                (user_id,),
            )
            row = cursor.fetchone()
        connection.commit()
    finally:
        connection.close()
    return {
        "bankroll_id": int(row[0]),
        "user_id": int(row[1]),
        "currency_code": str(row[2]),
        "current_amount": float(row[3]),
        "updated_at": row[4],
    }


def load_strategy_bankroll_context(
    user_id: int,
    sport: str,
    requested_amount: float | None = None,
    lock_requested: bool = False,
) -> dict[str, Any]:
    """Allocation strategie: une bankroll reelle, deux enveloppes de risque.

    La bankroll courante est deja debitee par les prises. Pour eviter que foot
    et golf recommandent chacun 100% du cash restant, on reconstruit le capital
    gere (cash + mises ouvertes) puis on reserve 50/50 par sport.
    """
    sport = GOLF_SPORT_PARAM if sport == GOLF_SPORT_PARAM else "football"
    bankroll = ensure_user_bankroll(user_id)
    available_cash = float(bankroll["current_amount"])
    football_open = 0.0
    golf_open = 0.0
    parlay_open = 0.0
    connection = connect_db(DatabaseSettings.from_env())
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    COALESCE(SUM(stake_amount) FILTER (WHERE bet_kind = 'GOLF'), 0) AS golf_open,
                    COALESCE(SUM(stake_amount) FILTER (WHERE bet_kind <> 'GOLF'), 0) AS football_open
                FROM model.user_bet_positions p
                WHERE p.user_id = %s
                  AND p.deleted_at IS NULL
                  AND p.cashed_out_at IS NULL
                  AND NOT EXISTS (
                      SELECT 1
                      FROM model.user_bankroll_events e
                      WHERE e.position_id = p.position_id
                        AND e.event_type = ANY(%s)
                  )
                """,
                (user_id, list(_SETTLEMENT_EVENT_TYPES)),
            )
            row = cursor.fetchone()
            if row:
                golf_open = float(row[0] or 0)
                football_open = float(row[1] or 0)
            cursor.execute(
                """
                SELECT COALESCE(SUM(pt.stake_amount), 0)
                FROM model.parlay_tickets pt
                WHERE pt.user_id = %s
                  AND pt.deleted_at IS NULL
                  AND pt.result_code IS NULL
                  AND NOT EXISTS (
                      SELECT 1
                      FROM model.user_bankroll_events e
                      WHERE e.parlay_id = pt.parlay_id
                        AND e.event_type = ANY(%s)
                  )
                """,
                (user_id, list(_SETTLEMENT_EVENT_TYPES)),
            )
            parlay_open = float((cursor.fetchone() or [0])[0] or 0)
    finally:
        connection.close()

    football_open += parlay_open
    managed_bankroll = available_cash + football_open + golf_open
    sport_share = float(SPORT_ALLOCATION_DEFAULTS[sport])
    sport_open = golf_open if sport == GOLF_SPORT_PARAM else football_open
    sport_cap = managed_bankroll * sport_share
    sport_remaining = max(0.0, sport_cap - sport_open)
    auto_budget = max(0.0, min(available_cash, sport_remaining))
    requested = float(requested_amount) if requested_amount is not None else auto_budget
    if lock_requested and requested_amount is not None:
        # Snapshot de strategie: apres une prise, les mises restantes ne
        # bougent pas tant que l'utilisateur ne clique pas sur Recalculer.
        effective = max(0.0, min(requested, managed_bankroll))
    else:
        effective = max(0.0, min(requested, auto_budget))
    return {
        "sport": sport,
        "sport_label": "Golf" if sport == GOLF_SPORT_PARAM else "Football",
        "available_cash": round(available_cash, 2),
        "managed_bankroll": round(managed_bankroll, 2),
        "football_open": round(football_open, 2),
        "golf_open": round(golf_open, 2),
        "parlay_open": round(parlay_open, 2),
        "total_open": round(football_open + golf_open, 2),
        "sport_share": sport_share,
        "sport_cap": round(sport_cap, 2),
        "sport_open": round(sport_open, 2),
        "sport_remaining": round(sport_remaining, 2),
        "requested_bankroll": round(requested, 2),
        "strategy_bankroll": round(effective, 2),
        "budget_locked": bool(lock_requested and requested_amount is not None),
    }


def render_strategy_bankroll_context(context: dict[str, Any] | None) -> str:
    if not context:
        return ""
    capped = float(context["requested_bankroll"]) > float(context["strategy_bankroll"]) + 0.009
    locked = bool(context.get("budget_locked"))
    cap_note = (
        "<p class='client-hint'><strong>Budget plafonne automatiquement :</strong> "
        "la demande depasse l'enveloppe disponible de ce sport apres les prises deja ouvertes.</p>"
        if capped and not locked else ""
    )
    lock_note = (
        "<p class='client-hint'><strong>Plan verrouille :</strong> les mises restent calculees "
        "sur ce budget de strategie. Elles ne changeront qu'apres un clic sur Recalculer.</p>"
        if locked else ""
    )
    return (
        "<div class='verdict-strip bankroll-allocation'>"
        f"<div class='cell'><div class='v'>{float(context['managed_bankroll']):.0f}$</div><div class='l'>Capital gere</div></div>"
        f"<div class='cell'><div class='v'>{float(context['available_cash']):.0f}$</div><div class='l'>Cash disponible</div></div>"
        f"<div class='cell'><div class='v'>50/50</div><div class='l'>Allocation foot / golf</div></div>"
        f"<div class='cell'><div class='v'>{float(context['football_open']):.0f}$</div><div class='l'>Expose football</div></div>"
        f"<div class='cell'><div class='v'>{float(context['golf_open']):.0f}$</div><div class='l'>Expose golf</div></div>"
        f"<div class='cell'><div class='v'>{float(context['strategy_bankroll']):.0f}$</div><div class='l'>Budget {escape(str(context['sport_label']))}</div></div>"
        "</div>"
        + cap_note
        + lock_note
    )


def set_user_bankroll(user: UserContext, amount: float, reason: str = "") -> None:
    require_permission(user, "BANKROLL_MANAGE_OWN")
    if amount < 0:
        raise ValueError("bankroll invalide")
    current = ensure_user_bankroll(user.user_id)
    minimum_bankroll = 0.0
    delta = round(float(amount) - float(current["current_amount"]), 2)
    connection = connect_db(DatabaseSettings.from_env())
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT COALESCE(open_total_stake, 0)
                FROM reporting.v_user_account_snapshot
                WHERE user_id = %s
                """,
                (user.user_id,),
            )
            minimum_bankroll = float((cursor.fetchone() or [0])[0] or 0)
            if float(amount) + 0.009 < minimum_bankroll:
                raise ValueError(
                    f"bankroll insuffisante : minimum {minimum_bankroll:.2f}$ pour couvrir les mises ouvertes"
                )
            cursor.execute(
                """
                UPDATE model.user_bankrolls
                SET current_amount = %s, updated_at = now()
                WHERE bankroll_id = %s
                """,
                (round(float(amount), 2), current["bankroll_id"]),
            )
            cursor.execute(
                """
                INSERT INTO model.user_bankroll_events (
                    bankroll_id, user_id, actor_user_id, event_type,
                    amount_delta, resulting_amount, reason
                )
                VALUES (%s, %s, %s, 'BANKROLL_SET', %s, %s, %s)
                """,
                (
                    current["bankroll_id"], user.user_id, user.user_id,
                    delta, round(float(amount), 2), reason.strip() or None,
                ),
            )
            cursor.execute(
                """
                INSERT INTO app_auth.audit_log (actor_user_id, target_user_id, action_code, entity_type, entity_id, metadata)
                VALUES (%s, %s, 'BANKROLL_SET', 'BANKROLL', %s, %s::jsonb)
                """,
                (
                    user.user_id, user.user_id, str(current["bankroll_id"]),
                    json.dumps(
                        {
                            "amount": round(float(amount), 2),
                            "delta": delta,
                            "minimum_bankroll": round(minimum_bankroll, 2),
                        }
                    ),
                ),
            )
        connection.commit()
    finally:
        connection.close()


def load_bankroll_account(user: UserContext) -> dict[str, Any]:
    # Rattrape d'abord les reglements non credites (paris finis -> ledger).
    try:
        settle_conn = connect_db(DatabaseSettings.from_env())
        try:
            reconcile_bankroll_settlements(settle_conn)
        finally:
            settle_conn.close()
    except Exception:
        pass
    bankroll = ensure_user_bankroll(user.user_id)
    connection = connect_db(DatabaseSettings.from_env())
    try:
        with connection.cursor() as cursor:
            snapshot = fetch_dicts(
                cursor,
                """
                SELECT bankroll_amount, currency_code, open_total_stake
                FROM reporting.v_user_account_snapshot
                WHERE user_id = %s
                """,
                (user.user_id,),
            )
            events = fetch_dicts(
                cursor,
                """
                SELECT event_type, amount_delta, resulting_amount, reason, created_at
                FROM model.user_bankroll_events
                WHERE user_id = %s
                ORDER BY created_at DESC, event_id DESC
                LIMIT 50
                """,
                (user.user_id,),
            )
    finally:
        connection.close()
    snapshot_row = (snapshot[0] if snapshot else {}) if "snapshot" in locals() else {}
    bankroll["current_amount"] = float(snapshot_row.get("bankroll_amount") or bankroll["current_amount"])
    bankroll["currency_code"] = str(snapshot_row.get("currency_code") or bankroll["currency_code"])
    bankroll["events"] = events
    bankroll["open_stake"] = float(snapshot_row.get("open_total_stake") or 0)
    return bankroll


def render_bankroll_account_page(user: UserContext, report: str = "") -> str:
    data = load_bankroll_account(user)
    events = data.get("events") or []
    can_edit_bankroll = can_manage_bankroll(user)
    manage_section = (
        (
            f"<form method=\"post\" action=\"/bankroll/account\" class=\"filterbar\">"
            f"<input type=\"hidden\" name=\"action\" value=\"set_bankroll\">"
            f"<label>Nouveau montant<input type=\"number\" name=\"amount\" min=\"0\" step=\"0.01\" value=\"{float(data['current_amount']):.2f}\" required></label>"
            f"<label>Raison<input type=\"text\" name=\"reason\" placeholder=\"ajustement, depot, retrait...\"></label>"
            f"<div class=\"filter-actions\"><button type=\"submit\" class=\"button-primary\">Enregistrer</button></div>"
            f"</form>"
        )
        if can_edit_bankroll else
        "<p class='client-hint'><strong>Lecture seule :</strong> les clients consultent leur ledger, mais la modification directe de bankroll reste reservee a l'administration.</p>"
    )
    event_rows = "".join(
        "<tr>"
        f"<td>{escape(str(e['event_type']))}</td>"
        f"<td class='num'>{float(e['amount_delta'] or 0):+.2f}$</td>"
        f"<td class='num'><strong>{float(e['resulting_amount'] or 0):.2f}$</strong></td>"
        f"<td>{escape(str(e.get('reason') or '-'))}</td>"
        f"<td class='muted'>{format_timestamp(e.get('created_at'))}</td>"
        "</tr>"
        for e in events
    ) or "<tr><td colspan='5' class='empty'>Aucun mouvement bankroll encore.</td></tr>"
    report_html = f"<div class='report'><strong>{escape(report)}</strong></div>" if report else ""
    return f"""<!DOCTYPE html>
<html lang="fr">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Bankroll - BPREDICTION</title>
  <style>{BASE_CSS}</style>
</head>
<body>
  <main class="shell">
    {render_sport_nav("football", "bankroll", user)}
    <header class="matchline">
      <div>
        <h1>Bankroll</h1>
        <p class="meta">Capital personnel utilise par les strategies et le back reel.</p>
      </div>
      <div class="hero-visual football" aria-hidden="true"><span class="ball"></span></div>
    </header>
    {report_html}
    <div class="verdict-strip">
      <div class="cell"><div class="v">{float(data['current_amount']):.2f}$</div><div class="l">Bankroll active</div></div>
      <div class="cell"><div class="v">{float(data['open_stake']):.2f}$</div><div class="l">Mises enregistrees</div></div>
      <div class="cell"><div class="v">{escape(str(data['currency_code']))}</div><div class="l">Devise</div></div>
    </div>
    <section class="section">
      <div class="section-head"><span class="no">01</span><h2>Modifier la bankroll</h2><span class="note">Chaque changement est historise.</span></div>
      {manage_section}
    </section>
    <section class="section">
      <div class="section-head"><span class="no">02</span><h2>Historique bankroll</h2><span class="note">Ledger personnel immuable.</span></div>
      <div class="table-wrap"><table><thead><tr><th>Type</th><th class="num">Variation</th><th class="num">Solde</th><th>Raison</th><th>Date</th></tr></thead><tbody>{event_rows}</tbody></table></div>
    </section>
    {render_product_footer("football")}
  </main>
{dev_reload_script()}
</body>
</html>"""


def load_admin_dashboard(search: str = "") -> dict[str, Any]:
    like = f"%{search.strip()}%"
    connection = connect_db(DatabaseSettings.from_env())
    try:
        with connection.cursor() as cursor:
            users = fetch_dicts(
                cursor,
                """
                SELECT
                    user_id,
                    email,
                    display_name,
                    role_code,
                    status_code,
                    bankroll_amount AS bankroll,
                    total_positions AS positions,
                    total_stake_amount AS stake_total,
                    open_total_stake,
                    active_session_count,
                    last_seen_at,
                    last_position_at,
                    company_name,
                    segment_code,
                    tags_csv,
                    requires_manual_review,
                    note_count
                FROM reporting.v_user_admin_profile
                WHERE %s = ''
                   OR email ILIKE %s
                   OR display_name ILIKE %s
                   OR COALESCE(company_name, '') ILIKE %s
                   OR COALESCE(tags_csv, '') ILIKE %s
                ORDER BY created_at DESC
                LIMIT 200
                """,
                (search.strip(), like, like, like, like),
            )
            audits = fetch_dicts(
                cursor,
                """
                SELECT a.action_code, a.created_at, u.email AS actor_email, tu.email AS target_email
                FROM app_auth.audit_log a
                LEFT JOIN app_auth.users u ON u.user_id = a.actor_user_id
                LEFT JOIN app_auth.users tu ON tu.user_id = a.target_user_id
                ORDER BY a.created_at DESC
                LIMIT 30
                """,
            )
            pipeline = fetch_dicts(
                cursor,
                """
                SELECT
                    provider_code,
                    endpoint_code,
                    provider_object_type,
                    provider_object_id,
                    normalization_status,
                    records_written,
                    captured_at,
                    normalized_at,
                    error_message
                FROM reporting.v_provider_payload_pipeline
                ORDER BY captured_at DESC
                LIMIT 20
                """,
            )
    finally:
        connection.close()
    return {"users": users, "audits": audits, "pipeline": pipeline, "search": search}


def signup_user_account(
    email: str,
    display_name: str,
    password: str,
    role_code: str,
    admin_key: str,
    environ: dict[str, Any] | None = None,
) -> int:
    """Creation de compte PUBLIQUE (/signup).

    Differences volontaires avec create_user_account (flux admin) :
    - INSERT strict : un email deja utilise est REFUSE (pas d'upsert, sinon
      n'importe qui pourrait ecraser le mot de passe d'un compte existant) ;
    - le role ADMIN exige la cle secrete ADMIN_SIGNUP_KEY (.env), comparee en
      temps constant (hmac.compare_digest) — sans cle valide : refus.
    """
    email = email.strip().lower()
    display_name = display_name.strip() or email
    role_code = (role_code or "CLIENT").strip().upper()
    if role_code not in ("CLIENT", "ADMIN"):
        raise ValueError("role invalide")
    if "@" not in email or len(email) < 4:
        raise ValueError("email invalide")
    if len(password) < _password_min_length():
        raise ValueError(f"mot de passe trop court ({_password_min_length()} caracteres minimum)")
    if role_code == "ADMIN":
        expected = (get_env("ADMIN_SIGNUP_KEY", "") or "").strip()
        provided = (admin_key or "").strip()
        if not expected or not hmac.compare_digest(provided.encode(), expected.encode()):
            raise ValueError("cle administrateur invalide")
    password_hash = hash_password(password)
    connection = connect_db(DatabaseSettings.from_env())
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT 1 FROM app_auth.users WHERE lower(email) = %s AND deleted_at IS NULL",
                (email,),
            )
            if cursor.fetchone():
                raise ValueError("cet email est deja utilise")
            cursor.execute(
                """
                INSERT INTO app_auth.users (email, display_name, password_hash, role_code, status_code)
                VALUES (%s, %s, %s, %s, 'ACTIVE')
                RETURNING user_id
                """,
                (email, display_name, password_hash, role_code),
            )
            user_id = int(cursor.fetchone()[0])
            _ensure_user_bankroll_cursor(cursor, user_id)
            cursor.execute(
                """
                INSERT INTO app_auth.audit_log (
                    actor_user_id, target_user_id, action_code, entity_type,
                    entity_id, route, metadata
                )
                VALUES (%s, %s, 'USER_SIGNUP', 'USER', %s, '/signup', %s::jsonb)
                """,
                (user_id, user_id, str(user_id),
                 json.dumps({"role": role_code, "email": email})),
            )
        connection.commit()
        return user_id
    finally:
        connection.close()


def create_user_account(
    actor: UserContext,
    email: str,
    display_name: str,
    password: str,
    role_code: str = "CLIENT",
) -> int:
    email = email.strip().lower()
    display_name = display_name.strip() or email
    role_code = role_code.strip().upper()
    if role_code not in ("CLIENT", "ADMIN"):
        raise ValueError("role invalide")
    if len(password) < _password_min_length():
        raise ValueError(f"mot de passe trop court ({_password_min_length()} caracteres minimum)")
    password_hash = hash_password(password)
    connection = connect_db(DatabaseSettings.from_env())
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO app_auth.users (email, display_name, password_hash, role_code, status_code)
                VALUES (%s, %s, %s, %s, 'ACTIVE')
                ON CONFLICT (lower(email)) WHERE deleted_at IS NULL
                DO UPDATE SET
                    display_name = EXCLUDED.display_name,
                    password_hash = EXCLUDED.password_hash,
                    role_code = EXCLUDED.role_code,
                    status_code = 'ACTIVE',
                    updated_at = now(),
                    deleted_at = NULL
                RETURNING user_id
                """,
                (email, display_name, password_hash, role_code),
            )
            user_id = int(cursor.fetchone()[0])
            _ensure_user_bankroll_cursor(cursor, user_id)
            cursor.execute(
                """
                INSERT INTO app_auth.audit_log (
                    actor_user_id, target_user_id, action_code, entity_type, entity_id, metadata
                )
                VALUES (%s, %s, 'USER_UPSERT', 'USER', %s, %s::jsonb)
                """,
                (
                    actor.user_id, user_id, str(user_id),
                    json.dumps({"email": email, "role_code": role_code}),
                ),
            )
        connection.commit()
    finally:
        connection.close()
    return user_id


def _form_checkbox(form: dict[str, list[str]], name: str) -> bool:
    return (form.get(name, [""])[0] or "").strip().lower() in {"1", "true", "on", "yes"}


def _clean_client_tags(raw_value: str) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for part in raw_value.replace("\n", ",").split(","):
        label = re.sub(r"\s+", " ", part).strip()
        if not label:
            continue
        key = label.casefold()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(label[:60])
    return cleaned


def _replace_client_tags(cursor, actor_user_id: int, target_user_id: int, tags_csv: str) -> None:
    cursor.execute("DELETE FROM app_auth.user_tag_links WHERE user_id = %s", (target_user_id,))
    for label in _clean_client_tags(tags_csv):
        tag_code = re.sub(r"[^A-Z0-9]+", "_", label.upper()).strip("_")[:50] or "CLIENT"
        cursor.execute(
            """
            INSERT INTO app_auth.client_tags (tag_code, display_label)
            VALUES (%s, %s)
            ON CONFLICT (tag_code) DO UPDATE
            SET display_label = EXCLUDED.display_label
            RETURNING tag_id
            """,
            (tag_code, label),
        )
        tag_id = int(cursor.fetchone()[0])
        cursor.execute(
            """
            INSERT INTO app_auth.user_tag_links (user_id, tag_id, applied_by_user_id)
            VALUES (%s, %s, %s)
            ON CONFLICT (user_id, tag_id) DO UPDATE
            SET applied_by_user_id = EXCLUDED.applied_by_user_id,
                applied_at = now()
            """,
            (target_user_id, tag_id, actor_user_id),
        )


def save_admin_client_profile(actor: UserContext, target_user_id: int, form: dict[str, list[str]]) -> None:
    display_name = (form.get("display_name", [""])[0] or "").strip()
    status_code = (form.get("status_code", ["ACTIVE"])[0] or "ACTIVE").strip().upper()
    if status_code not in {"ACTIVE", "DISABLED", "PENDING"}:
        raise ValueError("statut client invalide")

    segment_code = (form.get("segment_code", ["STANDARD"])[0] or "STANDARD").strip().upper()
    if segment_code not in {"STANDARD", "VIP", "PARTNER", "INTERNAL"}:
        raise ValueError("segment invalide")

    onboarding_status = (form.get("onboarding_status", ["ACTIVE"])[0] or "ACTIVE").strip().upper()
    if onboarding_status not in {"LEAD", "ACTIVE", "PAUSED", "CHURNED"}:
        raise ValueError("onboarding invalide")

    odds_format = (form.get("odds_format", ["DECIMAL"])[0] or "DECIMAL").strip().upper()
    if odds_format not in {"DECIMAL", "AMERICAN", "FRACTIONAL"}:
        raise ValueError("format de cote invalide")

    connection = connect_db(DatabaseSettings.from_env())
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE app_auth.users
                SET display_name = %s,
                    status_code = %s,
                    updated_at = now()
                WHERE user_id = %s
                  AND deleted_at IS NULL
                RETURNING user_id
                """,
                (display_name or f"Client {target_user_id}", status_code, target_user_id),
            )
            if not cursor.fetchone():
                raise ValueError("client introuvable")

            cursor.execute(
                """
                INSERT INTO app_auth.user_profiles (
                    user_id, company_name, phone, country_code, timezone_name,
                    segment_code, onboarding_status, source_channel, external_ref,
                    updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (user_id) DO UPDATE
                SET company_name = EXCLUDED.company_name,
                    phone = EXCLUDED.phone,
                    country_code = EXCLUDED.country_code,
                    timezone_name = EXCLUDED.timezone_name,
                    segment_code = EXCLUDED.segment_code,
                    onboarding_status = EXCLUDED.onboarding_status,
                    source_channel = EXCLUDED.source_channel,
                    external_ref = EXCLUDED.external_ref,
                    updated_at = now()
                """,
                (
                    target_user_id,
                    (form.get("company_name", [""])[0] or "").strip() or None,
                    (form.get("phone", [""])[0] or "").strip() or None,
                    (form.get("country_code", [""])[0] or "").strip().upper() or None,
                    (form.get("timezone_name", ["America/Toronto"])[0] or "America/Toronto").strip(),
                    segment_code,
                    onboarding_status,
                    (form.get("source_channel", [""])[0] or "").strip() or None,
                    (form.get("external_ref", [""])[0] or "").strip() or None,
                ),
            )

            cursor.execute(
                """
                INSERT INTO app_auth.user_preferences (
                    user_id, preferred_sport, preferred_language, preferred_currency,
                    odds_format, timezone_name, alert_opt_in, marketing_opt_in,
                    automation_opt_in, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (user_id) DO UPDATE
                SET preferred_sport = EXCLUDED.preferred_sport,
                    preferred_language = EXCLUDED.preferred_language,
                    preferred_currency = EXCLUDED.preferred_currency,
                    odds_format = EXCLUDED.odds_format,
                    timezone_name = EXCLUDED.timezone_name,
                    alert_opt_in = EXCLUDED.alert_opt_in,
                    marketing_opt_in = EXCLUDED.marketing_opt_in,
                    automation_opt_in = EXCLUDED.automation_opt_in,
                    updated_at = now()
                """,
                (
                    target_user_id,
                    (form.get("preferred_sport", ["football"])[0] or "football").strip().lower(),
                    (form.get("preferred_language", ["fr"])[0] or "fr").strip().lower(),
                    (form.get("preferred_currency", ["CAD"])[0] or "CAD").strip().upper(),
                    odds_format,
                    (form.get("preference_timezone_name", ["America/Toronto"])[0] or "America/Toronto").strip(),
                    _form_checkbox(form, "alert_opt_in"),
                    _form_checkbox(form, "marketing_opt_in"),
                    _form_checkbox(form, "automation_opt_in"),
                ),
            )

            cursor.execute(
                """
                INSERT INTO app_auth.user_limits (
                    user_id, max_single_bet, max_daily_stake, max_open_exposure,
                    loss_limit_daily, loss_limit_weekly, requires_manual_review, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (user_id) DO UPDATE
                SET max_single_bet = EXCLUDED.max_single_bet,
                    max_daily_stake = EXCLUDED.max_daily_stake,
                    max_open_exposure = EXCLUDED.max_open_exposure,
                    loss_limit_daily = EXCLUDED.loss_limit_daily,
                    loss_limit_weekly = EXCLUDED.loss_limit_weekly,
                    requires_manual_review = EXCLUDED.requires_manual_review,
                    updated_at = now()
                """,
                (
                    target_user_id,
                    _to_float(form.get("max_single_bet", [""])[0]),
                    _to_float(form.get("max_daily_stake", [""])[0]),
                    _to_float(form.get("max_open_exposure", [""])[0]),
                    _to_float(form.get("loss_limit_daily", [""])[0]),
                    _to_float(form.get("loss_limit_weekly", [""])[0]),
                    _form_checkbox(form, "requires_manual_review"),
                ),
            )

            _replace_client_tags(cursor, actor.user_id, target_user_id, form.get("tags_csv", [""])[0] or "")
            cursor.execute(
                """
                INSERT INTO app_auth.audit_log (
                    actor_user_id, target_user_id, action_code, entity_type, entity_id, metadata
                )
                VALUES (%s, %s, 'CLIENT_PROFILE_UPDATE', 'USER', %s, %s::jsonb)
                """,
                (
                    actor.user_id,
                    target_user_id,
                    str(target_user_id),
                    json.dumps({"status_code": status_code, "segment_code": segment_code}),
                ),
            )
        connection.commit()
    finally:
        connection.close()


def add_admin_client_note(actor: UserContext, target_user_id: int, note_kind: str, note_body: str) -> None:
    clean_kind = (note_kind or "ADMIN").strip().upper()
    if clean_kind not in {"ADMIN", "SUPPORT", "RISK", "CRM"}:
        raise ValueError("type de note invalide")
    clean_body = note_body.strip()
    if not clean_body:
        raise ValueError("note vide")

    connection = connect_db(DatabaseSettings.from_env())
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO app_auth.user_notes (user_id, author_user_id, note_kind, note_body)
                VALUES (%s, %s, %s, %s)
                """,
                (target_user_id, actor.user_id, clean_kind, clean_body[:4000]),
            )
            cursor.execute(
                """
                INSERT INTO app_auth.audit_log (
                    actor_user_id, target_user_id, action_code, entity_type, entity_id, metadata
                )
                VALUES (%s, %s, 'CLIENT_NOTE_ADD', 'USER', %s, %s::jsonb)
                """,
                (
                    actor.user_id,
                    target_user_id,
                    str(target_user_id),
                    json.dumps({"note_kind": clean_kind}),
                ),
            )
        connection.commit()
    finally:
        connection.close()


def load_admin_client_detail(user_id: int) -> dict[str, Any] | None:
    connection = connect_db(DatabaseSettings.from_env())
    try:
        with connection.cursor() as cursor:
            profile_rows = fetch_dicts(
                cursor,
                "SELECT * FROM reporting.v_user_admin_profile WHERE user_id = %s",
                (user_id,),
            )
            if not profile_rows:
                return None
            notes = fetch_dicts(
                cursor,
                """
                SELECT n.note_kind, n.note_body, n.created_at, u.email AS author_email
                FROM app_auth.user_notes n
                LEFT JOIN app_auth.users u ON u.user_id = n.author_user_id
                WHERE n.user_id = %s
                ORDER BY n.created_at DESC, n.note_id DESC
                LIMIT 20
                """,
                (user_id,),
            )
            sessions = fetch_dicts(
                cursor,
                """
                SELECT created_at, last_seen_at, expires_at, revoked_at, user_agent, ip_address
                FROM app_auth.sessions
                WHERE user_id = %s
                ORDER BY created_at DESC
                LIMIT 20
                """,
                (user_id,),
            )
            bankroll_events = fetch_dicts(
                cursor,
                """
                SELECT event_type, amount_delta, resulting_amount, reason, created_at
                FROM model.user_bankroll_events
                WHERE user_id = %s
                ORDER BY created_at DESC, event_id DESC
                LIMIT 20
                """,
                (user_id,),
            )
    finally:
        connection.close()
    return {
        "profile": profile_rows[0],
        "notes": notes,
        "sessions": sessions,
        "bankroll_events": bankroll_events,
    }


def render_admin_dashboard(
    user: UserContext,
    data: dict[str, Any],
    report: str = "",
    error: str = "",
) -> str:
    user_rows = "".join(
        "<tr>"
        f"<td><strong>{escape(str(u['display_name']))}</strong>"
        f"<div class='muted subline'>{escape(str(u['email']))}</div>"
        f"<div class='muted subline'>{escape(str(u.get('company_name') or '-'))} · {escape(str(u.get('tags_csv') or 'sans tag'))}</div></td>"
        f"<td>{escape(str(u['role_code']))}</td>"
        f"<td>{escape(str(u['status_code']))}<div class='muted subline'>{escape(str(u.get('segment_code') or '-'))}</div></td>"
        f"<td class='num'>{float(u.get('bankroll') or 0):.2f}$</td>"
        f"<td class='num'>{int(u.get('positions') or 0)}</td>"
        f"<td class='num'>{float(u.get('stake_total') or 0):.2f}$</td>"
        f"<td class='num'>{float(u.get('open_total_stake') or 0):.2f}$</td>"
        f"<td class='num'>{int(u.get('active_session_count') or 0)}</td>"
        f"<td class='muted'>{format_timestamp(u.get('last_seen_at'))}</td>"
        f"<td class='muted'>{format_timestamp(u.get('last_position_at'))}</td>"
        f"<td>{'Oui' if bool(u.get('requires_manual_review')) else 'Non'}<div class='muted subline'>{int(u.get('note_count') or 0)} note(s)</div></td>"
        f"<td><a class='detail-link' href='/admin/client/{int(u['user_id'])}'>Fiche</a> · <a class='detail-link' href='/back?sport=football&admin_user_id={int(u['user_id'])}'>Back</a></td>"
        "</tr>"
        for u in data.get("users", [])
    ) or "<tr><td colspan='12' class='empty'>Aucun utilisateur.</td></tr>"
    audit_rows = "".join(
        "<tr>"
        f"<td>{escape(str(a['action_code']))}</td>"
        f"<td>{escape(str(a.get('actor_email') or '-'))}</td>"
        f"<td>{escape(str(a.get('target_email') or '-'))}</td>"
        f"<td class='muted'>{format_timestamp(a.get('created_at'))}</td>"
        "</tr>"
        for a in data.get("audits", [])
    ) or "<tr><td colspan='4' class='empty'>Aucun audit.</td></tr>"
    pipeline_rows = "".join(
        "<tr>"
        f"<td>{escape(str(p.get('provider_code') or '-'))}</td>"
        f"<td>{escape(str(p.get('endpoint_code') or '-'))}</td>"
        f"<td>{escape(str(p.get('provider_object_type') or '-'))}</td>"
        f"<td>{escape(str(p.get('normalization_status') or 'PENDING'))}</td>"
        f"<td class='num'>{int(p.get('records_written') or 0)}</td>"
        f"<td class='muted'>{format_timestamp(p.get('captured_at'))}</td>"
        f"<td class='muted'>{escape(str(p.get('error_message') or '-'))[:140]}</td>"
        "</tr>"
        for p in data.get("pipeline", [])
    ) or "<tr><td colspan='7' class='empty'>Aucun payload pipeline recent.</td></tr>"
    report_html = f"<div class='report success'><strong>{escape(report)}</strong></div>" if report else ""
    error_html = f"<div class='report error'><strong>{escape(error)}</strong></div>" if error else ""
    return f"""<!DOCTYPE html>
<html lang="fr">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Admin - BPREDICTION</title>
  <style>{BASE_CSS}</style>
</head>
<body>
  <main class="shell">
    {render_sport_nav("football", "admin", user)}
    <header class="matchline">
      <div>
        <h1>Administration</h1>
        <p class="meta">Supervision clients, bankrolls, positions, pipeline brut et audit.</p>
      </div>
      <div class="hero-visual football" aria-hidden="true"><span class="ball"></span></div>
    </header>
    {report_html}
    {error_html}
    <section class="section">
      <div class="section-head"><span class="no">00</span><h2>Créer un accès</h2><span class="note">Client par défaut, admin uniquement si nécessaire.</span></div>
      <form method="post" action="/admin" class="filterbar">
        <input type="hidden" name="action" value="create_user">
        <label>Email<input name="email" type="email" placeholder="client@exemple.com" required></label>
        <label>Nom<input name="display_name" placeholder="Nom client" required></label>
        <label>Rôle<select name="role_code"><option value="CLIENT">Client</option><option value="ADMIN">Admin</option></select></label>
        <label>Mot de passe<input name="password" type="password" minlength="{_password_min_length()}" required></label>
        <div class="filter-actions"><button type="submit" class="button-primary">Créer / mettre à jour</button></div>
      </form>
    </section>
    <form method="get" action="/admin" class="filterbar">
      <label>Recherche client<input name="q" value="{escape(str(data.get('search') or ''))}" placeholder="nom, email, societe, tag"></label>
      <div class="filter-actions"><button type="submit" class="button-primary">Rechercher</button></div>
    </form>
    <section class="section">
      <div class="section-head"><span class="no">01</span><h2>Clients</h2><span class="note">Vue admin globale.</span></div>
      <div class="table-wrap"><table><thead><tr><th>Utilisateur</th><th>Role</th><th>Statut</th><th class="num">Bankroll</th><th class="num">Positions</th><th class="num">Mises</th><th class="num">Expo ouverte</th><th class="num">Sessions</th><th>Derniere activite</th><th>Derniere prise</th><th>Review</th><th>Detail</th></tr></thead><tbody>{user_rows}</tbody></table></div>
    </section>
    <section class="section">
      <div class="section-head"><span class="no">02</span><h2>Audit recent</h2><span class="note">Dernieres actions sensibles.</span></div>
      <div class="table-wrap"><table><thead><tr><th>Action</th><th>Acteur</th><th>Cible</th><th>Date</th></tr></thead><tbody>{audit_rows}</tbody></table></div>
    </section>
    <section class="section">
      <div class="section-head"><span class="no">03</span><h2>Pipeline brut</h2><span class="note">Payloads API recents et statut de normalisation.</span></div>
      <div class="table-wrap"><table><thead><tr><th>Provider</th><th>Endpoint</th><th>Objet</th><th>Statut</th><th class="num">Ecrits</th><th>Date brute</th><th>Erreur</th></tr></thead><tbody>{pipeline_rows}</tbody></table></div>
    </section>
    {render_product_footer("football")}
  </main>
{dev_reload_script()}
</body>
</html>"""


def render_admin_client_page(
    user: UserContext,
    data: dict[str, Any],
    report: str = "",
    error: str = "",
) -> str:
    profile = data["profile"]
    notes = data.get("notes") or []
    sessions = data.get("sessions") or []
    bankroll_events = data.get("bankroll_events") or []
    tags_csv = str(profile.get("tags_csv") or "")
    report_html = f"<div class='report success'><strong>{escape(report)}</strong></div>" if report else ""
    error_html = f"<div class='report error'><strong>{escape(error)}</strong></div>" if error else ""
    checked = lambda value: " checked" if value else ""
    note_rows = "".join(
        "<tr>"
        f"<td>{escape(str(n.get('note_kind') or '-'))}</td>"
        f"<td>{escape(str(n.get('note_body') or '-'))}</td>"
        f"<td>{escape(str(n.get('author_email') or '-'))}</td>"
        f"<td class='muted'>{format_timestamp(n.get('created_at'))}</td>"
        "</tr>"
        for n in notes
    ) or "<tr><td colspan='4' class='empty'>Aucune note.</td></tr>"
    session_rows = "".join(
        "<tr>"
        f"<td class='muted'>{format_timestamp(s.get('created_at'))}</td>"
        f"<td class='muted'>{format_timestamp(s.get('last_seen_at'))}</td>"
        f"<td class='muted'>{format_timestamp(s.get('expires_at'))}</td>"
        f"<td>{'Active' if s.get('revoked_at') is None else 'Revoquee'}</td>"
        f"<td>{escape(str(s.get('ip_address') or '-'))}</td>"
        "</tr>"
        for s in sessions
    ) or "<tr><td colspan='5' class='empty'>Aucune session.</td></tr>"
    bankroll_rows = "".join(
        "<tr>"
        f"<td>{escape(str(e.get('event_type') or '-'))}</td>"
        f"<td class='num'>{float(e.get('amount_delta') or 0):+.2f}$</td>"
        f"<td class='num'>{float(e.get('resulting_amount') or 0):.2f}$</td>"
        f"<td>{escape(str(e.get('reason') or '-'))}</td>"
        f"<td class='muted'>{format_timestamp(e.get('created_at'))}</td>"
        "</tr>"
        for e in bankroll_events
    ) or "<tr><td colspan='5' class='empty'>Aucun mouvement bankroll.</td></tr>"
    return f"""<!DOCTYPE html>
<html lang="fr">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Client - BPREDICTION</title>
  <style>{BASE_CSS}</style>
</head>
<body>
  <main class="shell">
    {render_sport_nav("football", "admin", user)}
    <header class="matchline">
      <div>
        <h1>{escape(str(profile.get('display_name') or 'Client'))}</h1>
        <p class="meta">{escape(str(profile.get('email') or '-'))} · {escape(str(profile.get('company_name') or 'sans societe'))}</p>
      </div>
      <div class="hero-visual football" aria-hidden="true"><span class="ball"></span></div>
    </header>
    {report_html}
    {error_html}
    <div class="verdict-strip">
      <div class="cell"><div class="v">{float(profile.get('bankroll_amount') or 0):.2f}$</div><div class="l">Bankroll</div></div>
      <div class="cell"><div class="v">{float(profile.get('open_total_stake') or 0):.2f}$</div><div class="l">Expo ouverte</div></div>
      <div class="cell"><div class="v">{int(profile.get('active_session_count') or 0)}</div><div class="l">Sessions actives</div></div>
      <div class="cell"><div class="v">{int(profile.get('note_count') or 0)}</div><div class="l">Notes</div></div>
      <div class="cell"><div class="v">{escape(str(profile.get('segment_code') or '-'))}</div><div class="l">Segment</div></div>
    </div>
    <div class="filter-actions" style="margin:12px 0 18px"><a class="button-primary" href="/admin">Retour admin</a> <a class="button-primary" href="/back?sport=football&admin_user_id={int(profile['user_id'])}">Voir le back client</a></div>
    <section class="section">
      <div class="section-head"><span class="no">01</span><h2>Fiche client</h2><span class="note">Profil, preferences et limites.</span></div>
      <form method="post" action="/admin/client/{int(profile['user_id'])}" class="filterbar">
        <input type="hidden" name="action" value="update_client_profile">
        <label>Nom<input name="display_name" value="{escape(str(profile.get('display_name') or ''))}" required></label>
        <label>Statut<select name="status_code"><option value="ACTIVE"{' selected' if str(profile.get('status_code')) == 'ACTIVE' else ''}>ACTIVE</option><option value="PENDING"{' selected' if str(profile.get('status_code')) == 'PENDING' else ''}>PENDING</option><option value="DISABLED"{' selected' if str(profile.get('status_code')) == 'DISABLED' else ''}>DISABLED</option></select></label>
        <label>Societe<input name="company_name" value="{escape(str(profile.get('company_name') or ''))}"></label>
        <label>Telephone<input name="phone" value="{escape(str(profile.get('phone') or ''))}"></label>
        <label>Pays<input name="country_code" value="{escape(str(profile.get('country_code') or ''))}"></label>
        <label>Timezone profil<input name="timezone_name" value="{escape(str(profile.get('timezone_name') or 'America/Toronto'))}"></label>
        <label>Segment<select name="segment_code"><option value="STANDARD"{' selected' if str(profile.get('segment_code')) == 'STANDARD' else ''}>STANDARD</option><option value="VIP"{' selected' if str(profile.get('segment_code')) == 'VIP' else ''}>VIP</option><option value="PARTNER"{' selected' if str(profile.get('segment_code')) == 'PARTNER' else ''}>PARTNER</option><option value="INTERNAL"{' selected' if str(profile.get('segment_code')) == 'INTERNAL' else ''}>INTERNAL</option></select></label>
        <label>Onboarding<select name="onboarding_status"><option value="LEAD"{' selected' if str(profile.get('onboarding_status')) == 'LEAD' else ''}>LEAD</option><option value="ACTIVE"{' selected' if str(profile.get('onboarding_status')) == 'ACTIVE' else ''}>ACTIVE</option><option value="PAUSED"{' selected' if str(profile.get('onboarding_status')) == 'PAUSED' else ''}>PAUSED</option><option value="CHURNED"{' selected' if str(profile.get('onboarding_status')) == 'CHURNED' else ''}>CHURNED</option></select></label>
        <label>Source<input name="source_channel" value="{escape(str(profile.get('source_channel') or ''))}"></label>
        <label>Reference externe<input name="external_ref" value="{escape(str(profile.get('external_ref') or ''))}"></label>
        <label>Sport prefere<input name="preferred_sport" value="{escape(str(profile.get('preferred_sport') or 'football'))}"></label>
        <label>Langue<input name="preferred_language" value="{escape(str(profile.get('preferred_language') or 'fr'))}"></label>
        <label>Devise<select name="preferred_currency"><option value="CAD"{' selected' if str(profile.get('preferred_currency')) == 'CAD' else ''}>CAD</option><option value="USD"{' selected' if str(profile.get('preferred_currency')) == 'USD' else ''}>USD</option><option value="EUR"{' selected' if str(profile.get('preferred_currency')) == 'EUR' else ''}>EUR</option></select></label>
        <label>Format cotes<select name="odds_format"><option value="DECIMAL"{' selected' if str(profile.get('odds_format')) == 'DECIMAL' else ''}>DECIMAL</option><option value="AMERICAN"{' selected' if str(profile.get('odds_format')) == 'AMERICAN' else ''}>AMERICAN</option><option value="FRACTIONAL"{' selected' if str(profile.get('odds_format')) == 'FRACTIONAL' else ''}>FRACTIONAL</option></select></label>
        <label>Timezone preference<input name="preference_timezone_name" value="{escape(str(profile.get('preference_timezone_name') or 'America/Toronto'))}"></label>
        <label>Tags<input name="tags_csv" value="{escape(tags_csv)}" placeholder="vip, risque, onboarding"></label>
        <label>Max bet<input type="number" step="0.01" min="0" name="max_single_bet" value="{escape('' if profile.get('max_single_bet') is None else str(profile.get('max_single_bet')))}"></label>
        <label>Max journalier<input type="number" step="0.01" min="0" name="max_daily_stake" value="{escape('' if profile.get('max_daily_stake') is None else str(profile.get('max_daily_stake')))}"></label>
        <label>Max expo<input type="number" step="0.01" min="0" name="max_open_exposure" value="{escape('' if profile.get('max_open_exposure') is None else str(profile.get('max_open_exposure')))}"></label>
        <label>Loss daily<input type="number" step="0.01" min="0" name="loss_limit_daily" value="{escape('' if profile.get('loss_limit_daily') is None else str(profile.get('loss_limit_daily')))}"></label>
        <label>Loss weekly<input type="number" step="0.01" min="0" name="loss_limit_weekly" value="{escape('' if profile.get('loss_limit_weekly') is None else str(profile.get('loss_limit_weekly')))}"></label>
        <label><input type="checkbox" name="alert_opt_in" value="1"{checked(profile.get('alert_opt_in'))}> Alertes actives</label>
        <label><input type="checkbox" name="marketing_opt_in" value="1"{checked(profile.get('marketing_opt_in'))}> Marketing actif</label>
        <label><input type="checkbox" name="automation_opt_in" value="1"{checked(profile.get('automation_opt_in'))}> Automation active</label>
        <label><input type="checkbox" name="requires_manual_review" value="1"{checked(profile.get('requires_manual_review'))}> Review manuelle</label>
        <div class="filter-actions"><button type="submit" class="button-primary">Enregistrer la fiche</button></div>
      </form>
    </section>
    <section class="section">
      <div class="section-head"><span class="no">02</span><h2>Ajouter une note</h2><span class="note">Journal admin CRM / risque / support.</span></div>
      <form method="post" action="/admin/client/{int(profile['user_id'])}" class="filterbar">
        <input type="hidden" name="action" value="add_client_note">
        <label>Type<select name="note_kind"><option value="ADMIN">ADMIN</option><option value="SUPPORT">SUPPORT</option><option value="RISK">RISK</option><option value="CRM">CRM</option></select></label>
        <label style="flex:1 1 420px">Note<textarea name="note_body" rows="4" placeholder="Contexte client, risque, suivi..." required></textarea></label>
        <div class="filter-actions"><button type="submit" class="button-primary">Ajouter la note</button></div>
      </form>
    </section>
    <section class="section">
      <div class="section-head"><span class="no">03</span><h2>Notes recentes</h2><span class="note">Les 20 dernieres notes admin.</span></div>
      <div class="table-wrap"><table><thead><tr><th>Type</th><th>Note</th><th>Auteur</th><th>Date</th></tr></thead><tbody>{note_rows}</tbody></table></div>
    </section>
    <section class="section">
      <div class="section-head"><span class="no">04</span><h2>Sessions recentes</h2><span class="note">Trace activite et connexions.</span></div>
      <div class="table-wrap"><table><thead><tr><th>Creation</th><th>Derniere activite</th><th>Expiration</th><th>Statut</th><th>IP</th></tr></thead><tbody>{session_rows}</tbody></table></div>
    </section>
    <section class="section">
      <div class="section-head"><span class="no">05</span><h2>Ledger bankroll</h2><span class="note">Derniers mouvements financiers du client.</span></div>
      <div class="table-wrap"><table><thead><tr><th>Type</th><th class="num">Variation</th><th class="num">Solde</th><th>Raison</th><th>Date</th></tr></thead><tbody>{bankroll_rows}</tbody></table></div>
    </section>
    {render_product_footer("football")}
  </main>
{dev_reload_script()}
</body>
</html>"""


def _back_qs(filters: dict[str, str]) -> str:
    # Emet les noms de parametres que la route GET /back relit (taken->positions).
    params: list[tuple[str, str]] = []
    scalar_map = {
        "kind": "kind",
        "league": "league",
        "taken": "positions",
        "sort": "sort",
        "sport": "sport",
        "day": "day",
        "date_kind": "date_kind",
        "admin_user_id": "admin_user_id",
    }
    for key, param in scalar_map.items():
        value = filters.get(key)
        if value and value != "all":
            params.append((param, str(value)))
    for market in filters.get("markets") or []:
        params.append(("market", str(market)))
    for status in filters.get("statuses") or []:
        params.append(("status", str(status)))
    return "&".join(f"{k}={quote(v)}" for k, v in params)


def selected_league_from_params(params: dict[str, list[str]]) -> str:
    league_values = params.get("league", [])
    if not league_values:
        return DEFAULT_LEAGUE
    league = league_values[0].strip()
    return league or DEFAULT_LEAGUE


# Horizons de filtre des pronostics : code -> (libelle, heures)
HORIZONS: dict[str, tuple[str, int]] = {
    "today": ("Jour", 24),
    "7d": ("Semaine", 168),
    "30d": ("Mois", 24 * 30),
    "all": ("Tout", 24 * 365),
}
DEFAULT_HORIZON = "7d"


def horizon_from_params(params: dict[str, list[str]]) -> str:
    values = params.get("horizon", [])
    code = values[0].strip() if values else ""
    return code if code in HORIZONS else DEFAULT_HORIZON


# Seuils de filtre (0 = pas de filtre) et cles de tri des pronostics.
SURETE_OPTIONS = (0, 50, 60, 70)
CONFIANCE_OPTIONS = (0, 50, 70, 80)
SORT_KEYS = {
    "kickoff": "Kickoff",
    "surete": "Surete",
    "confiance": "Confiance",
}
DEFAULT_SORT = "kickoff"


def prediction_filters_from_params(params: dict[str, list[str]]) -> dict[str, Any]:
    def _first(name: str, default: str = "") -> str:
        values = params.get(name, [])
        return values[0].strip() if values else default

    def _int_in(name: str, allowed: tuple[int, ...]) -> int:
        try:
            value = int(_first(name, "0") or 0)
        except ValueError:
            return 0
        return value if value in allowed else 0

    sort = _first("sort", DEFAULT_SORT)
    direction = _first("dir", "")
    if sort not in SORT_KEYS:
        sort = DEFAULT_SORT
    if direction not in ("asc", "desc"):
        # Kickoff se lit du plus proche au plus lointain ; les scores du
        # meilleur au moins bon.
        direction = "asc" if sort == "kickoff" else "desc"
    return {
        "horizon": horizon_from_params(params),
        "smin": _int_in("smin", SURETE_OPTIONS),
        "cmin": _int_in("cmin", CONFIANCE_OPTIONS),
        "sort": sort,
        "dir": direction,
    }


def _query_string(league: str, filters: dict[str, Any], **overrides: Any) -> str:
    merged = {**filters, **overrides}
    parts = [f"league={quote(league)}"]
    for key in ("view", "horizon", "smin", "cmin", "sort", "dir"):
        value = merged.get(key)
        if value not in (None, "", 0):
            parts.append(f"{key}={quote(str(value))}")
    return "/?" + "&".join(parts)


def load_dashboard_data(
    selected_league: str,
    horizon_code: str = DEFAULT_HORIZON,
    user_id: int = 1,
) -> dict[str, Any]:
    horizon_hours = HORIZONS.get(horizon_code, HORIZONS[DEFAULT_HORIZON])[1]
    connection = connect_db(DatabaseSettings.from_env())
    try:
        with connection.cursor() as cursor:
            total_fixtures = int(fetch_one(cursor, "SELECT COUNT(*) FROM core.fixtures") or 0)
            total_odds = int(fetch_one(cursor, "SELECT COUNT(*) FROM core.fixture_odds_1x2") or 0)
            total_predictions = int(fetch_one(cursor, "SELECT COUNT(*) FROM model.predictions") or 0)
            total_value_bets = int(fetch_one(cursor, "SELECT COUNT(*) FROM model.value_bets") or 0)

            league_fixtures = int(
                fetch_one(
                    cursor,
                    """
                    SELECT COUNT(*)
                    FROM reporting.v_match_board_1x2
                    WHERE (%s = '__ALL__' OR league_name = %s)
                      AND home_score IS NULL
                      AND kickoff_utc >= now()
                    """,
                    (selected_league, selected_league),
                )
                or 0
            )
            league_deals = int(
                fetch_one(
                    cursor,
                    """
                    WITH latest_model_run AS (
                        SELECT p.model_run_id
                        FROM model.deal_rankings d
                        JOIN model.predictions p
                          ON p.prediction_id = d.prediction_id
                        JOIN core.fixtures f
                          ON f.fixture_id = d.fixture_id
                        JOIN core.leagues l
                          ON l.league_id = f.league_id
                        WHERE d.value_bet_id IS NOT NULL
                          AND (%s = '__ALL__' OR l.league_name = %s)
                        ORDER BY d.created_at DESC
                        LIMIT 1
                    )
                    SELECT COUNT(*)
                    FROM model.deal_rankings d
                    JOIN model.predictions p
                      ON p.prediction_id = d.prediction_id
                    JOIN core.fixtures f
                      ON f.fixture_id = d.fixture_id
                    JOIN core.leagues l
                      ON l.league_id = f.league_id
                    JOIN latest_model_run r
                      ON p.model_run_id = r.model_run_id
                    WHERE d.value_bet_id IS NOT NULL
                      -- Proactif : un deal sur un match deja lance n'est plus
                      -- jouable, il sort du tableau (son sort se lit dans le
                      -- track record).
                      AND f.kickoff_utc >= now()
                      AND (%s = '__ALL__' OR l.league_name = %s)
                    """,
                    (selected_league, selected_league, selected_league, selected_league),
                )
                or 0
            )

            leagues = [
                row["league_name"]
                for row in fetch_dicts(
                    cursor,
                    """
                    SELECT DISTINCT league_name
                    FROM core.leagues
                    ORDER BY league_name
                    """
                )
            ]

            runs = fetch_dicts(
                cursor,
                """
                SELECT
                    provider_name,
                    status_code,
                    run_scope,
                    endpoint_code,
                    started_at,
                    finished_at,
                    heartbeat_at,
                    records_received,
                    records_written,
                    error_message,
                    application_name,
                    trigger_source,
                    host_name,
                    elapsed_seconds,
                    is_stale
                FROM reporting.v_ingestion_run_latest
                WHERE provider_code IN ('THESPORTSDB', 'THEODDSAPI')
                ORDER BY provider_name
                """
            )

            model_run = fetch_dicts(
                cursor,
                """
                SELECT
                    model_name,
                    model_version,
                    status_code,
                    started_at,
                    finished_at,
                    created_at
                FROM model.model_runs
                ORDER BY created_at DESC
                LIMIT 1
                """
            )

            upcoming_matches = fetch_dicts(
                cursor,
                """
                SELECT
                    v.fixture_id,
                    v.league_name,
                    v.home_team_name,
                    v.away_team_name,
                    v.kickoff_utc,
                    v.status_code,
                    ROUND((v.home_win_probability * 100)::numeric, 1) AS home_pct,
                    ROUND((v.draw_probability * 100)::numeric, 1) AS draw_pct,
                    ROUND((v.away_win_probability * 100)::numeric, 1) AS away_pct,
                    ROUND(v.expected_home_goals::numeric, 2) AS expected_home_goals,
                    ROUND(v.expected_away_goals::numeric, 2) AS expected_away_goals,
                    ROUND((v.confidence_score * 100)::numeric, 1) AS confidence_pct,
                    (mp.feature_snapshot_json::jsonb ->> 'explication') AS explication,
                    (mp.feature_snapshot_json::jsonb -> 'exact_scores') AS exact_scores,
                    (mp.feature_snapshot_json::jsonb -> 'marches_derives') AS marches_derives,
                    (mp.feature_snapshot_json::jsonb ->> 'pronostic') AS pronostic,
                    COALESCE(pos.position_count, 0) AS position_count,
                    COALESCE(pos.position_summary, '{}'::jsonb) AS position_summary,
                    EXISTS (
                        SELECT 1 FROM model.value_bets vb
                        WHERE vb.fixture_id = v.fixture_id AND vb.result_code IS NULL
                    ) AS has_value
                FROM reporting.v_match_board_1x2 v
                LEFT JOIN model.predictions mp
                  ON mp.prediction_id = v.prediction_id
                LEFT JOIN LATERAL (
                    SELECT COALESCE(SUM(pc.position_count), 0)::integer AS position_count,
                           COALESCE(
                               jsonb_object_agg(pc.market_code || '|' || pc.selection_code, pc.position_count),
                               '{}'::jsonb
                           ) AS position_summary
                    FROM (
                        SELECT p.market_code, p.selection_code, COUNT(*)::integer AS position_count
                        FROM model.user_bet_positions p
                        WHERE p.fixture_id = v.fixture_id
                          AND p.bet_kind = 'PRONOSTIC'
                          AND p.user_id = %(user_id)s
                          AND p.deleted_at IS NULL
                        GROUP BY p.market_code, p.selection_code
                    ) pc
                ) pos ON true
                WHERE (%(league)s = '__ALL__' OR v.league_name = %(league)s)
                  -- Proactif : un pronostic n'existe que pour un match SUR
                  -- LEQUEL ON PEUT ENCORE AGIR. Regle/termine (score connu)
                  -- ou deja lance (kickoff passe) = hors du tableau.
                  AND v.home_score IS NULL
                  AND v.kickoff_utc >= now()
                  AND v.kickoff_utc <= now() + (%(horizon_hours)s * interval '1 hour')
                ORDER BY v.kickoff_utc ASC NULLS LAST, v.fixture_id
                LIMIT 30
                """,
                {"league": selected_league, "horizon_hours": horizon_hours, "user_id": user_id},
            )

            ranked_deals = fetch_dicts(
                cursor,
                """
                -- Les deals du board = value bets ACTIFS non regles. Avec les
                -- runs PAR COMPETITION, "le dernier run" ne veut plus rien
                -- dire globalement ; le supersede-on-write garantit deja UNE
                -- recommandation courante par match.
                WITH deals AS (
                    SELECT
                        d.fixture_id,
                        l.league_name,
                        d.ranking_score,
                        d.summary_reason,
                        f.kickoff_utc,
                        home_team.team_name AS home_team_name,
                        away_team.team_name AS away_team_name,
                        b.bookmaker_name,
                        vb.selection_code,
                        vb.market_code,
                        COALESCE(pos.position_count, 0) AS position_count,
                        pos.taken_odd,
                        pos.stake_amount,
                        vb.model_probability,
                        vb.implied_probability,
                        vb.edge_probability,
                        vb.market_odd,
                        vb.fair_odd,
                        vb.line
                    FROM model.value_bets vb
                    JOIN model.deal_rankings d
                      ON d.value_bet_id = vb.value_bet_id
                    JOIN core.fixtures f
                      ON f.fixture_id = vb.fixture_id
                    JOIN core.leagues l
                      ON l.league_id = f.league_id
                    JOIN core.teams home_team
                      ON home_team.team_id = f.home_team_id
                    JOIN core.teams away_team
                      ON away_team.team_id = f.away_team_id
                    LEFT JOIN core.bookmakers b
                      ON b.bookmaker_id = vb.bookmaker_id
                    LEFT JOIN LATERAL (
                        SELECT
                            COUNT(*)::integer AS position_count,
                            ROUND((SUM(p.stake_amount * p.taken_odd) / NULLIF(SUM(p.stake_amount), 0))::numeric, 4) AS taken_odd,
                            ROUND(SUM(p.stake_amount)::numeric, 4) AS stake_amount
                        FROM model.user_bet_positions p
                        WHERE p.fixture_id = vb.fixture_id
                          AND p.bet_kind = 'DEAL'
                          AND p.market_code = vb.market_code
                          AND p.selection_code = vb.selection_code
                          AND p.user_id = %s
                          AND p.deleted_at IS NULL
                          AND (vb.market_code <> 'HANDICAP' OR p.line IS NOT DISTINCT FROM vb.line)
                    ) pos ON true
                    WHERE vb.status_code = 'ACTIVE'
                      AND vb.result_code IS NULL
                      -- Proactif : un deal sur un match deja lance n'est plus
                      -- jouable, il sort du tableau (son sort se lit dans le
                      -- track record).
                      AND f.kickoff_utc >= now()
                      AND (%s = '__ALL__' OR l.league_name = %s)
                ),
                fixture_ranking AS (
                    SELECT
                        fixture_id,
                        MAX(ranking_score) AS fixture_best_score,
                        MIN(kickoff_utc) AS first_kickoff
                    FROM deals
                    GROUP BY fixture_id
                ),
                ranked_fixtures AS (
                    SELECT
                        fixture_id,
                        fixture_best_score,
                        first_kickoff,
                        DENSE_RANK() OVER (
                            ORDER BY fixture_best_score DESC, first_kickoff ASC, fixture_id ASC
                        ) AS fixture_rank
                    FROM fixture_ranking
                )
                SELECT
                    d.fixture_id,
                    rf.fixture_rank AS rank_position,
                    d.kickoff_utc,
                    d.league_name,
                    d.home_team_name,
                    d.away_team_name,
                    d.bookmaker_name,
                    d.selection_code,
                    d.market_code,
                    d.line,
                    d.position_count,
                    d.taken_odd,
                    d.stake_amount,
                    d.summary_reason AS explication,
                    ROUND((d.model_probability * 100)::numeric, 1) AS model_pct,
                    ROUND((d.implied_probability * 100)::numeric, 1) AS implied_pct,
                    ROUND((d.edge_probability * 100)::numeric, 1) AS edge_pct,
                    ROUND(d.market_odd::numeric, 2) AS market_odd,
                    ROUND(d.fair_odd::numeric, 2) AS fair_odd,
                    ROUND((d.ranking_score * 100)::numeric, 1) AS ranking_pct
                FROM deals d
                JOIN ranked_fixtures rf
                  ON rf.fixture_id = d.fixture_id
                ORDER BY
                    rf.fixture_rank ASC,
                    d.ranking_score DESC,
                    d.kickoff_utc ASC
                LIMIT 20
                """,
                (user_id, selected_league, selected_league),
            )

            league_label = (
                "toutes competitions" if selected_league == ALL_LEAGUES else selected_league
            )
            football_last_update = fetch_one(
                cursor,
                """
                SELECT MAX(updated_at) FROM (
                    SELECT MAX(finished_at) AS updated_at
                    FROM ops.ingestion_runs
                    WHERE status_code = 'SUCCESS'
                    UNION ALL
                    SELECT MAX(generated_at) FROM model.predictions
                    UNION ALL
                    SELECT MAX(detected_at) FROM model.value_bets
                    UNION ALL
                    SELECT MAX(captured_at) FROM core.fixture_odds_1x2
                    UNION ALL
                    SELECT MAX(captured_at) FROM core.fixture_odds_totals
                    UNION ALL
                    SELECT MAX(captured_at) FROM core.fixture_odds_btts
                    UNION ALL
                    SELECT MAX(last_synced_at) FROM core.fixtures
                    UNION ALL
                    SELECT MAX(last_synced_at) FROM core.teams
                    UNION ALL
                    SELECT MAX(last_synced_at) FROM core.players
                ) updates
                """,
            )

            metrics = [
                MetricCard("Derniere mise a jour Quebec", format_metric_timestamp(football_last_update), "timestamp"),
                MetricCard("Fixtures en base", f"{total_fixtures}", "neutral"),
                MetricCard("Cotes capturees", f"{total_odds}", "neutral"),
                MetricCard("Predictions creees", f"{total_predictions}", "neutral"),
                MetricCard("Value bets detectes", f"{total_value_bets}", "positive"),
                MetricCard(f"Matchs {league_label}", f"{league_fixtures}", "accent"),
                MetricCard(f"Deals {league_label}", f"{league_deals}", "accent"),
            ]

            performance_rows = fetch_dicts(cursor, "SELECT * FROM reporting.v_deal_performance")
            performance = performance_rows[0] if performance_rows else None

            equity_points = fetch_dicts(
                cursor,
                """
                SELECT settled_at, profit_units
                FROM model.value_bets
                WHERE result_code IN ('WON', 'LOST') AND settled_at IS NOT NULL
                ORDER BY settled_at
                """,
            )

            return {
                "metrics": metrics,
                "leagues": leagues,
                "runs": runs,
                "model_run": model_run[0] if model_run else None,
                "upcoming_matches": upcoming_matches,
                "ranked_deals": ranked_deals,
                "performance": performance,
                "equity_points": equity_points,
            }
    finally:
        connection.close()


def load_match_detail(fixture_id: int) -> dict[str, Any] | None:
    """Toutes les donnees de la page detail d'un match."""
    connection = connect_db(DatabaseSettings.from_env())
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT f.fixture_id, l.league_name, f.kickoff_utc, f.status_code,
                       ht.team_id, ht.team_name, at.team_id, at.team_name
                FROM core.fixtures f
                JOIN core.leagues l ON l.league_id = f.league_id
                JOIN core.teams ht ON ht.team_id = f.home_team_id
                JOIN core.teams at ON at.team_id = f.away_team_id
                WHERE f.fixture_id = %s
                """,
                (fixture_id,),
            )
            row = cursor.fetchone()
            if not row:
                return None
            (_fid, league_name, kickoff_utc, status_code,
             home_id, home_name, away_id, away_name) = row

            prediction = fetch_dicts(
                cursor,
                """
                SELECT
                    ROUND((p.home_win_probability*100)::numeric,1) AS home_pct,
                    ROUND((p.draw_probability*100)::numeric,1) AS draw_pct,
                    ROUND((p.away_win_probability*100)::numeric,1) AS away_pct,
                    ROUND(p.expected_home_goals::numeric,2) AS xg_home,
                    ROUND(p.expected_away_goals::numeric,2) AS xg_away,
                    ROUND((p.confidence_score*100)::numeric,1) AS confidence_pct,
                    p.model_version,
                    p.generated_at,
                    p.feature_snapshot_json::jsonb ->> 'explication' AS explication,
                    p.feature_snapshot_json::jsonb ->> 'pronostic' AS pronostic,
                    p.feature_snapshot_json::jsonb -> 'exact_scores' AS exact_scores,
                    p.feature_snapshot_json::jsonb ->> 'exact_score_note' AS exact_score_note,
                    p.feature_snapshot_json::jsonb -> 'marches_derives' AS marches_derives,
                    ROUND(((p.feature_snapshot_json::jsonb ->> 'surete')::numeric)*100, 1) AS surete_pct
                FROM model.predictions p
                WHERE p.fixture_id = %s
                ORDER BY p.generated_at DESC
                LIMIT 1
                """,
                (fixture_id,),
            )

            odds = fetch_dicts(
                cursor,
                """
                SELECT b.bookmaker_name,
                       ROUND(o.home_odd::numeric,2) AS home_odd,
                       ROUND(o.draw_odd::numeric,2) AS draw_odd,
                       ROUND(o.away_odd::numeric,2) AS away_odd,
                       o.captured_at
                FROM reporting.v_latest_odds_1x2 o
                JOIN core.bookmakers b ON b.bookmaker_id = o.bookmaker_id
                WHERE o.fixture_id = %s
                ORDER BY b.bookmaker_name
                """,
                (fixture_id,),
            )

            h2h = fetch_dicts(
                cursor,
                """
                SELECT f.kickoff_utc, ht.team_name AS home, at.team_name AS away,
                       fs.home_score, fs.away_score, l.league_name
                FROM core.fixtures f
                JOIN core.teams ht ON ht.team_id = f.home_team_id
                JOIN core.teams at ON at.team_id = f.away_team_id
                JOIN core.leagues l ON l.league_id = f.league_id
                JOIN core.fixture_scores fs ON fs.fixture_id = f.fixture_id
                WHERE fs.home_score IS NOT NULL
                  AND ((f.home_team_id = %(h)s AND f.away_team_id = %(a)s)
                    OR (f.home_team_id = %(a)s AND f.away_team_id = %(h)s))
                  AND f.fixture_id <> %(fid)s
                ORDER BY f.kickoff_utc DESC
                LIMIT 6
                """,
                {"h": home_id, "a": away_id, "fid": fixture_id},
            )

            def team_form(team_id: int) -> list[dict[str, Any]]:
                return fetch_dicts(
                    cursor,
                    """
                    SELECT f.kickoff_utc,
                           CASE WHEN f.home_team_id = %(tid)s THEN at.team_name ELSE ht.team_name END AS opponent,
                           CASE WHEN f.home_team_id = %(tid)s THEN fs.home_score ELSE fs.away_score END AS gf,
                           CASE WHEN f.home_team_id = %(tid)s THEN fs.away_score ELSE fs.home_score END AS ga,
                           l.league_name
                    FROM core.fixtures f
                    JOIN core.teams ht ON ht.team_id = f.home_team_id
                    JOIN core.teams at ON at.team_id = f.away_team_id
                    JOIN core.leagues l ON l.league_id = f.league_id
                    JOIN core.fixture_scores fs ON fs.fixture_id = f.fixture_id
                    WHERE (f.home_team_id = %(tid)s OR f.away_team_id = %(tid)s)
                      AND fs.home_score IS NOT NULL
                      AND f.kickoff_utc < now()
                    ORDER BY f.kickoff_utc DESC
                    LIMIT 8
                    """,
                    {"tid": team_id},
                )

            insights = fetch_dicts(
                cursor,
                """
                SELECT insight_code, team_id, subject_label,
                       ROUND(effect_value::numeric,2) AS effect_value,
                       ROUND(baseline_value::numeric,2) AS baseline_value,
                       sample_with, sample_without, q_value, is_validated
                FROM model.correlation_insights
                WHERE team_id = ANY(%s)
                ORDER BY is_validated DESC, q_value ASC NULLS LAST, effect_value DESC
                LIMIT 10
                """,
                ([home_id, away_id],),
            )

            deals = fetch_dicts(
                cursor,
                """
                WITH latest AS (
                    SELECT p.model_run_id FROM model.deal_rankings d
                    JOIN model.predictions p ON p.prediction_id = d.prediction_id
                    ORDER BY d.created_at DESC LIMIT 1
                )
                SELECT b.bookmaker_name, vb.selection_code,
                       ROUND((vb.model_probability*100)::numeric,1) AS model_pct,
                       ROUND((vb.implied_probability*100)::numeric,1) AS implied_pct,
                       ROUND((vb.edge_probability*100)::numeric,1) AS edge_pct,
                       ROUND(vb.market_odd::numeric,2) AS market_odd,
                       ROUND(vb.fair_odd::numeric,2) AS fair_odd,
                       ROUND((d.ranking_score*100)::numeric,1) AS score_pct
                FROM model.deal_rankings d
                JOIN model.predictions p ON p.prediction_id = d.prediction_id
                JOIN latest r ON r.model_run_id = p.model_run_id
                JOIN model.value_bets vb ON vb.value_bet_id = d.value_bet_id
                JOIN core.bookmakers b ON b.bookmaker_id = vb.bookmaker_id
                WHERE d.fixture_id = %s
                ORDER BY d.ranking_score DESC
                """,
                (fixture_id,),
            )

            odds_history = fetch_dicts(
                cursor,
                """
                SELECT date_trunc('hour', o.captured_at) AS bucket,
                       percentile_cont(0.5) WITHIN GROUP (ORDER BY o.home_odd) AS home_odd,
                       percentile_cont(0.5) WITHIN GROUP (ORDER BY o.draw_odd) AS draw_odd,
                       percentile_cont(0.5) WITHIN GROUP (ORDER BY o.away_odd) AS away_odd
                FROM core.fixture_odds_1x2 o
                WHERE o.fixture_id = %s
                GROUP BY 1 ORDER BY 1
                """,
                (fixture_id,),
            )

            # Marche buteur : proba modele + cote + edge par joueur.
            scorers = fetch_dicts(
                cursor,
                """
                SELECT sb.player_name, sb.team_name,
                       ROUND(sb.probability * 100, 1) AS proba_pct,
                       sb.anytime_odd, sb.bookmaker_name,
                       CASE WHEN sb.anytime_odd IS NOT NULL AND sb.anytime_odd > 1
                            THEN ROUND((sb.probability - 1.0 / sb.anytime_odd) * 100, 1)
                            ELSE NULL END AS edge_pct
                FROM reporting.v_scorer_board sb
                WHERE sb.fixture_id = %s AND sb.probability IS NOT NULL
                ORDER BY sb.probability DESC
                LIMIT 12
                """,
                (fixture_id,),
            )

            return {
                "fixture_id": fixture_id,
                "league_name": league_name,
                "kickoff_utc": kickoff_utc,
                "status_code": status_code,
                "home_id": home_id,
                "home_name": home_name,
                "away_id": away_id,
                "away_name": away_name,
                "odds_history": odds_history,
                "prediction": prediction[0] if prediction else None,
                "odds": odds,
                "h2h": h2h,
                "home_form": team_form(home_id),
                "away_form": team_form(away_id),
                "insights": insights,
                "deals": deals,
                "scorers": scorers,
            }
    finally:
        connection.close()


def format_timestamp(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=ZoneInfo("UTC"))
        return value.astimezone(QUEBEC_TZ).strftime("%Y-%m-%d %H:%M HE")
    return escape(str(value))


def format_metric_timestamp(value: Any) -> str:
    if value is None:
        return "Jamais"
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=ZoneInfo("UTC"))
        return value.astimezone(QUEBEC_TZ).strftime("%Y-%m-%d %H:%M")
    return str(value)[:16]


def render_metric(metric: MetricCard) -> str:
    tone_classes = []
    if metric.tone in ("accent", "positive"):
        tone_classes.append("accent")
    if metric.tone == "timestamp":
        tone_classes.append("timestamp")
    if metric.tone == "negative":
        tone_classes.append("negative")
    tone_class = (" " + " ".join(tone_classes)) if tone_classes else ""
    return (
        f"<div class='metric{tone_class}'>"
        f"<div class='value'>{escape(metric.value)}</div>"
        f"<div class='label'>{escape(metric.label)}</div>"
        "</div>"
    )


def render_probability_rail(
    home_pct: float, draw_pct: float, away_pct: float,
    lead_code: str | None = None, has_value: bool = False, hero: bool = False,
) -> str:
    """Le rail de probabilites : segments aux positions exactes H/N/A,
    numeraux composes dans les segments, favori rempli (rouge si value bet)."""
    p_map = {"HOME": home_pct, "DRAW": draw_pct, "AWAY": away_pct}
    lead = lead_code or max(p_map, key=p_map.get)
    segs = []
    for code in ("HOME", "DRAW", "AWAY"):
        pct = p_map[code]
        classes = "seg"
        if code == lead:
            classes += " lead"
            if has_value:
                classes += " value"
        segs.append(
            f"<div class='{classes}' style='width:{max(pct, 4):.1f}%'>{pct:.0f}</div>"
        )
    hero_class = " rail-hero" if hero else ""
    return f"<div class='rail{hero_class}'>{''.join(segs)}</div>"


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip().replace(" ", "").replace(",", ".")
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _format_optional_number(value: Any, decimals: int = 1) -> str:
    number = _to_float(value)
    if number is None:
        return "-"
    return f"{number:.{decimals}f}"


def _parse_jsonish(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8")
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return None
    return None


def _normalize_exact_scores(value: Any, top_n: int = 5) -> list[dict[str, Any]]:
    parsed = _parse_jsonish(value)
    if not isinstance(parsed, list):
        return []

    normalized: list[dict[str, Any]] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        score = str(item.get("score") or "").strip()
        probability_pct = _to_float(item.get("probability_pct"))
        fair_odd = _to_float(item.get("fair_odd"))
        outcome_code = str(item.get("outcome_code") or "").strip().upper()
        if not score or probability_pct is None:
            continue
        normalized.append(
            {
                "score": score,
                "probability_pct": round(probability_pct, 1),
                "fair_odd": round(fair_odd, 2) if fair_odd is not None else None,
                "outcome_code": outcome_code,
            }
        )

    normalized.sort(key=lambda row: float(row["probability_pct"]), reverse=True)
    return normalized[:top_n]


def _fallback_exact_scores(row: dict[str, Any], top_n: int = 5) -> list[dict[str, Any]]:
    home_pct = _to_float(row.get("home_pct"))
    draw_pct = _to_float(row.get("draw_pct"))
    away_pct = _to_float(row.get("away_pct"))
    xg_home = _to_float(row.get("expected_home_goals"))
    xg_away = _to_float(row.get("expected_away_goals"))
    if xg_home is None:
        xg_home = _to_float(row.get("xg_home"))
    if xg_away is None:
        xg_away = _to_float(row.get("xg_away"))

    if None in (home_pct, draw_pct, away_pct, xg_home, xg_away):
        return []

    target = OutcomeProbabilities(
        home=max(0.0, float(home_pct) / 100.0),
        draw=max(0.0, float(draw_pct) / 100.0),
        away=max(0.0, float(away_pct) / 100.0),
    )
    top_scores, _distribution = build_exact_score_distribution(
        float(xg_home),
        float(xg_away),
        target,
        top_n=top_n,
    )
    return [
        {
            "score": score.score_label,
            "probability_pct": round(score.probability * 100.0, 1),
            "fair_odd": round(score.fair_odd, 2),
            "outcome_code": score.outcome_code,
        }
        for score in top_scores
    ]


def _exact_scores_for_row(row: dict[str, Any], top_n: int = 5) -> list[dict[str, Any]]:
    exact_scores = _normalize_exact_scores(row.get("exact_scores"), top_n=top_n)
    if exact_scores:
        return exact_scores
    return _fallback_exact_scores(row, top_n=top_n)


def _fair_odd_from_pct(probability_pct: float | None) -> float | None:
    if probability_pct is None or probability_pct <= 0:
        return None
    return round(100.0 / probability_pct, 2)


def _position_option_value(
    market_code: str,
    selection_code: str,
    probability_pct: float | None,
    fair_odd: float | None,
    label: str,
) -> str:
    probability = (probability_pct or 0.0) / 100.0 if probability_pct is not None else 0.0
    fair = fair_odd or _fair_odd_from_pct(probability_pct) or 0.0
    return "|".join([
        market_code,
        selection_code,
        f"{probability:.8f}",
        f"{fair:.4f}",
        label,
    ])


def _pronostic_position_options(row: dict[str, Any]) -> list[tuple[str, str]]:
    """Options jouables depuis un pronostic, avec cote modele/fair odd."""
    options: list[tuple[str, str]] = []
    position_summary = _parse_jsonish(row.get("position_summary")) or {}

    def _with_position_badge(market: str, selection: str, label: str) -> str:
        count = int(position_summary.get(f"{market}|{selection}") or 0)
        return f"{label} - PRIS x{count}" if count else label

    one_x_two = [
        ("HOME", "1X2 - Domicile", _to_float(row.get("home_pct"))),
        ("DRAW", "1X2 - Nul", _to_float(row.get("draw_pct"))),
        ("AWAY", "1X2 - Exterieur", _to_float(row.get("away_pct"))),
    ]
    for selection, label, pct in one_x_two:
        fair = _fair_odd_from_pct(pct)
        suffix = f" | modele {fair:.2f}" if fair else ""
        options.append((
            _position_option_value("1X2", selection, pct, fair, label),
            _with_position_badge("1X2", selection, label + suffix),
        ))

    derived = _parse_jsonish(row.get("marches_derives")) or {}
    derived_options = [
        ("OU25", "OVER", "+2,5 buts", _to_float(derived.get("over25_pct"))),
        ("OU25", "UNDER", "-2,5 buts", _to_float(derived.get("under25_pct"))),
        ("BTTS", "BTTS_YES", "BTTS oui", _to_float(derived.get("btts_oui_pct"))),
        ("BTTS", "BTTS_NO", "BTTS non", _to_float(derived.get("btts_non_pct"))),
    ]
    for market, selection, label, pct in derived_options:
        fair = _fair_odd_from_pct(pct)
        suffix = f" | modele {fair:.2f}" if fair else ""
        options.append((
            _position_option_value(market, selection, pct, fair, label),
            _with_position_badge(market, selection, label + suffix),
        ))

    for item in _exact_scores_for_row(row, top_n=3):
        score = str(item.get("score") or "").strip()
        pct = _to_float(item.get("probability_pct"))
        fair = _to_float(item.get("fair_odd")) or _fair_odd_from_pct(pct)
        if not score:
            continue
        label = f"Score exact {score}"
        suffix = f" | modele {fair:.2f}" if fair else ""
        options.append((
            _position_option_value("EXACT_SCORE", score, pct, fair, label),
            _with_position_badge("EXACT_SCORE", score, label + suffix),
        ))

    return options


def render_exact_score_stack(scores: list[dict[str, Any]], compact: bool = False) -> str:
    if not scores:
        return "<span class='muted'>-</span>"

    stack_class = "score-stack compact" if compact else "score-stack"
    cards = []
    for item in scores:
        probability_pct = _to_float(item.get("probability_pct"))
        fair_odd = _to_float(item.get("fair_odd"))
        meta = f"{probability_pct:.1f}%" if probability_pct is not None else "-"
        if fair_odd is not None:
            meta += f" | fair {fair_odd:.2f}"
        cards.append(
            "<div class='score-chip'>"
            f"<strong>{escape(str(item.get('score') or '-'))}</strong>"
            f"<span>{escape(meta)}</span>"
            "</div>"
        )
    return f"<div class='{stack_class}'>{''.join(cards)}</div>"


SELECTION_FRENCH_LABELS = {
    "HOME": "Victoire domicile",
    "DRAW": "Match nul",
    "AWAY": "Victoire exterieur",
}


def _verdict_for_row(row: dict[str, Any]) -> dict[str, Any]:
    """Compute the dashboard-side prediction verdict from a SQL row.

    Mirrors the engine's ``build_verdict`` but works off the already-rounded
    percentages stored in the report view, so we don't need a schema change.
    """
    home = _to_float(row.get("home_pct")) or 0.0
    draw = _to_float(row.get("draw_pct")) or 0.0
    away = _to_float(row.get("away_pct")) or 0.0
    confidence_pct = _to_float(row.get("confidence_pct"))
    candidates = [("HOME", home), ("DRAW", draw), ("AWAY", away)]
    candidates.sort(key=lambda kv: kv[1], reverse=True)
    top_code, top_pct = candidates[0]
    runner_up_pct = candidates[1][1] if len(candidates) > 1 else 0.0
    margin = max(0.0, top_pct - runner_up_pct)
    confidence_unit = (confidence_pct or 0.0) / 100.0
    surety = (
        0.18
        + 0.28 * (top_pct / 100.0)
        + 0.32 * (margin / 100.0)
        + 0.12 * confidence_unit
    )
    surety_pct = max(0.0, min(1.0, surety)) * 100.0
    if top_code == "HOME":
        predicted_team = row.get("home_team_name") or "-"
    elif top_code == "AWAY":
        predicted_team = row.get("away_team_name") or "-"
    else:
        predicted_team = "Match nul"
    expected_home = _to_float(row.get("expected_home_goals"))
    expected_away = _to_float(row.get("expected_away_goals"))
    total_xg = None
    if expected_home is not None and expected_away is not None:
        total_xg = round(expected_home + expected_away, 2)
    # Nul competitif : il talonne le pronostic sans le dominer (>= 28% et a
    # <= 8 pts du max) — meme regle que forecaster.draw_is_competitive.
    top_side = max(home, away)
    draw_competitive = (
        top_code != "DRAW" and draw >= 28.0 and (top_side - draw) <= 8.0
    )
    return {
        "verdict_code": top_code,
        "verdict_label": SELECTION_FRENCH_LABELS.get(top_code, top_code),
        "verdict_team": predicted_team,
        "verdict_probability_pct": round(top_pct, 1),
        "verdict_margin_pct": round(margin, 1),
        "verdict_surety_pct": round(surety_pct, 1),
        "total_xg": total_xg,
        "draw_competitive": draw_competitive,
    }


def _sort_header(
    label: str, key: str, league: str, filters: dict[str, Any] | None, numeric: bool = False,
) -> str:
    """En-tete triable : lien qui bascule la direction ; actif = noir plein
    avec caret CSS indiquant le sens."""
    css = "num" if numeric else ""
    if filters is None:
        return f"<th class='{css}'>{escape(label)}</th>"
    active = filters.get("sort") == key
    if active:
        next_dir = "desc" if filters.get("dir") == "asc" else "asc"
        caret = "caret-up" if filters.get("dir") == "asc" else "caret-down"
        href = _query_string(league, filters, sort=key, dir=next_dir)
        return (
            f"<th class='{css}'><a class='sort active {caret}' "
            f"href='{href}'>{escape(label)}</a></th>"
        )
    default_dir = "asc" if key == "kickoff" else "desc"
    href = _query_string(league, filters, sort=key, dir=default_dir)
    return f"<th class='{css}'><a class='sort' href='{href}'>{escape(label)}</a></th>"


def apply_prediction_filters(
    rows: list[dict[str, Any]], filters: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Enrichit (verdict/surete), applique les seuils, trie. La surete etant
    calculee cote dashboard, ce filtre vit en Python apres enrichissement."""
    enriched_rows = [{**row, **_verdict_for_row(row)} for row in rows]
    if not filters:
        return enriched_rows

    smin = float(filters.get("smin") or 0)
    cmin = float(filters.get("cmin") or 0)
    if smin > 0:
        enriched_rows = [
            r for r in enriched_rows
            if (_to_float(r.get("verdict_surety_pct")) or 0.0) >= smin
        ]
    if cmin > 0:
        enriched_rows = [
            r for r in enriched_rows
            if (_to_float(r.get("confidence_pct")) or 0.0) >= cmin
        ]

    sort_key = filters.get("sort", DEFAULT_SORT)
    reverse = filters.get("dir") == "desc"
    if sort_key == "surete":
        enriched_rows.sort(key=lambda r: _to_float(r.get("verdict_surety_pct")) or 0.0, reverse=reverse)
    elif sort_key == "confiance":
        enriched_rows.sort(key=lambda r: _to_float(r.get("confidence_pct")) or 0.0, reverse=reverse)
    else:
        enriched_rows.sort(
            key=lambda r: (r.get("kickoff_utc") is None, str(r.get("kickoff_utc") or "")),
            reverse=reverse,
        )
    return enriched_rows[:30]


def render_predictions_table(
    rows: list[dict[str, Any]], empty_label: str,
    league: str = DEFAULT_LEAGUE, filters: dict[str, Any] | None = None,
    user: UserContext | None = None,
) -> str:
    rows = apply_prediction_filters(rows, filters)
    headers = (
        "<th>Position</th>"
        "<th>Match</th>"
        + _sort_header("Kickoff", "kickoff", league, filters)
        + "<th>Pronostic</th>"
        "<th style='min-width:280px'>Probabilites H / N / A</th>"
        + _sort_header("Surete", "surete", league, filters, numeric=True)
        + "<th class='num'>xG dom.</th><th class='num'>xG ext.</th><th class='num'>xG match</th>"
        + _sort_header("Confiance", "confiance", league, filters, numeric=True)
        + "<th>Score exact</th>"
        + "<th>Analyse</th>"
    )
    if not rows:
        body = f"<tr><td colspan='12' class='empty'>{escape(empty_label)}</td></tr>"
    else:
        rendered_rows: list[str] = []
        for row in rows:
            enriched = row
            # Pas encore de prediction (pipeline pas passe sur ce match) :
            # afficher l'attente honnetement, jamais un faux HOME 0/0/0.
            if row.get("home_pct") is None:
                match_label = (
                    f"<strong>{escape(str(row.get('home_team_name') or '-'))}</strong>"
                    f" <span class='muted'>vs</span> "
                    f"<strong>{escape(str(row.get('away_team_name') or '-'))}</strong>"
                    f"<div class='muted' style='font-size:12px'>{escape(str(row.get('league_name') or ''))}</div>"
                )
                rendered_rows.append(
                    "<tr>"
                    "<td></td>"
                    f"<td>{match_label}</td>"
                    f"<td class='muted'>{format_timestamp(row.get('kickoff_utc'))}</td>"
                    "<td colspan='9' class='muted'>Pas encore analyse - "
                    "selectionne cette competition et clique « Lancer predictions ».</td>"
                    "</tr>"
                )
                continue
            verdict_code = str(enriched.get("verdict_code") or "")
            home_pct = _to_float(row.get("home_pct")) or 0.0
            draw_pct = _to_float(row.get("draw_pct")) or 0.0
            away_pct = _to_float(row.get("away_pct")) or 0.0
            rail = render_probability_rail(
                home_pct, draw_pct, away_pct,
                lead_code=verdict_code,
                has_value=bool(row.get("has_value")),
            )
            fid = enriched.get("fixture_id")
            link = (
                f"<a class='detail-link' href='/match/{int(fid)}'>Analyse</a>"
                if fid is not None else "-"
            )
            exact_scores_html = render_exact_score_stack(
                _exact_scores_for_row(row, top_n=3),
                compact=True,
            )
            match_label = (
                f"<strong>{escape(str(row.get('home_team_name') or '-'))}</strong>"
                f" <span class='muted'>vs</span> "
                f"<strong>{escape(str(row.get('away_team_name') or '-'))}</strong>"
                f"<div class='muted' style='font-size:12px'>{escape(str(row.get('league_name') or ''))}</div>"
            )
            # La case "prise" cible le pronostic stocke (meme selection que le
            # join d'annotation) ; les matchs affiches sont tous a venir.
            pronostic_code = str(row.get("pronostic") or verdict_code)
            rendered_rows.append(
                "<tr>"
                + _prise_button(
                    title="Prendre un pronostic",
                    subtitle=(
                        f"{str(row.get('home_team_name') or '')} vs "
                        f"{str(row.get('away_team_name') or '')}"
                    ),
                    bet_kind="PRONOSTIC",
                    fixture_id=enriched.get("fixture_id"),
                    options=_pronostic_position_options(row),
                    position_count=row.get("position_count"),
                    return_league=league,
                    can_write=can_manage_positions(user),
                )
                + f"<td>{match_label}</td>"
                f"<td class='muted'>{format_timestamp(row.get('kickoff_utc'))}</td>"
                f"<td>{_pick_chip(verdict_code, lead=True)}"
                + (
                    "<div style='font-size:11px;margin-top:4px;letter-spacing:.04em'>"
                    "NUL TRES POSSIBLE</div>"
                    if enriched.get("draw_competitive") else ""
                )
                + f"<div class='muted' style='font-size:12px;margin-top:4px'>{escape(str(enriched.get('verdict_team') or ''))}</div></td>"
                f"<td>{rail}</td>"
                f"<td class='num'><strong>{enriched.get('verdict_surety_pct', '-')}</strong></td>"
                f"<td class='num'>{enriched.get('expected_home_goals', '-')}</td>"
                f"<td class='num'>{enriched.get('expected_away_goals', '-')}</td>"
                f"<td class='num'><strong>{enriched.get('total_xg', '-')}</strong></td>"
                f"<td class='num'>{enriched.get('confidence_pct', '-')}</td>"
                f"<td>{exact_scores_html}</td>"
                f"<td>{link}</td>"
                "</tr>"
            )
            explication = row.get("explication")
            if explication:
                rendered_rows.append(
                    "<tr class='explain-row'><td colspan='12'>"
                    "<details><summary>Pourquoi cette prediction</summary>"
                    f"<p>{escape(str(explication))}</p></details></td></tr>"
                )
        body = "".join(rendered_rows)
    table = (
        "<div class='table-wrap'>"
        f"<table><thead><tr>{headers}</tr></thead><tbody>{body}</tbody></table>"
        "</div>"
    )
    return render_section(
        "01", "Predictions", table,
        note="Choisis le type de pari du pronostic, entre la cote reelle prise et la mise. Chaque OK ajoute un ticket ; le Back cumule les prises.",
    )


def _edge_cell(edge_pct: float | None) -> str:
    """Edge positif = LE signal rouge (la ou il y a de la value). Negatif = gris."""
    if edge_pct is None:
        return "<td class='num'>-</td>"
    if edge_pct > 0:
        return f"<td class='num sig'>+{edge_pct}</td>"
    return f"<td class='num muted'>{edge_pct}</td>"


# Libelles des selections des marches derives (le 1X2 garde ses codes bruts).
SELECTION_CHIP_LABELS = {
    "OVER": "+2,5 BUTS",
    "UNDER": "-2,5 BUTS",
    "BTTS_YES": "BTTS OUI",
    "BTTS_NO": "BTTS NON",
    "DC_1X": "1X", "DC_12": "12", "DC_X2": "X2",
}

# Ligne O/U par marche (le code OVER/UNDER est partage entre les lignes).
_OU_LINE_LABEL = {"OU15": "1,5", "OU25": "2,5", "OU35": "3,5"}


def _format_line_value(line: Any) -> str:
    line_f = _to_float(line)
    if line_f is None:
        return ""
    return f"{line_f:+g}".replace(".", ",")


def _selection_display(selection_code: str, market_code: str | None,
                       line: Any = None) -> str:
    raw = str(selection_code or "-")
    if raw in ("OVER", "UNDER"):
        ou_line = _OU_LINE_LABEL.get(str(market_code or ""), "2,5")
        return f"{'+' if raw == 'OVER' else '-'}{ou_line} BUTS"
    if market_code == "DNB":
        return "DNB DOM." if raw == "HOME" else "DNB EXT." if raw == "AWAY" else raw
    if market_code == "HANDICAP":
        side = "Domicile" if raw == "HOME" else "Exterieur" if raw == "AWAY" else raw
        line_label = _format_line_value(line)
        return f"{side} handicap {line_label}" if line_label else f"{side} handicap (ligne manquante)"
    return SELECTION_CHIP_LABELS.get(raw, raw)


def _bet_instruction(market_code: Any, selection_code: Any, line: Any = None) -> str:
    market = str(market_code or "")
    selection = str(selection_code or "")
    if market == "HANDICAP":
        side = "Domicile" if selection == "HOME" else "Exterieur" if selection == "AWAY" else selection
        line_label = _format_line_value(line)
        if line_label:
            return f"Bet365: Handicap asiatique / Handicap - choisir {side} {line_label}"
        return f"Bet365: Handicap - choisir {side}; ligne manquante dans le flux, ne pas jouer sans verifier."
    if market.startswith("OU"):
        line_label = _OU_LINE_LABEL.get(market, "2,5")
        side = "Plus de" if selection == "OVER" else "Moins de" if selection == "UNDER" else selection
        return f"Bet365: Total buts - {side} {line_label}"
    if market == "BTTS":
        side = "Oui" if selection == "BTTS_YES" else "Non" if selection == "BTTS_NO" else selection
        return f"Bet365: Les deux equipes marquent - {side}"
    if market == "DNB":
        side = "Domicile" if selection == "HOME" else "Exterieur" if selection == "AWAY" else selection
        return f"Bet365: Rembourse si nul - {side}"
    if market == "DOUBLE_CHANCE":
        return f"Bet365: Double chance - {_selection_display(selection, market)}"
    if market == "EXACT_SCORE":
        return f"Bet365: Score exact - {selection}"
    return "Bet365: Resultat du match / 1X2"


def _pick_chip(selection_code: str | None, lead: bool = False,
               market_code: str | None = None, line: Any = None) -> str:
    label = escape(_selection_display(str(selection_code or "-"), market_code, line))
    return f"<span class='pick{' lead' if lead else ''}'>{label}</span>"


# ---------------------------------------------------------------------------
# Libelles football facon GOLF : le pari s'enonce en UNE phrase claire, et la
# ligne de detail porte le contexte (match, book, echeance, ou poser le pari).
# Meme grammaire que _golf_outright_bet_sentence / _golf_competition_line.
# ---------------------------------------------------------------------------
def _football_teams(label: Any) -> tuple[str, str]:
    """('Arsenal vs Chelsea') -> ('Arsenal', 'Chelsea'). Outrights -> ('', '')."""
    text = str(label or "").strip()
    if " vs " in text:
        home, away = text.split(" vs ", 1)
        return home.strip(), away.strip()
    return "", ""


def _football_bet_sentence(label: Any, market_code: Any, selection_code: Any,
                           line: Any = None) -> str:
    """Le pari en clair : « Parier que X bat Y », « ... depasse 2,5 buts »..."""
    home, away = _football_teams(label)
    market = str(market_code or "1X2")
    selection = str(selection_code or "")
    match_txt = f"{home} - {away}" if home and away else str(label or "-")

    if not home or not away:
        # Marches long terme : le label porte deja « Marche : sujet ».
        return f"Parier sur {label}"
    if market == "1X2":
        if selection == "HOME":
            return f"Parier que {home} bat {away}"
        if selection == "AWAY":
            return f"Parier que {away} bat {home}"
        if selection == "DRAW":
            return f"Parier sur le nul entre {home} et {away}"
    if market.startswith("OU"):
        goals = _OU_LINE_LABEL.get(market, "2,5")
        if selection == "OVER":
            return f"Parier que {match_txt} depasse {goals} buts"
        if selection == "UNDER":
            return f"Parier que {match_txt} reste sous {goals} buts"
    if market == "BTTS":
        if selection == "BTTS_YES":
            return f"Parier que {home} et {away} marquent tous les deux"
        if selection == "BTTS_NO":
            return f"Parier qu'une des deux equipes ne marque pas"
    if market == "DNB":
        if selection == "HOME":
            return f"Parier sur {home}, rembourse si nul"
        if selection == "AWAY":
            return f"Parier sur {away}, rembourse si nul"
    if market == "DOUBLE_CHANCE":
        if selection == "DC_1X":
            return f"Parier que {home} gagne ou fait nul"
        if selection == "DC_X2":
            return f"Parier que {away} gagne ou fait nul"
        if selection == "DC_12":
            return f"Parier que {home} ou {away} gagne (pas de nul)"
    if market == "HANDICAP":
        side = home if selection == "HOME" else away if selection == "AWAY" else selection
        line_label = _format_line_value(line)
        return (f"Parier sur {side} avec handicap {line_label}" if line_label
                else f"Parier sur {side} avec handicap")
    if market == "EXACT_SCORE":
        return f"Parier sur le score exact {selection} - {match_txt}"
    return f"Parier sur {match_txt} - {_selection_display(selection, market, line)}"


def _football_market_label(market_code: Any) -> str:
    """Nom du MARCHE (pas de la selection) — pendant de _golf_market_label.
    La selection est deja portee par la phrase de pari."""
    market = str(market_code or "1X2")
    if market.startswith("OU"):
        return f"Total buts {_OU_LINE_LABEL.get(market, '2,5')}"
    return {
        "1X2": "Resultat du match",
        "BTTS": "Les deux equipes marquent",
        "DNB": "Rembourse si nul",
        "DOUBLE_CHANCE": "Double chance",
        "HANDICAP": "Handicap asiatique",
        "EXACT_SCORE": "Score exact",
    }.get(market, market)


def _football_competition_line(line_row: dict[str, Any]) -> str:
    """Contexte du pari : match - book - echeance - ou le poser (facon golf)."""
    parts = [str(line_row.get("label") or "").strip()]
    book = str(line_row.get("bookmaker") or "").strip()
    if book and book != "-":
        parts.append(book)
    kickoff = line_row.get("kickoff_utc")
    if kickoff:
        parts.append(format_timestamp(kickoff))
    # Meme grammaire que le golf (« Ou poser: Tour > 18 trous »), en gardant
    # le bookmaker cible dans le chemin : « Ou poser: Bet365 > Total buts... ».
    parts.append(
        _bet_instruction(
            line_row.get("market_code"), line_row.get("selection_code"), line_row.get("line"),
        ).replace("Bet365: ", "Ou poser: Bet365 > ")
    )
    return " - ".join(part for part in parts if part)


def _football_take_cell(key: str, market_odd: Any, position_count: int = 0,
                        total_stake: Any = None, return_to: str = "",
                        stake_default: float | None = None,
                        selection_label: str = "", line: Any = None,
                        can_write: bool = True) -> str:
    """Prise inline football — meme mecanique que _golf_take_cell : badge
    PRIS xN, retrait, puis mini-formulaire cote/mise -> ticket complet.
    Remplace le modal : la prise se fait sans quitter le tableau."""
    if not key:
        return "<td class='muted'>-</td>"
    badge = ""
    if position_count:
        stake_txt = f" · {float(total_stake):g}$" if total_stake else ""
        badge = (f"<span class='pick sig' title='Positions prises'>PRIS x{position_count}"
                 f"{stake_txt}</span> ")
    if not can_write:
        hint = "<span class='muted'>Lecture seule</span>" if position_count else "<span class='muted'>Validation seulement</span>"
        return f"<td class='cell-nowrap'>{badge}{hint}</td>"
    delete_controls = ""
    if position_count:
        delete_controls = (
            "<details class='ticket-details'>"
            "<summary class='detail-link'>Supprimer prise</summary>"
            "<div class='ticket-list'>"
            "<form method='post' class='inline-form'>"
            "<input type='hidden' name='action' value='clear_position' />"
            f"<input type='hidden' name='key' value='{escape(key)}' />"
            f"<input type='hidden' name='return_to' value='{escape(return_to)}' />"
            "<button type='submit' class='button-mini'>Tout retirer</button>"
            "</form>"
            "</div></details>"
        )
    odd_f = _to_float(market_odd)
    odd_val = f"{odd_f:.2f}" if odd_f and odd_f > 1 else ""
    stake_val = f"{stake_default:.2f}" if stake_default else ""
    form = (
        "<form method='post' class='golf-take-form'>"
        "<input type='hidden' name='action' value='take_position' />"
        f"<input type='hidden' name='key' value='{escape(key)}' />"
        f"<input type='hidden' name='selection_label' value='{escape(selection_label)}' />"
        f"<input type='hidden' name='line' value='{escape(str(line or ''))}' />"
        f"<input type='hidden' name='return_to' value='{escape(return_to)}' />"
        f"<input type='number' name='taken_odd' value='{odd_val}' step='any' min='1.01' "
        "title='Cote prise' class='input-compact input-stake' />"
        f"<input type='number' name='stake_amount' value='{stake_val}' step='any' min='0.01' placeholder='mise $' "
        "title='Mise en $' class='input-compact input-stake' />"
        "<button type='submit' class='button-compact'>Prendre</button>"
        "</form>"
    )
    return f"<td class='cell-nowrap'>{badge}{delete_controls}{form}</td>"


def prise_modal_html(return_to: str, return_league: str) -> str:
    """UN modal partage (dialog natif) + JS, injecte une fois par page.

    Le bouton Prise remplit ce modal depuis ses data-* puis l'ouvre. Deux
    modes : selection FIXE (deal/strategie -> data-key) ou CHOIX du type de
    pari (pronostic -> data-options JSON). return_to redirige apres la prise.
    """
    return f"""
<dialog id="prise-modal" class="prise-modal">
  <form method="post" action="/">
    <h3 id="prise-title" class="modal-title">Prendre le pari</h3>
    <p id="prise-sub" class="muted modal-sub"></p>
    <input type="hidden" name="action" id="prise-action" value="take_position">
    <input type="hidden" name="key" id="prise-key">
    <input type="hidden" name="bet_kind" id="prise-betkind">
    <input type="hidden" name="fixture_id" id="prise-fixture">
    <input type="hidden" name="parlay" id="prise-parlay">
    <input type="hidden" name="selection_label" id="prise-selection-label">
    <input type="hidden" name="line" id="prise-line">
    <input type="hidden" name="league" value="{escape(return_league)}">
    <input type="hidden" name="return_to" value="{escape(return_to)}">
    <div id="prise-pick-wrap" class="modal-field">
      <label class="form-field">Type de pari
        <select id="prise-pick"></select>
      </label>
    </div>
    <div class="modal-grid">
      <label id="prise-odd-label" class="form-field">Cote reelle prise
        <input type="number" name="taken_odd" id="prise-odd" min="1.01" step="0.01" required
               >
      </label>
      <label class="form-field">Montant mise
        <input type="number" name="stake_amount" id="prise-stake" min="0.01" step="0.01" value="1"
               >
      </label>
    </div>
    <div class="form-actions">
      <button type="button" onclick="document.getElementById('prise-modal').close()"
              class="button-secondary">Annuler</button>
      <button type="submit" class="button-primary">Confirmer la prise</button>
    </div>
  </form>
</dialog>
<script>
function openPrise(btn){{
  var d=document.getElementById('prise-modal');
  document.getElementById('prise-title').textContent=btn.dataset.title||'Prendre le pari';
  document.getElementById('prise-sub').textContent=btn.dataset.sub||'';
  document.getElementById('prise-key').value=btn.dataset.key||'';
  document.getElementById('prise-betkind').value=btn.dataset.betkind||'';
  document.getElementById('prise-fixture').value=btn.dataset.fixture||'';
  document.getElementById('prise-selection-label').value=btn.dataset.selectionLabel||'';
  document.getElementById('prise-line').value=btn.dataset.line||'';
  var wrap=document.getElementById('prise-pick-wrap');
  var pick=document.getElementById('prise-pick');
  var oddLabel=document.getElementById('prise-odd-label');
  var parlayField=document.getElementById('prise-parlay');
  var actionField=document.getElementById('prise-action');
  if(btn.dataset.parlay){{
    // Combine : selection fixe (toutes les jambes), on saisit cote combinee + mise.
    actionField.value='take_parlay';
    parlayField.value=btn.dataset.parlay;
    wrap.style.display='none';pick.name='';
    oddLabel.firstChild.textContent='Cote combinee reelle';
  }} else {{
    actionField.value='take_position';
    parlayField.value='';
    oddLabel.firstChild.textContent='Cote reelle prise';
    if(btn.dataset.options){{
      var opts=JSON.parse(btn.dataset.options);
      pick.innerHTML=opts.map(function(o){{return '<option value="'+o[0].replace(/"/g,'&quot;')+'">'+o[1]+'</option>';}}).join('');
      wrap.style.display='';pick.name='position_pick';
    }} else {{ wrap.style.display='none';pick.name=''; }}
  }}
  var o=parseFloat(btn.dataset.odd);
  document.getElementById('prise-odd').value=(o&&o>1)?o.toFixed(2):'';
  document.getElementById('prise-stake').value='1';
  d.showModal();
}}
</script>
"""


def _prise_button(
    title: str,
    subtitle: str = "",
    key: str = "",
    bet_kind: str = "",
    fixture_id: Any = None,
    suggested_odd: Any = None,
    options: list[tuple[str, str]] | None = None,
    position_count: Any = 0,
    selection_label: str = "",
    line: Any = None,
    return_league: str = DEFAULT_LEAGUE,
    return_to: str = "/",
    cell: bool = True,
    parlay_legs: list[dict[str, Any]] | None = None,
    can_write: bool = True,
) -> str:
    """Bouton "Prise" ouvrant le modal partage. Porte tout via data-*.

    - deal/strategie : passer `key` (selection fixe) -> confirmation directe.
    - pronostic : passer `options` (choix du type de pari) + bet_kind/fixture.
    - combine : passer `parlay_legs` -> modal cote combinee + mise.
    """
    odd_attr = ""
    odd_val = _to_float(suggested_odd)
    if odd_val is not None and odd_val > 1:
        odd_attr = f"{odd_val:.2f}"
    count = int(position_count or 0)
    data = (
        f"data-title=\"{escape(title)}\" "
        f"data-sub=\"{escape(subtitle)}\" "
        f"data-key=\"{escape(key)}\" "
        f"data-betkind=\"{escape(bet_kind)}\" "
        f"data-fixture=\"{int(fixture_id) if fixture_id else ''}\" "
        f"data-odd=\"{odd_attr}\" "
        f"data-selection-label=\"{escape(selection_label)}\" "
        f"data-line=\"{escape(str(line or ''))}\" "
    )
    if options:
        data += f"data-options='{escape(json.dumps(options, ensure_ascii=True))}' "
    if parlay_legs:
        data += f"data-parlay='{escape(json.dumps(parlay_legs, ensure_ascii=True))}' "
    count_badge = (
        f"<span class='pick lead'>Pris x{count}</span>"
        if count > 0 else ""
    )
    if not can_write:
        read_only = (
            f"<span class='taken-meta'>{count_badge}<span class='muted'>"
            f"{'Lecture seule' if count > 0 else 'Validation seulement'}</span></span>"
        )
        if cell:
            return f"<td class='cell-nowrap'>{read_only}</td>"
        return read_only
    clear_btn = ""
    if count > 0 and key:
        clear_btn = (
            "<form method='post' action='/' class='inline-form'>"
            "<input type='hidden' name='action' value='clear_position'>"
            f"<input type='hidden' name='key' value='{escape(key)}'>"
            f"<input type='hidden' name='league' value='{escape(return_league)}'>"
            f"<input type='hidden' name='return_to' value='{escape(return_to)}'>"
            "<button type='submit' title='Tout retirer' class='button-mini'>X</button>"
            "</form>"
        )
    button = (
        f"<span class='taken-meta'>{count_badge}"
        f"<button type='button' class='prise-btn' onclick='openPrise(this)' {data}>Prise</button>"
        f"{clear_btn}"
        "</span>"
    )
    if cell:
        return f"<td class='cell-nowrap'>{button}</td>"
    return button


def _position_form(
    bet_kind: str,
    fixture_id: Any,
    market_code: str,
    selection_code: str,
    taken: bool,
    return_league: str,
    suggested_odd: Any = None,
    taken_odd: Any = None,
    stake_amount: Any = None,
    position_count: Any = 0,
    options: list[tuple[str, str]] | None = None,
) -> str:
    """Declaration live d'une position avec cote prise et mise."""
    if fixture_id is None:
        return "<td></td>"
    key = f"{bet_kind}|f{int(fixture_id)}|{market_code}|{selection_code}"
    odd_value = _to_float(taken_odd)
    stake_value = _to_float(stake_amount)
    suggested_value = _to_float(suggested_odd)
    count = int(position_count or 0)

    suggested_attr = (
        f" value='{suggested_value:.2f}'" if suggested_value is not None and suggested_value > 1 else ""
    )
    taken_hint = ""
    if count > 0:
        odd_label = f"{odd_value:.2f}" if odd_value is not None else "-"
        stake_label = f"{stake_value:.2f}" if stake_value is not None else "0.00"
        if options:
            taken_hint = (
                "<div class='taken-meta'>"
                f"<span class='pick lead'>Pris x{count}</span>"
                "<span class='muted'>sur ce match</span></div>"
            )
        else:
            taken_hint = (
                "<div class='taken-meta'>"
                f"<span class='pick lead'>Pris x{count}</span>"
                f"<span class='muted'>moy @{escape(odd_label)} / {escape(stake_label)}u</span>"
                "<form method='post' action='/' class='inline-form'>"
                "<input type='hidden' name='action' value='clear_position'>"
                f"<input type='hidden' name='key' value='{escape(key)}'>"
                f"<input type='hidden' name='league' value='{escape(return_league)}'>"
                "<button type='submit' title='Supprimer ces prises' class='button-mini'>X</button>"
                "</form></div>"
            )

    picker_html = ""
    hidden_key = f"<input type='hidden' name='key' value='{escape(key)}'>"
    if options:
        opts = "".join(
            f"<option value='{escape(value)}'>{escape(label)}</option>"
            for value, label in options
        )
        picker_html = (
            "<select name='position_pick' title='Type de pari' class='input-compact select-market'>"
            f"{opts}</select>"
        )
        hidden_key = (
            f"<input type='hidden' name='bet_kind' value='{escape(bet_kind)}'>"
            f"<input type='hidden' name='fixture_id' value='{int(fixture_id)}'>"
        )
    return (
        "<td>"
        f"{taken_hint}"
        "<form method='post' action='/' class='inline-bet-form'>"
        "<input type='hidden' name='action' value='take_position'>"
        f"{hidden_key}"
        f"<input type='hidden' name='league' value='{escape(return_league)}'>"
        f"{picker_html}"
        f"<input type='number' name='taken_odd' min='1.01' step='any'{suggested_attr} "
        "placeholder='cote' required title='Cote reelle prise' "
        "class='input-compact input-odd'>"
        "<input type='number' name='stake_amount' min='0.01' step='any' value='1.00' "
        "title='Mise en unites' "
        "class='input-compact input-stake'>"
        "<button type='submit' class='button-compact'>OK</button>"
        "</form></td>"
    )


def _derived_markets_cells(value: Any) -> str:
    """Cellules P(+2,5 buts) et P(BTTS) du bandeau verdict, si le snapshot
    de la prediction les porte (predictions posterieures aux marches derives)."""
    payload = value
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError):
            return ""
    if not isinstance(payload, dict):
        return ""
    cells = []
    over = payload.get("over25_pct")
    btts = payload.get("btts_oui_pct")
    if over is not None:
        cells.append(
            f"<div class='cell'><div class='v'>{escape(str(over))}</div>"
            "<div class='l'>P(+2,5 buts) %</div></div>"
        )
    if btts is not None:
        cells.append(
            f"<div class='cell'><div class='v'>{escape(str(btts))}</div>"
            "<div class='l'>P(BTTS oui) %</div></div>"
        )
    return "".join(cells)


def _kelly_stake_pct(model_pct: float | None, market_odd: float | None) -> float | None:
    """Fractional Kelly (x0.25, plafond 5%) — % de bankroll a miser.

    Reproduit decision._fractional_kelly avec les valeurs par defaut de
    DecisionConfig, a partir des colonnes deja affichees.
    """
    if model_pct is None or market_odd is None or market_odd <= 1.0:
        return None
    p = model_pct / 100.0
    b = market_odd - 1.0
    raw = ((b * p) - (1.0 - p)) / b
    stake = max(0.0, raw) * 0.25
    return round(min(stake, 0.05) * 100.0, 2)


def render_deals_table(
    rows: list[dict[str, Any]], empty_label: str, league: str = DEFAULT_LEAGUE,
    user: UserContext | None = None,
) -> str:
    headers = (
        "<th>Position</th><th class='num'>Rang</th><th>Match</th><th>Kickoff</th><th>Bookmaker</th>"
        "<th>Selection</th><th class='num'>Modele</th><th class='num'>Marche</th>"
        "<th class='num'>Edge</th><th class='num'>Cote</th><th class='num'>Fair</th>"
        "<th class='num'>Mise</th><th class='num'>Score</th><th>Analyse</th>"
    )
    if not rows:
        body = f"<tr><td colspan='14' class='empty'>{escape(empty_label)}</td></tr>"
    else:
        rendered_rows = []
        for row in rows:
            edge_pct = _to_float(row.get("edge_pct"))
            stake = _kelly_stake_pct(
                _to_float(row.get("model_pct")), _to_float(row.get("market_odd"))
            )
            fid = row.get("fixture_id")
            link = (
                f"<a class='detail-link' href='/match/{int(fid)}'>Analyse</a>"
                if fid is not None else "-"
            )
            league_line = (
                f"<div class='muted' style='font-size:12px'>{escape(str(row.get('league_name')))}</div>"
                if row.get("league_name") else ""
            )
            match_label = (
                f"{escape(str(row.get('home_team_name') or '-'))}"
                f" <span class='muted'>vs</span> "
                f"{escape(str(row.get('away_team_name') or '-'))}"
                f"{league_line}"
            )
            deal_market = str(row.get("market_code") or "1X2")
            deal_sel = str(row.get("selection_code") or "-")
            deal_line = row.get("line")
            deal_key = f"DEAL|f{int(fid)}|{deal_market}|{deal_sel}" if fid is not None else ""
            sel_label = _selection_display(deal_sel, deal_market, deal_line)
            instruction = _bet_instruction(deal_market, deal_sel, deal_line)
            rendered_rows.append(
                "<tr>"
                + _prise_button(
                    title="Prendre ce deal",
                    subtitle=(
                        f"{str(row.get('home_team_name') or '')} vs "
                        f"{str(row.get('away_team_name') or '')} - {sel_label} "
                        f"@ {row.get('market_odd', '-')}"
                    ),
                    key=deal_key,
                    suggested_odd=row.get("market_odd"),
                    position_count=row.get("position_count"),
                    selection_label=sel_label,
                    line=deal_line,
                    return_league=league,
                    can_write=can_manage_positions(user),
                )
                + f"<td class='num'><strong>{escape(str(row.get('rank_position') or '-'))}</strong></td>"
                f"<td>{match_label}</td>"
                f"<td class='muted'>{format_timestamp(row.get('kickoff_utc'))}</td>"
                f"<td>{escape(str(row.get('bookmaker_name') or '-'))}</td>"
                f"<td>{_pick_chip(row.get('selection_code'), market_code=row.get('market_code'), line=row.get('line'))}"
                f"<div class='muted' style='font-size:11px;margin-top:4px'>{escape(instruction)}</div></td>"
                f"<td class='num'>{row.get('model_pct', '-')}</td>"
                f"<td class='num'>{row.get('implied_pct', '-')}</td>"
                + _edge_cell(edge_pct) +
                f"<td class='num'><strong>{row.get('market_odd', '-')}</strong></td>"
                f"<td class='num'>{row.get('fair_odd', '-')}</td>"
                f"<td class='num'>{stake if stake is not None else '-'}</td>"
                f"<td class='num'>{row.get('ranking_pct', '-')}</td>"
                f"<td>{link}</td>"
                "</tr>"
            )
            explication = row.get("explication")
            if explication:
                rendered_rows.append(
                    "<tr class='explain-row'><td colspan='14'>"
                    "<details><summary>Pourquoi ce deal</summary>"
                    f"<p>{escape(str(explication))}</p></details></td></tr>"
                )
        body = "".join(rendered_rows)
    table = (
        "<div class='table-wrap'>"
        f"<table><thead><tr>{headers}</tr></thead><tbody>{body}</tbody></table>"
        "</div>"
    )
    return render_section(
        "02", "Deals classes", table,
        note="Entre la cote reelle prise et la mise quand tu joues un deal. Edge rouge = value detectee. Mise affichee = fraction Kelly theorique en % de bankroll.",
    )


def render_section(number: str, title: str, inner_html: str, note: str = "",
                   anchor: str = "") -> str:
    note_html = f"<span class='note'>{escape(note)}</span>" if note else ""
    id_attr = f" id='{escape(anchor)}'" if anchor else ""
    return (
        f"<section class='section'{id_attr}>"
        f"<div class='section-head'><span class='no'>{escape(number)}</span>"
        f"<h2>{escape(title)}</h2>{note_html}</div>"
        f"{inner_html}"
        "</section>"
    )


# Onglets partages par sport (structure « façon foot » sur chaque sport).
def render_sport_nav(sport: str, active_tab: str, user: UserContext | None = None) -> str:
    """Barre unifiee : selecteur de sport + onglets Prediction/Deal/Strategie/Back."""
    sports = [("football", "Football", "/"),
              ("golf", "Golf", f"/?sport={GOLF_SPORT_PARAM}")]
    sport_pills = "".join(
        f"<a class='sport-pill{' active' if sport == key else ''}' href='{href}'>{label}</a>"
        for key, label, href in sports
    )
    if sport == "golf":
        g = f"/?sport={GOLF_SPORT_PARAM}"
        tabs = [("predictions", "Predictions", f"{g}&view=predictions"),
                ("deals", "Deals", f"{g}&view=deals"),
                ("strategie", "Strategie", f"/bankroll?sport={GOLF_SPORT_PARAM}"),
                ("back", "Back", f"/back?sport={GOLF_SPORT_PARAM}")]
    else:
        tabs = [("predictions", "Predictions", "/?view=predictions"),
                ("deals", "Deals", "/?view=deals"),
                ("strategie", "Strategie", "/bankroll"),
                ("back", "Back", "/back?sport=football")]
    tabs.append(("bankroll", "Bankroll", f"/bankroll/account?sport={GOLF_SPORT_PARAM}" if sport == "golf" else "/bankroll/account?sport=football"))
    if user and user.is_admin:
        tabs.append(("admin", "Admin", "/admin"))
    tab_pills = "".join(
        f"<a class='nav-tab{' active' if active_tab == key else ''}' href='{href}'>{label}</a>"
        for key, label, href in tabs
    )
    account_label = escape(user.display_name if user else "Espace client")
    role_label = escape(user.role_code if user else "CLIENT")
    logout_link = "<a class='nav-tab' href='/logout'>Sortir</a>" if user else ""
    return (
        "<nav class='sportnav'>"
        "<div class='sport-switch'>"
        "<a class='brand' href='/'><b><i>BP</i>REDICTION</b><small>Sports Trading Intelligence</small></a>"
        f"{sport_pills}</div>"
        f"<div class='nav-primary'><div class='nav-tabs'>{tab_pills}</div></div>"
        "<div class='nav-access'>"
        f"<span class='account-chip active'>{account_label} · {role_label}</span>"
        f"{logout_link}"
        "</div>"
        "</nav>"
    )


def render_product_footer(sport: str = "football") -> str:
    sport_label = "Golf" if sport == GOLF_SPORT_PARAM else "Football"
    return (
        "<footer class='product-footer'>"
        "<div>"
        "<h2>Cadre legal</h2>"
        "<p><strong>BPREDICTION</strong> fournit des analyses probabilistes et un track record, "
        "pas une garantie de gain. Les paris comportent un risque de perte; utilise le produit "
        "uniquement si tu es majeur et autorise a parier dans ta juridiction.</p>"
        "<div class='footer-line'>"
        "<span class='footer-tag'>Jeu responsable</span>"
        "<span class='footer-tag'>Decision utilisateur</span>"
        "<span class='footer-tag'>V1 auditable</span>"
        "</div>"
        "</div>"
        "<div>"
        "<h2>Maintenance</h2>"
        f"<p>Mode actif : <strong>{escape(sport_label)}</strong>. Les donnees, cotes et resultats "
        "dependent des fournisseurs API; chaque changement de schema doit passer par migration, "
        "test smoke et validation back.</p>"
        "</div>"
        "<div>"
        "<h2>Vision produit</h2>"
        "<p>Interface client d'aide a la decision : predictions, deals, strategie de mise et "
        "performance verifiee doivent rester separes, lisibles et coherents sur toutes les pages.</p>"
        "</div>"
        "</footer>"
    )


def render_login_page(error: str = "", next_url: str = "/") -> str:
    error_html = f"<div class='report error'><strong>{escape(error)}</strong></div>" if error else ""
    return f"""<!DOCTYPE html>
<html lang="fr">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Connexion - BPREDICTION</title>
  <style>{BASE_CSS}</style>
</head>
<body>
  <main class="shell">
    <header class="masthead hero-auth">
      <div>
        <h1>Le sport, traite comme un marche.</h1>
        <p class="sub">Predictions probabilistes, value bets detectes, bankroll et track record verifie
        &mdash; football et golf. Connecte-toi a ton desk. <a class="detail-link" href="/signup">Ouvrir un compte</a></p>
      </div>
    </header>
    <div class="verdict-strip">
      <div class="cell"><div class="v">8</div><div class="l">Marches football</div></div>
      <div class="cell"><div class="v">Mondial</div><div class="l">Circuit golf couvert</div></div>
      <div class="cell"><div class="v">Kelly</div><div class="l">Gestion de bankroll</div></div>
      <div class="cell"><div class="v">Verifie</div><div class="l">Reglement automatique</div></div>
    </div>
    {error_html}
    <section class="section">
      <div class="section-head"><span class="no">01</span><h2>Acces a ton desk</h2><span class="note">Compte client ou administrateur.</span></div>
      <form method="post" action="/login" class="filterbar">
        <input type="hidden" name="next" value="{escape(_safe_next(next_url))}">
        <label>Email<input name="email" type="email" autocomplete="username" required></label>
        <label>Mot de passe<input name="password" type="password" autocomplete="current-password" required></label>
        <div class="filter-actions"><button type="submit" class="button-primary">Se connecter</button>
        <a class="detail-link" href="/signup">Pas encore de compte ?</a></div>
      </form>
      <p class="score-note">Chaque session est chiffree et journalisee. Acces reserve aux comptes autorises.</p>
    </section>
    {render_product_footer("football")}
  </main>
{dev_reload_script()}
</body>
</html>"""


def render_signup_page(error: str = "") -> str:
    error_html = f"<div class='report error'><strong>{escape(error)}</strong></div>" if error else ""
    return f"""<!DOCTYPE html>
<html lang="fr">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Creer un compte - BPREDICTION</title>
  <style>{BASE_CSS}</style>
</head>
<body>
  <main class="shell">
    <header class="masthead hero-auth golf">
      <div>
        <h1>Ouvre ton desk BPREDICTION.</h1>
        <p class="sub">Ton espace personnel : predictions, deals, strategie de mise, bankroll et back verifie.
        Chaque pari que tu prends est date, chiffre et regle automatiquement.
        <a class="detail-link" href="/login">Deja un compte ? Se connecter</a></p>
      </div>
    </header>
    {error_html}
    <section class="section">
      <div class="section-head"><span class="no">01</span><h2>Informations du compte</h2>
      <span class="note">Le role Administrateur exige la cle secrete de l'entreprise.</span></div>
      <form method="post" action="/signup" class="filterbar">
        <label>Nom affiche<input name="display_name" type="text" autocomplete="name" required></label>
        <label>Email<input name="email" type="email" autocomplete="username" required></label>
        <label>Mot de passe<input name="password" type="password" autocomplete="new-password" required minlength="8"></label>
        <label>Confirmer le mot de passe<input name="password_confirm" type="password" autocomplete="new-password" required minlength="8"></label>
        <label>Role
          <select name="role_code">
            <option value="CLIENT" selected>Client</option>
            <option value="ADMIN">Administrateur</option>
          </select>
        </label>
        <label>Cle administrateur (si role admin)
          <input name="admin_key" type="password" autocomplete="off" placeholder="requise pour Administrateur"></label>
        <div class="filter-actions"><button type="submit" class="button-primary">Creer mon compte</button>
        <a class="detail-link" href="/login">Annuler</a></div>
      </form>
      <p class="score-note">La cle administrateur est definie par l'entreprise (ADMIN_SIGNUP_KEY).
      Un compte client n'en a pas besoin.</p>
    </section>
    {render_product_footer("football")}
  </main>
{dev_reload_script()}
</body>
</html>"""


def render_run_cards(runs: list[dict[str, Any]], model_run: dict[str, Any] | None) -> str:
    provider_cards = []
    for run in runs:
        run_scope = str(run.get("run_scope") or run.get("endpoint_code") or "-")
        elapsed_seconds = int(run.get("elapsed_seconds") or 0)
        elapsed_label = (
            f"{elapsed_seconds // 3600}h {(elapsed_seconds % 3600) // 60:02d}m"
            if elapsed_seconds >= 3600
            else f"{elapsed_seconds // 60}m {elapsed_seconds % 60:02d}s"
            if elapsed_seconds >= 60
            else f"{elapsed_seconds}s"
        )
        status_label = str(run.get("status_code") or "-")
        if run.get("is_stale"):
            status_label += " (STALE)"
        error_message = str(run.get("error_message") or "").strip()
        error_html = (
            f"<p class='muted' style='font-size:12px'>{escape(error_message[:180])}</p>"
            if error_message else ""
        )
        provider_cards.append(
            (
                "<div class='run-card'>"
                f"<h3>{escape(str(run.get('provider_name') or 'Provider'))}</h3>"
                f"<p>Statut <strong>{escape(status_label)}</strong></p>"
                f"<p>Scope <strong>{escape(run_scope)}</strong></p>"
                f"<p>Debut <strong>{format_timestamp(run.get('started_at'))}</strong></p>"
                f"<p>Fin <strong>{format_timestamp(run.get('finished_at'))}</strong></p>"
                f"<p>Heartbeat <strong>{format_timestamp(run.get('heartbeat_at'))}</strong></p>"
                f"<p>Duree <strong>{escape(elapsed_label)}</strong></p>"
                f"<p>Recus <strong>{escape(str(run.get('records_received') or 0))}</strong> / "
                f"ecrits <strong>{escape(str(run.get('records_written') or 0))}</strong></p>"
                f"<p>Run <strong>{escape(str(run.get('application_name') or '-'))}</strong> "
                f"via <strong>{escape(str(run.get('trigger_source') or '-'))}</strong></p>"
                f"<p>Host <strong>{escape(str(run.get('host_name') or '-'))}</strong></p>"
                f"{error_html}"
                "</div>"
            )
        )

    if model_run:
        provider_cards.append(
            "<div class='run-card'>"
            f"<h3>{escape(str(model_run.get('model_name') or 'Modele'))}"
            f" <span class='sig'>v{escape(str(model_run.get('model_version') or '-'))}</span></h3>"
            f"<p>Statut <strong>{escape(str(model_run.get('status_code') or '-'))}</strong></p>"
            f"<p>Debut <strong>{format_timestamp(model_run.get('started_at'))}</strong></p>"
            f"<p>Fin <strong>{format_timestamp(model_run.get('finished_at'))}</strong></p>"
            "</div>"
        )

    return render_section(
        "04", "Pilotage",
        f"<div class='run-grid'>{''.join(provider_cards)}</div>",
        note="Derniers runs d'ingestion et de modele.",
    )


def render_action_report(report: ActionReport | None, error_message: str | None) -> str:
    if error_message:
        return (
            "<section class='flash flash-error'>"
            "<h2>Execution en erreur</h2>"
            f"<pre>{escape(error_message)}</pre>"
            "</section>"
        )
    if report is None:
        return ""
    payload = json.dumps(report.payload, indent=2, ensure_ascii=True)
    return (
        f"<section class='flash flash-{escape(report.status)}'>"
        f"<h2>{escape(report.title)}</h2>"
        f"<pre>{escape(payload)}</pre>"
        "</section>"
    )


def render_controls(
    selected_league: str,
    leagues: list[str],
    horizon_code: str = DEFAULT_HORIZON,
    view: str = "predictions",
    user: UserContext | None = None,
) -> str:
    # "Toutes competitions" et "Faits divers" en tete : les deux modes speciaux.
    league_options = [
        f"<option value='{ALL_LEAGUES}'"
        f"{' selected' if selected_league == ALL_LEAGUES else ''}>"
        f"{escape(ALL_LEAGUES_LABEL)}</option>",
        f"<option value='{FAITS_DIVERS}'"
        f"{' selected' if selected_league == FAITS_DIVERS else ''}>"
        f"{escape(FAITS_DIVERS_LABEL)}</option>",
    ]
    for league in leagues:
        selected = " selected" if league == selected_league else ""
        league_options.append(
            f"<option value='{escape(league)}'{selected}>{escape(league)}</option>"
        )
    league_options_html = "".join(league_options)

    buttons = [
        ("sync_reference", "Sync football"),
        ("sync_odds", "Sync cotes 1X2"),
        ("run_predictions", "Lancer predictions"),
        ("full_refresh", "Cycle complet V1"),
    ]
    action_buttons = []
    if has_permission(user, "CYCLE_RUN") and background_actions_enabled():
        for action, label in buttons:
            css_class = " class='primary'" if action == "run_predictions" else ""
            action_buttons.append(
                f"<button type='submit' name='action'{css_class} "
                f"value='{escape(action)}'>{escape(label)}</button>"
            )
    orchestration_hint = (
        f"<p class='client-hint'><strong>Mode Vercel :</strong> {escape(background_actions_disabled_message())}</p>"
        if has_permission(user, "CYCLE_RUN") and not background_actions_enabled()
        else ""
    )

    view_param = "deals" if view == "deals" else "predictions"
    return (
        "<header class='masthead'>"
        "<div>"
        "<h1>Sports Prediction Engine</h1>"
        "<p class='sub'>Poste de decision football pour lire les predictions, classer les deals, prendre position "
        "et verifier la performance avec un track record auditable.</p>"
        "</div>"
        "<div class='hero-visual football' aria-hidden='true'><span class='ball'></span></div>"
        "</header>"
        "<div class='actions'>"
        "<form method='get'>"
        f"<input type='hidden' name='view' value='{escape(view_param)}' />"
        f"<input type='hidden' name='horizon' value='{escape(horizon_code)}' />"
        f"<select id='league' name='league' aria-label='Competition'>{league_options_html}</select>"
        "<button type='submit'>Afficher</button>"
        "</form>"
        + (
            "<form method='post'>"
            f"<input type='hidden' name='view' value='{escape(view_param)}' />"
            f"<input type='hidden' name='league' value='{escape(selected_league)}' />"
            f"<input type='hidden' name='horizon' value='{escape(horizon_code)}' />"
            + "".join(action_buttons)
            + "</form>"
            if action_buttons else ""
        )
        + orchestration_hint
        + "</div>"
    )


def render_page(selected_league: str, data: dict[str, Any], report: ActionReport | None, error_message: str | None, user: UserContext | None = None) -> str:
    active_view = str(data.get("view") or "predictions").strip().lower()
    if active_view not in ("predictions", "deals"):
        active_view = "predictions"
    metrics_html = "".join(render_metric(metric) for metric in data["metrics"])
    runs_html = render_run_cards(data["runs"], data["model_run"])
    report_html = render_action_report(report, error_message)
    controls_html = render_controls(
        selected_league,
        data["leagues"],
        str(data.get("horizon") or DEFAULT_HORIZON),
        active_view,
        user,
    )
    filters: dict[str, Any] = data.get("filters") or {
        "horizon": str(data.get("horizon") or DEFAULT_HORIZON),
        "smin": 0, "cmin": 0, "sort": DEFAULT_SORT, "dir": "asc",
    }
    horizon_code = str(filters.get("horizon") or DEFAULT_HORIZON)
    horizon_label = HORIZONS.get(horizon_code, HORIZONS[DEFAULT_HORIZON])[0]

    period_chips = "".join(
        (
            f"<a class='period-chip{' active' if code == horizon_code else ''}' "
            f"href='{_query_string(selected_league, filters, horizon=code)}'>{escape(label)}</a>"
        )
        for code, (label, _hours) in HORIZONS.items()
    )

    def _threshold_options(options: tuple[int, ...], current: int, none_label: str) -> str:
        rendered = []
        for value in options:
            label = none_label if value == 0 else f"{value} % et plus"
            selected = " selected" if value == current else ""
            rendered.append(f"<option value='{value}'{selected}>{label}</option>")
        return "".join(rendered)

    thresholds_form = (
        "<form method='get' class='thresholds'>"
        f"<input type='hidden' name='view' value='{escape(active_view)}' />"
        f"<input type='hidden' name='league' value='{escape(selected_league)}' />"
        f"<input type='hidden' name='horizon' value='{escape(horizon_code)}' />"
        f"<input type='hidden' name='sort' value='{escape(str(filters.get('sort') or DEFAULT_SORT))}' />"
        f"<input type='hidden' name='dir' value='{escape(str(filters.get('dir') or 'asc'))}' />"
        "<label for='smin'>Surete min</label>"
        f"<select id='smin' name='smin'>{_threshold_options(SURETE_OPTIONS, int(filters.get('smin') or 0), 'Toutes')}</select>"
        "<label for='cmin'>Confiance min</label>"
        f"<select id='cmin' name='cmin'>{_threshold_options(CONFIANCE_OPTIONS, int(filters.get('cmin') or 0), 'Toutes')}</select>"
        "<button type='submit'>Filtrer</button>"
        "</form>"
    )

    filters_html = (
        "<div class='periodbar'>"
        "<span class='label'>Periode</span>"
        f"{period_chips}"
        f"{thresholds_form}"
        "</div>"
    )
    matches_html = render_predictions_table(
        data["upcoming_matches"],
        f"Aucun match a venir ne passe les filtres actuels (horizon « {horizon_label} »).",
        league=selected_league,
        filters=filters,
        user=user,
    )
    deals_html = render_deals_table(
        data["ranked_deals"],
        "Aucun deal encore classe pour cette competition.",
        league=selected_league,
        user=user,
    )

    # --- Section 03 : Marche et performance (courbes trading) -------------
    market_parts: list[str] = []
    eq_pts = data.get("equity_points") or []
    if len(eq_pts) >= 2:
        # Axe X = numero de pari (norme des track records) : les reglements
        # arrivent par lots au meme horodatage, un axe temporel s'ecraserait
        # en ligne verticale. Depart force a zero.
        series: list[tuple[float, float]] = [(0.0, 0.0)]
        total = 0.0
        for i, r in enumerate(eq_pts, start=1):
            total += float(r["profit_units"])
            series.append((float(i), total))
        first_label = "pari 1 (" + format_timestamp(eq_pts[0]["settled_at"])[:10] + ")"
        last_label = f"pari {len(eq_pts)} (" + format_timestamp(eq_pts[-1]["settled_at"])[:10] + ")"
        market_parts.append(
            "<h3 class='sub-head'>Courbe d'equite - deals regles (mise plate 1 unite)</h3>"
            + svg_line_chart(
                [("Profit cumule", "#0E1B2E", series)],
                height=200, y_format="{:+.2f}",
                x_labels=(first_label, last_label),
            )
        )
    try:
        movers = elo_movers(days=30, top_n=5)
    except Exception:
        movers = []
    if movers:
        rows_html = "".join(
            "<tr>"
            f"<td>{escape(m['name'])}</td>"
            f"<td class='num'><strong>{m['rating']:.0f}</strong></td>"
            f"<td class='num {'sig' if m['delta'] >= 0 else 'muted'}'>{m['delta']:+.0f}</td>"
            f"<td>{svg_sparkline(m['spark'], 130, 26)}</td>"
            "</tr>"
            for m in movers
        )
        market_parts.append(
            "<h3 class='sub-head'>Mouvements Elo - 30 jours (hausses puis baisses)</h3>"
            "<div class='table-wrap'><table>"
            "<thead><tr><th>Equipe</th><th class='num'>Elo</th><th class='num'>Delta 30j</th><th>20 derniers matchs</th></tr></thead>"
            f"<tbody>{rows_html}</tbody></table></div>"
        )
    market_html = (
        render_section(
            "03", "Marche et performance",
            "".join(market_parts),
            note="Courbes calculees sur les donnees reelles: deals regles et replay Elo complet.",
        )
        if market_parts else ""
    )

    # Track record reel (deals regles) — optionnel, absent en mode degrade.
    perf = data.get("performance")
    perf_html = ""
    if perf and perf.get("bets_settled"):
        roi = _to_float(perf.get("roi_pct"))
        roi_class = " accent" if roi is not None and roi > 0 else " negative"
        perf_html = (
            "<div class='metrics'>"
            f"<div class='metric'><div class='value'>{escape(str(perf.get('bets_settled')))}</div>"
            "<div class='label'>Deals regles</div></div>"
            f"<div class='metric'><div class='value'>{escape(str(perf.get('win_rate_pct')))}</div>"
            "<div class='label'>Taux de reussite %</div></div>"
            f"<div class='metric{roi_class}'><div class='value'>{escape(str(perf.get('roi_pct')))}</div>"
            "<div class='label'>ROI % (mise plate 1 unite)</div></div>"
            f"<div class='metric'><div class='value'>{escape(str(perf.get('total_profit_units')))}</div>"
            "<div class='label'>Profit cumule (unites)</div></div>"
            "</div>"
        )

    main_sections = matches_html + market_html if active_view == "predictions" else deals_html + market_html

    return f"""<!DOCTYPE html>
<html lang="fr">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Sports Prediction Engine V1</title>
  <style>{BASE_CSS}</style>
</head>
<body>
  <main class="shell">
    {render_sport_nav("football", active_view, user)}
    {controls_html}
    {report_html}
    <div class="metrics">{metrics_html}</div>
    {perf_html}
    {filters_html}
    {main_sections}
    {runs_html}
    {render_product_footer("football")}
  </main>
  {prise_modal_html("/", selected_league)}
{dev_reload_script()}
</body>
</html>"""


def _form_badge(gf: Any, ga: Any) -> str:
    try:
        gf_i, ga_i = int(gf), int(ga)
    except (TypeError, ValueError):
        return "<span class='form-badge form-n'>?</span>"
    if gf_i > ga_i:
        return "<span class='form-badge form-w'>V</span>"
    if gf_i == ga_i:
        return "<span class='form-badge form-n'>N</span>"
    return "<span class='form-badge form-l'>D</span>"


def render_match_detail(data: dict[str, Any]) -> str:
    home = escape(str(data["home_name"]))
    away = escape(str(data["away_name"]))
    pred = data.get("prediction")
    has_value = bool(data["deals"])

    # --- Verdict + rail heros -------------------------------------------
    if pred:
        h = _to_float(pred["home_pct"]) or 0.0
        d = _to_float(pred["draw_pct"]) or 0.0
        a = _to_float(pred["away_pct"]) or 0.0
        pronostic = str(pred.get("pronostic") or "")
        verdict_team = home if pronostic == "HOME" else away if pronostic == "AWAY" else "Match nul"
        top_pct = {"HOME": h, "DRAW": d, "AWAY": a}.get(pronostic, max(h, d, a))
        # Meme regle que forecaster.draw_is_competitive (28% et <= 8 pts du max).
        if pronostic != "DRAW" and d >= 28.0 and (max(h, a) - d) <= 8.0:
            verdict_team += " - nul tres possible"
        rail = render_probability_rail(h, d, a, lead_code=pronostic or None,
                                       has_value=has_value, hero=True)
        rail_block = (
            f"{rail}"
            f"<div class='rail-legend'><span>{home}</span><span>Nul</span><span>{away}</span></div>"
        )
        verdict_no = (
            f"<div class='verdict-no'>{top_pct:.0f}<small>"
            f"{escape(verdict_team)}</small></div>"
        )
        xg_home = _to_float(pred.get("xg_home"))
        xg_away = _to_float(pred.get("xg_away"))
        xg_total = round(xg_home + xg_away, 2) if xg_home is not None and xg_away is not None else "-"
        strip = (
            "<div class='verdict-strip'>"
            f"<div class='cell'><div class='v'>{escape(str(pred.get('surete_pct') or '-'))}</div><div class='l'>Surete %</div></div>"
            f"<div class='cell'><div class='v'>{escape(str(pred['confidence_pct']))}</div><div class='l'>Confiance %</div></div>"
            f"<div class='cell'><div class='v'>{escape(str(pred['xg_home']))}</div><div class='l'>xG {home}</div></div>"
            f"<div class='cell'><div class='v'>{escape(str(pred['xg_away']))}</div><div class='l'>xG {away}</div></div>"
            f"<div class='cell'><div class='v'>{escape(str(xg_total))}</div><div class='l'>xG du match</div></div>"
            + _derived_markets_cells(pred.get("marches_derives")) +
            f"<div class='cell'><div class='v'>v{escape(str(pred['model_version']))}</div><div class='l'>Modele</div></div>"
            f"<div class='cell'><div class='v'>{format_timestamp(pred.get('generated_at'))}</div><div class='l'>Genere</div></div>"
            "</div>"
        )
        explication_html = render_section(
            "01", "Pourquoi ce pronostic",
            f"<p class='prose'>{escape(str(pred.get('explication') or 'Explication non disponible pour cette prediction.'))}</p>",
        )
        exact_scores = _exact_scores_for_row(pred, top_n=5)
        exact_score_note = str(
            pred.get("exact_score_note")
            or "Le score exact combine xG, probabilites 1X2, biais de nul, absences et signaux de correlation valides."
        )
        exact_scores_html = render_section(
            "02", "Scores exacts probables",
            f"{render_exact_score_stack(exact_scores)}<p class='score-note'>{escape(exact_score_note)}</p>",
        )
    else:
        rail_block = ""
        verdict_no = ""
        strip = "<p class='empty'>Aucune prediction pour ce match - lance le pipeline.</p>"
        explication_html = ""
        exact_scores_html = ""

    # --- Correlations ------------------------------------------------------
    insight_rows = []
    for ins in data["insights"]:
        badge = ("<span class='badge-valid'>VALIDEE</span>" if ins["is_validated"]
                 else "<span class='badge-info'>indicatif</span>")
        effect = f"{ins['effect_value']}"
        if ins["baseline_value"] is not None:
            effect += f" vs {ins['baseline_value']}"
        samples = f"{ins['sample_with']}" + (
            f"/{ins['sample_without']}" if ins["sample_without"] is not None else ""
        )
        insight_rows.append(
            f"<tr><td>{badge}</td><td>{escape(str(ins['subject_label']))}</td>"
            f"<td class='num'>{escape(effect)}</td><td class='num'>{escape(samples)}</td></tr>"
        )
    insights_body = "".join(insight_rows) or (
        "<tr><td colspan='4' class='empty'>Aucune correlation detectee pour ces equipes "
        "- le scan s'enrichit avec le backfill des compositions.</td></tr>"
    )
    insights_html = render_section(
        "03", "Correlations detectees",
        "<div class='table-wrap'><table>"
        "<thead><tr><th>Statut</th><th>Correlation</th><th class='num'>Effet</th><th class='num'>Matchs</th></tr></thead>"
        f"<tbody>{insights_body}</tbody></table></div>",
        note="Seules les correlations VALIDEES (test + correction FDR) influencent les probabilites.",
    )

    # --- Forme -------------------------------------------------------------
    def form_block(team_name: str, matches: list[dict[str, Any]]) -> str:
        rows_html = "".join(
            f"<tr><td style='width:36px'>{_form_badge(m['gf'], m['ga'])}</td>"
            f"<td class='num' style='width:56px'><strong>{escape(str(m['gf']))}-{escape(str(m['ga']))}</strong></td>"
            f"<td>{escape(str(m['opponent']))}</td>"
            f"<td class='muted'>{format_timestamp(m['kickoff_utc'])[:10]}</td></tr>"
            for m in matches
        ) or "<tr><td colspan='4' class='empty'>Pas d'historique.</td></tr>"
        # Sparkline de forme : points par match, du plus ancien au plus recent.
        points_chrono = [
            3.0 if int(m["gf"]) > int(m["ga"]) else 1.0 if int(m["gf"]) == int(m["ga"]) else 0.0
            for m in reversed(matches)
            if m.get("gf") is not None and m.get("ga") is not None
        ]
        spark = svg_sparkline(points_chrono, 110, 26) if len(points_chrono) >= 2 else ""
        return (
            "<div><h3 style='font-size:15px;font-weight:700;margin:14px 0 6px;display:flex;justify-content:space-between;align-items:center'>"
            f"<span>{escape(team_name)}</span>{spark}</h3>"
            f"<table><tbody>{rows_html}</tbody></table></div>"
        )

    # --- Trajectoire Elo (24 mois) — la courbe "cours de bourse" des équipes
    elo_html = ""
    try:
        history, _names = get_elo_history()
        cutoff_ts = _time.time() - 730 * 86400
        home_series = [(ts, r) for ts, r in history.get(int(data["home_id"]), []) if ts >= cutoff_ts]
        away_series = [(ts, r) for ts, r in history.get(int(data["away_id"]), []) if ts >= cutoff_ts]
        if len(home_series) >= 2 or len(away_series) >= 2:
            from datetime import datetime as _dt
            all_ts = [ts for ts, _r in home_series + away_series]
            x_labels = (
                _dt.fromtimestamp(min(all_ts)).strftime("%Y-%m"),
                _dt.fromtimestamp(max(all_ts)).strftime("%Y-%m"),
            )
            elo_html = render_section(
                "04", "Trajectoire Elo - 24 mois",
                svg_line_chart(
                    [
                        (str(data["home_name"]), "#0E1B2E", home_series),
                        (str(data["away_name"]), "#7C8DA8", away_series),
                    ],
                    height=240, y_format="{:.0f}", x_labels=x_labels,
                ),
                note="Elo ajuste adversaire, recalcule apres chaque match (replay complet de l'historique).",
            )
    except Exception:
        elo_html = ""

    form_html = render_section(
        "05", "Forme recente",
        "<div class='grid2'>"
        + form_block(str(data["home_name"]), data["home_form"])
        + form_block(str(data["away_name"]), data["away_form"])
        + "</div>",
        note="8 derniers matchs. Carre plein = victoire, gris = nul, filet = defaite. Courbe = points par match.",
    )

    # --- Head to head --------------------------------------------------------
    h2h_rows = "".join(
        f"<tr><td class='muted'>{format_timestamp(m['kickoff_utc'])[:10]}</td>"
        f"<td>{escape(str(m['home']))}</td>"
        f"<td class='num'><strong>{escape(str(m['home_score']))} - {escape(str(m['away_score']))}</strong></td>"
        f"<td>{escape(str(m['away']))}</td>"
        f"<td class='muted'>{escape(str(m['league_name']))}</td></tr>"
        for m in data["h2h"]
    ) or "<tr><td colspan='5' class='empty'>Aucune confrontation directe en base.</td></tr>"
    h2h_html = render_section(
        "06", "Confrontations directes",
        "<div class='table-wrap'><table>"
        "<thead><tr><th>Date</th><th>Domicile</th><th class='num'>Score</th><th>Exterieur</th><th>Competition</th></tr></thead>"
        f"<tbody>{h2h_rows}</tbody></table></div>",
    )

    # --- Cotes ----------------------------------------------------------------
    best_home = max((float(o["home_odd"]) for o in data["odds"]), default=0)
    best_draw = max((float(o["draw_odd"]) for o in data["odds"]), default=0)
    best_away = max((float(o["away_odd"]) for o in data["odds"]), default=0)

    def _odd_cell(value: Any, best: float) -> str:
        cls = "num best-odd" if float(value) == best else "num"
        return f"<td class='{cls}'>{escape(str(value))}</td>"

    odds_rows = "".join(
        f"<tr><td>{escape(str(o['bookmaker_name']))}</td>"
        + _odd_cell(o["home_odd"], best_home)
        + _odd_cell(o["draw_odd"], best_draw)
        + _odd_cell(o["away_odd"], best_away)
        + f"<td class='muted'>{format_timestamp(o['captured_at'])}</td></tr>"
        for o in data["odds"]
    ) or "<tr><td colspan='5' class='empty'>Aucune cote en base pour ce match.</td></tr>"
    # Evolution du marche : cote mediane des books par heure de capture.
    odds_history = data.get("odds_history") or []
    odds_chart = ""
    if len(odds_history) >= 2:
        def _series(col: str):
            return [
                (row["bucket"].timestamp(), float(row[col]))
                for row in odds_history if row.get(col) is not None
            ]
        odds_chart = (
            "<h3 class='sub-head'>Evolution du marche (cote mediane par heure de capture)</h3>"
            + svg_line_chart(
                [
                    (home, "#0E1B2E", _series("home_odd")),
                    ("Nul", "#B9C6DA", _series("draw_odd")),
                    (away, "#7C8DA8", _series("away_odd")),
                ],
                height=200, y_format="{:.2f}",
                x_labels=(
                    format_timestamp(odds_history[0]["bucket"])[:16],
                    format_timestamp(odds_history[-1]["bucket"])[:16],
                ),
            )
        )

    odds_html = render_section(
        "07", f"Cotes 1X2 - {len(data['odds'])} bookmakers",
        odds_chart
        + "<div class='table-wrap'><table>"
        f"<thead><tr><th>Bookmaker</th><th class='num'>{home}</th><th class='num'>Nul</th>"
        f"<th class='num'>{away}</th><th>Capture</th></tr></thead>"
        f"<tbody>{odds_rows}</tbody></table></div>",
        note="Meilleure cote de chaque colonne en noir plein.",
    )

    # --- Value bets -------------------------------------------------------------
    deal_rows = "".join(
        f"<tr><td>{escape(str(dl['bookmaker_name']))}</td>"
        f"<td>{_pick_chip(dl['selection_code'], market_code=dl.get('market_code'))}</td>"
        f"<td class='num'>{dl['model_pct']}</td>"
        f"<td class='num'>{dl['implied_pct']}</td>"
        + _edge_cell(_to_float(dl['edge_pct'])) +
        f"<td class='num'><strong>{dl['market_odd']}</strong></td>"
        f"<td class='num'>{dl['fair_odd']}</td>"
        f"<td class='num'>{dl['score_pct']}</td></tr>"
        for dl in data["deals"]
    ) or (
        "<tr><td colspan='8' class='empty'>Aucun value bet sur ce match "
        "- normal quand le marche est bien price.</td></tr>"
    )
    deals_html = render_section(
        "08", "Value bets sur ce match",
        "<div class='table-wrap'><table>"
        "<thead><tr><th>Bookmaker</th><th>Selection</th><th class='num'>Modele</th>"
        "<th class='num'>Marche</th><th class='num'>Edge</th><th class='num'>Cote</th>"
        "<th class='num'>Fair</th><th class='num'>Score</th></tr></thead>"
        f"<tbody>{deal_rows}</tbody></table></div>",
    )

    # --- Marche buteur ----------------------------------------------------------
    scorer_rows = "".join(
        "<tr>"
        f"<td><strong>{escape(str(s['player_name']))}</strong>"
        f"<div class='muted' style='font-size:12px'>{escape(str(s['team_name'] or ''))}</div></td>"
        f"<td class='num'><strong>{s['proba_pct']}</strong></td>"
        + (
            f"<td class='num'>{float(s['anytime_odd']):.2f}</td>"
            if s.get("anytime_odd") else "<td class='num muted'>-</td>"
        )
        + _edge_cell(_to_float(s.get("edge_pct")))
        + f"<td class='muted'>{escape(str(s['bookmaker_name'] or '-'))}</td>"
        "</tr>"
        for s in (data.get("scorers") or [])
    ) or (
        "<tr><td colspan='5' class='empty'>Pas de cotes buteur sur ce match.</td></tr>"
    )
    scorers_html = render_section(
        "09", "Marche buteur - probabilite de marquer",
        "<div class='table-wrap'><table>"
        "<thead><tr><th>Joueur</th><th class='num'>Proba modele %</th>"
        "<th class='num'>Cote</th><th class='num'>Edge</th><th>Bookmaker</th></tr></thead>"
        f"<tbody>{scorer_rows}</tbody></table></div>",
        note="P(marque >= 1) = part de buts du joueur x xG attendu de son equipe (Poisson). Edge rouge = value detectee vs la cote 'buteur a tout moment'.",
    )

    kickoff = format_timestamp(data["kickoff_utc"])
    league = escape(str(data["league_name"]))

    return f"""<!DOCTYPE html>
<html lang="fr">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{home} vs {away} - Analyse</title>
  <style>{BASE_CSS}</style>
</head>
<body>
  <main class="shell">
    <a class="back" href="/">Retour au tableau de bord</a>
    <header class="matchline">
      <div>
        <h1>{home} <span style="color:var(--muted)">vs</span> {away}</h1>
        <p class="meta">{league} - {escape(kickoff)}</p>
      </div>
      {verdict_no}
    </header>
    {strip}
    <div style="margin-top:24px">{rail_block}</div>
    {explication_html}
    {exact_scores_html}
    {insights_html}
    {elo_html}
    {form_html}
    {h2h_html}
    {odds_html}
    {deals_html}
    {scorers_html}
    {render_product_footer("football")}
  </main>
{dev_reload_script()}
</body>
</html>"""


SAFE_PICK_SURETY_FLOOR = 0.62


def load_bankroll_inputs(
    days: int = 30,
    competition: str | None = None,
    user_id: int = 1,
) -> tuple[list[dict[str, Any]], float]:
    """Positions candidates du portefeuille : deals actifs (value bets) +
    pronostics SURS sans deal (surete elevee, EV >= 0 chez le meilleur book).

    Le melange demande par la strategie : les deals portent l'edge, les
    pronostics surs ancrent le portefeuille sur des issues probables — mais
    seulement quand leur esperance n'est pas negative (on ne paie jamais la
    marge du book pour du confort).
    """
    connection = connect_db(DatabaseSettings.from_env())
    try:
        with connection.cursor() as cursor:
            rows = fetch_dicts(
                cursor,
                """
                SELECT DISTINCT ON (vb.fixture_id)
                    vb.fixture_id,
                    ht.team_name AS home_team_name,
                    at.team_name AS away_team_name,
                    vb.market_code,
                    vb.selection_code,
                    vb.model_probability,
                    vb.implied_probability,
                    vb.market_odd,
                    vb.line,
                    d.confidence_score,
                    f.kickoff_utc,
                    b.bookmaker_name,
                    COALESCE(pos.position_count, 0) AS position_count,
                    pos.taken_odd,
                    pos.stake_amount
                FROM model.value_bets vb
                JOIN core.fixtures f ON f.fixture_id = vb.fixture_id
                JOIN core.leagues l ON l.league_id = f.league_id
                JOIN core.teams ht ON ht.team_id = f.home_team_id
                JOIN core.teams at ON at.team_id = f.away_team_id
                LEFT JOIN core.bookmakers b ON b.bookmaker_id = vb.bookmaker_id
                LEFT JOIN model.deal_rankings d ON d.value_bet_id = vb.value_bet_id
                LEFT JOIN LATERAL (
                    SELECT COUNT(*)::integer AS position_count,
                           ROUND((SUM(p.stake_amount * p.taken_odd) / NULLIF(SUM(p.stake_amount), 0))::numeric, 4) AS taken_odd,
                           ROUND(SUM(p.stake_amount)::numeric, 4) AS stake_amount
                    FROM model.user_bet_positions p
                    WHERE p.fixture_id = vb.fixture_id
                      AND p.bet_kind = 'DEAL'
                      AND p.market_code = vb.market_code
                      AND p.selection_code = vb.selection_code
                      AND p.user_id = %(user_id)s
                      AND p.deleted_at IS NULL
                      AND (vb.market_code <> 'HANDICAP' OR p.line IS NOT DISTINCT FROM vb.line)
                ) pos ON true
                WHERE vb.status_code = 'ACTIVE'
                  AND vb.result_code IS NULL
                  AND f.kickoff_utc >= now()
                  AND f.kickoff_utc <= now() + (%(days)s * interval '1 day')
                  AND (%(competition)s::text IS NULL OR l.league_name = %(competition)s)
                ORDER BY vb.fixture_id, vb.market_odd DESC
                """,
                {"days": days, "competition": competition, "user_id": user_id},
            )
            # Pronostics surs sans deal actif : la meilleure cote 1X2 dispo
            # sur la selection pronostiquee, avec les 3 cotes du book pour
            # calculer sa probabilite implicite sans marge.
            safe_rows = fetch_dicts(
                cursor,
                """
                SELECT DISTINCT ON (f.fixture_id)
                    f.fixture_id,
                    ht.team_name AS home_team_name,
                    at.team_name AS away_team_name,
                    f.kickoff_utc,
                    p.feature_snapshot_json::jsonb ->> 'pronostic' AS pronostic,
                    (p.feature_snapshot_json::jsonb ->> 'surete')::float AS surete,
                    p.home_win_probability,
                    p.draw_probability,
                    p.away_win_probability,
                    p.confidence_score,
                    o.home_odd, o.draw_odd, o.away_odd,
                    b.bookmaker_name,
                    COALESCE(pos.position_count, 0) AS position_count,
                    pos.taken_odd,
                    pos.stake_amount
                FROM reporting.v_latest_predictions_1x2 lp
                JOIN model.predictions p ON p.prediction_id = lp.prediction_id
                JOIN core.fixtures f ON f.fixture_id = lp.fixture_id
                JOIN core.leagues l ON l.league_id = f.league_id
                JOIN core.teams ht ON ht.team_id = f.home_team_id
                JOIN core.teams at ON at.team_id = f.away_team_id
                JOIN reporting.v_latest_odds_1x2 o ON o.fixture_id = f.fixture_id
                JOIN core.bookmakers b ON b.bookmaker_id = o.bookmaker_id
                LEFT JOIN LATERAL (
                    SELECT COUNT(*)::integer AS position_count,
                           ROUND((SUM(up.stake_amount * up.taken_odd) / NULLIF(SUM(up.stake_amount), 0))::numeric, 4) AS taken_odd,
                           ROUND(SUM(up.stake_amount)::numeric, 4) AS stake_amount
                    FROM model.user_bet_positions up
                    WHERE up.fixture_id = f.fixture_id
                      AND up.bet_kind = 'PRONOSTIC'
                      AND up.market_code = '1X2'
                      AND up.selection_code = (p.feature_snapshot_json::jsonb ->> 'pronostic')
                      AND up.user_id = %(user_id)s
                      AND up.deleted_at IS NULL
                ) pos ON true
                WHERE f.kickoff_utc >= now()
                  AND f.kickoff_utc <= now() + (%(days)s * interval '1 day')
                  AND (%(competition)s::text IS NULL OR l.league_name = %(competition)s)
                  AND (p.feature_snapshot_json::jsonb ->> 'surete')::float >= %(surety)s
                  AND NOT EXISTS (
                      SELECT 1 FROM model.value_bets vb
                      WHERE vb.fixture_id = f.fixture_id
                        AND vb.status_code = 'ACTIVE'
                        AND vb.result_code IS NULL
                  )
                ORDER BY f.fixture_id,
                    CASE p.feature_snapshot_json::jsonb ->> 'pronostic'
                        WHEN 'HOME' THEN o.home_odd
                        WHEN 'AWAY' THEN o.away_odd
                        ELSE o.draw_odd
                    END DESC
                """,
                {
                    "days": days,
                    "competition": competition,
                    "surety": SAFE_PICK_SURETY_FLOOR,
                    "user_id": user_id,
                },
            )
            # Rythme de deals des 30 derniers jours (matchs distincts,
            # recommandations reelles : les supersedes VOID sont exclus).
            cursor.execute(
                """
                SELECT COUNT(DISTINCT fixture_id)
                FROM model.value_bets
                WHERE detected_at > now() - interval '30 days'
                  AND (result_code IS NULL OR result_code IN ('WON', 'LOST'))
                """
            )
            bets_per_day = float(cursor.fetchone()[0] or 0) / 30.0

            # Positions LONG TERME (faits divers) : integrees d'office des
            # que la periode couvre leur horizon (>= 30 jours).
            outright_rows = []
            outright_watch = []
            if days >= 30:
                # 1. Deals outright (avec edge) : positions a miser.
                outright_rows = fetch_dicts(
                    cursor,
                    """
                    SELECT od.model_probability, od.implied_probability,
                           od.market_odd, m.market_label, m.deadline_utc,
                           s.subject_label, b.bookmaker_name
                    FROM model.outright_deals od
                    JOIN core.outright_markets m ON m.outright_market_id = od.outright_market_id
                    JOIN core.outright_selections s
                      ON s.outright_selection_id = od.outright_selection_id
                    LEFT JOIN core.bookmakers b ON b.bookmaker_id = od.bookmaker_id
                    LEFT JOIN core.leagues l ON l.league_id = m.league_id
                    WHERE od.status_code = 'ACTIVE' AND od.result_code IS NULL
                      AND (%(competition)s::text IS NULL OR l.league_name = %(competition)s)
                    """,
                    {"competition": competition},
                )
                # 2. Favoris long terme SANS deal : proposes quand meme (le
                #    client veut voir les faits divers meme sans value). Le
                #    favori de chaque marche cote, hors indice Ballon d'Or
                #    (pas un marche jouable).
                outright_watch = fetch_dicts(
                    cursor,
                    """
                    SELECT DISTINCT ON (b.outright_market_id)
                        b.market_label, b.subject_label,
                        ROUND(b.probability * 100, 1) AS model_pct,
                        b.best_odd, b.bookmaker_name, b.market_code,
                        CASE WHEN b.best_odd IS NOT NULL AND b.best_odd > 1.0
                             THEN ROUND((b.probability - 1.0 / b.best_odd) * 100, 1)
                             ELSE NULL END AS edge_pct
                    FROM reporting.v_outright_board b
                    LEFT JOIN core.leagues l ON l.league_name = b.league_name
                    WHERE b.probability IS NOT NULL
                      AND b.market_code <> 'BALLON_DOR_INDEX'
                      AND (%(competition)s::text IS NULL OR b.league_name = %(competition)s)
                      AND NOT EXISTS (
                          SELECT 1 FROM model.outright_deals od
                          WHERE od.outright_market_id = b.outright_market_id
                            AND od.status_code = 'ACTIVE' AND od.result_code IS NULL
                      )
                    ORDER BY b.outright_market_id, b.probability DESC
                    """,
                    {"competition": competition},
                )
    finally:
        connection.close()

    deals = [
        {
            "label": f"{row['home_team_name']} vs {row['away_team_name']}",
            "market_code": row["market_code"],
            "selection_code": row["selection_code"],
            "bookmaker": row["bookmaker_name"],
            "kickoff_utc": row["kickoff_utc"],
            "market_odd": float(row["market_odd"]),
            "line": row.get("line"),
            "model_probability": float(row["model_probability"]),
            "implied_probability": float(row["implied_probability"]),
            "confidence_score": float(row["confidence_score"] or 0.5),
            "source": "VALUE BET",
            "fixture_id": row["fixture_id"],
            "position_count": int(row.get("position_count") or 0),
            "taken_odd": float(row["taken_odd"]) if row.get("taken_odd") is not None else None,
            "stake_amount": float(row["stake_amount"]) if row.get("stake_amount") is not None else None,
        }
        for row in rows
    ]

    for row in safe_rows:
        pronostic = str(row["pronostic"] or "")
        odds = {
            "HOME": float(row["home_odd"]),
            "DRAW": float(row["draw_odd"]),
            "AWAY": float(row["away_odd"]),
        }
        probabilities = {
            "HOME": float(row["home_win_probability"]),
            "DRAW": float(row["draw_probability"]),
            "AWAY": float(row["away_win_probability"]),
        }
        if pronostic not in odds:
            continue
        market_odd = odds[pronostic]
        model_probability = probabilities[pronostic]
        if market_odd <= 1.0:
            continue
        # EV >= 0 obligatoire : un pronostic sur avec esperance negative
        # n'ancre rien, il finance la marge du bookmaker.
        if model_probability * market_odd - 1.0 < 0.0:
            continue
        inverse_sum = sum(1.0 / odd for odd in odds.values() if odd > 1.0)
        implied = (1.0 / market_odd) / inverse_sum if inverse_sum > 0 else 1.0 / market_odd
        deals.append(
            {
                "label": f"{row['home_team_name']} vs {row['away_team_name']}",
                "market_code": "1X2",
                "selection_code": pronostic,
                "bookmaker": row["bookmaker_name"],
                "kickoff_utc": row["kickoff_utc"],
                "market_odd": market_odd,
                "model_probability": model_probability,
                "implied_probability": implied,
                "confidence_score": float(row["confidence_score"] or 0.5),
                "source": "PRONOSTIC SUR",
                "fixture_id": row["fixture_id"],
                "position_count": int(row.get("position_count") or 0),
                "taken_odd": float(row["taken_odd"]) if row.get("taken_odd") is not None else None,
                "stake_amount": float(row["stake_amount"]) if row.get("stake_amount") is not None else None,
            }
        )

    for row in outright_rows:
        deals.append(
            {
                "label": f"{row['market_label']} : {row['subject_label']}",
                "market_code": "OUTRIGHT",
                "selection_code": str(row["subject_label"]),
                "bookmaker": row["bookmaker_name"],
                "kickoff_utc": row["deadline_utc"],
                "market_odd": float(row["market_odd"]),
                "model_probability": float(row["model_probability"]),
                "implied_probability": float(row["implied_probability"]),
                # Horizon long = incertitude structurelle plus grande.
                "confidence_score": 0.45,
                "source": "LONG TERME",
                "fixture_id": None,
            }
        )
    return deals, bets_per_day, outright_watch


_CATEGORY_LABELS = {"SAFE": "Sur", "MODERE": "Modere", "RISQUE": "Risque"}

# Periodes de la strategie de paris : libelle -> nombre de jours.
STRATEGY_PERIODS: dict[str, tuple[str, int]] = {
    "jour": ("1 jour", 1),
    "semaine": ("1 semaine", 7),
    "mois": ("1 mois", 30),
    "trimestre": ("3 mois", 90),
    "annee": ("1 an", 365),
}
DEFAULT_PERIOD = "mois"


def golf_scope_filters(periode: str) -> tuple[dict[str, str], int]:
    """Fenetre temporelle de la strategie golf.

    On garde quelques jours avant aujourd'hui pour les tournois deja demarres
    (golf = evenement multi-jours), puis on borne la fin selon le scope choisi.
    """
    if periode not in STRATEGY_PERIODS:
        periode = DEFAULT_PERIOD
    days = STRATEGY_PERIODS[periode][1]
    today = datetime.now().date()
    return {
        "tour": "all",
        "market": "all",
        "deal_status": "active",
        "date_from": (today - timedelta(days=4)).isoformat(),
        "date_to": (today + timedelta(days=days)).isoformat(),
    }, days


def render_bankroll_page(
    plan: dict[str, Any], montant: float, jours: int, profil: str,
    periode: str = DEFAULT_PERIOD,
    competition: str = "",
    parlays: list[dict[str, Any]] | None = None,
    outright_watch: list[dict[str, Any]] | None = None,
    user: UserContext | None = None,
    bankroll_context: dict[str, Any] | None = None,
    validation_notice: str = "",
) -> str:
    profile = plan["profile"]
    simulation = plan["simulation"]

    profile_options = "".join(
        f"<option value='{escape(p.code)}'{' selected' if p.code == profil else ''}>"
        f"{escape(p.label)}</option>"
        for p in RISK_PROFILES.values()
    )
    period_options = "".join(
        f"<option value='{escape(code)}'{' selected' if code == periode else ''}>"
        f"{escape(label)}</option>"
        for code, (label, _) in STRATEGY_PERIODS.items()
    )
    try:
        connection = connect_db(DatabaseSettings.from_env())
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT DISTINCT l.league_name
                    FROM core.fixtures f
                    JOIN core.leagues l ON l.league_id = f.league_id
                    WHERE f.kickoff_utc >= now()
                    ORDER BY l.league_name
                    """
                )
                competitions = [str(r[0]) for r in cursor.fetchall()]
        finally:
            connection.close()
    except Exception:
        competitions = []
    competition_options = (
        f"<option value=''{' selected' if not competition else ''}>Toutes competitions</option>"
        + "".join(
            f"<option value='{escape(name)}'{' selected' if name == competition else ''}>"
            f"{escape(name)}</option>"
            for name in competitions
        )
    )
    form_html = (
        "<form method='get' action='/bankroll' class='filterbar'>"
        "<input type='hidden' name='recalculate' value='1' />"
        "<label>Budget strategie ($)"
        f"<input type='number' name='montant' value='{montant:g}' min='10' step='any' "
        "></label>"
        "<label>Periode"
        f"<select name='periode'>"
        f"{period_options}</select></label>"
        "<label>Competition"
        f"<select name='competition'>"
        f"{competition_options}</select></label>"
        "<label>Profil de risque"
        f"<select name='profil'>"
        f"{profile_options}</select></label>"
        "<div class='filter-actions'><button type='submit' class='button-primary'>Calculer la strategie</button></div>"
        "</form>"
    )
    allocation_html = render_strategy_bankroll_context(bankroll_context)

    # Retour a CETTE strategie apres une prise ou une validation.
    strategy_qs = urlencode({
        "montant": f"{montant:g}", "periode": periode, "profil": profil,
        **({"competition": competition} if competition else {}),
    })
    strategy_return = f"/bankroll?{strategy_qs}"
    validation_form = strategy_validation_form("football", strategy_return)
    read_only_notice = (
        "<p class='client-hint'><strong>Mode lecture seule :</strong> "
        "validation autorisee, mais la prise, la suppression et le cashout des tickets restent reserves a l'administration.</p>"
        if not can_manage_positions(user) else ""
    )

    strip = (
        "<div class='verdict-strip'>"
        f"<div class='cell'><div class='v'>{plan['exposure_amount']:.2f}$</div>"
        f"<div class='l'>Exposition totale ({plan['exposure_pct']:.1f}%)</div></div>"
        f"<div class='cell'><div class='v'>{plan['expected_profit_amount']:+.2f}$</div>"
        "<div class='l'>Gain espere (deals actifs)</div></div>"
        f"<div class='cell'><div class='v'>{simulation.get('final_median', 0):.0f}$</div>"
        f"<div class='l'>Bankroll mediane a {plan['days']} j</div></div>"
        f"<div class='cell'><div class='v'>{simulation.get('final_p5', 0):.0f}$ / {simulation.get('final_p95', 0):.0f}$</div>"
        "<div class='l'>Fourchette 5% - 95%</div></div>"
        f"<div class='cell'><div class='v'>{simulation.get('prob_loss_pct', 0)}%</div>"
        "<div class='l'>Risque de finir en perte</div></div>"
        f"<div class='cell'><div class='v'>{simulation.get('prob_drawdown30_pct', 0)}%</div>"
        "<div class='l'>Risque de creux -30%</div></div>"
        "</div>"
    )

    source_to_kind = {"VALUE BET": "DEAL", "PRONOSTIC SUR": "PRONOSTIC", "LONG TERME": "OUTRIGHT"}

    def _strategy_key(line: dict[str, Any]) -> str:
        fid = line.get("fixture_id")
        if not fid:
            return ""
        bet_kind = source_to_kind.get(str(line.get("source")), "DEAL")
        return f"{bet_kind}|f{int(fid)}|{line.get('market_code', '1X2')}|{line['selection_code']}"

    # Tableau STRUCTURE GOLF : le pari s'enonce en une phrase, le contexte
    # (match, book, echeance, ou poser) passe en ligne de detail, et la prise
    # se fait inline (cote + mise) sans quitter le tableau.
    if plan["lines"]:
        rows = []
        for line in plan["lines"]:
            selection_label = _selection_display(
                str(line.get("selection_code") or ""),
                str(line.get("market_code") or "1X2"),
                line.get("line"),
            )
            pos_n = int(line.get("position_count") or 0)
            rows.append(
                ("<tr class='taken-row'>" if pos_n else "<tr>")
                + "<td><strong>"
                + escape(_football_bet_sentence(
                    line.get("label"), line.get("market_code"),
                    line.get("selection_code"), line.get("line"),
                ))
                + "</strong>"
                + (
                    f"<a class='detail-link' style='margin-left:6px;font-size:11px' "
                    f"href='/match/{int(line['fixture_id'])}'>Analyse</a>"
                    if line.get("fixture_id") else ""
                )
                + f"<div class='muted' style='font-size:11px'>{escape(_football_competition_line(line))} · "
                + escape(str(line.get("source") or "-"))
                + f" · {escape(_CATEGORY_LABELS.get(line['category'], line['category']))}</div></td>"
                + f"<td>{escape(_football_market_label(line.get('market_code')))}</td>"
                + f"<td class='num'>{line['market_odd']:.2f}</td>"
                + f"<td class='num'>{line['credible_probability'] * 100:.1f}</td>"
                + _edge_cell(round(line.get("expected_value", 0) * 100, 1))
                + f"<td class='num'>{line['stake_pct']:.2f}%</td>"
                + f"<td class='num'><strong>{line['stake_amount']:.2f}$</strong></td>"
                + _football_take_cell(
                    _strategy_key(line), line.get("market_odd"), pos_n,
                    line.get("taken_stake_amount"), strategy_return,
                    stake_default=line.get("stake_amount"),
                    selection_label=selection_label, line=line.get("line"),
                    can_write=can_manage_positions(user),
                )
                + "</tr>"
            )
        line_rows = "".join(rows)
    else:
        line_rows = (
            "<tr><td colspan='8' class='empty'>Aucune position candidate en ce moment - "
            "le plan se remplira au prochain scan.</td></tr>"
        )
    allocations_html = render_section(
        "01", "Prises de position recommandees",
        "<div class='table-wrap'><table><thead><tr><th>Pari</th><th>Marche</th>"
        "<th class='num'>Cote</th><th class='num'>Proba projetee %</th><th class='num'>EV</th>"
        "<th class='num'>Mise % bankroll</th><th class='num'>Mise $</th>"
        "<th>Prise (cote / mise $)</th></tr></thead>"
        f"<tbody>{line_rows}</tbody></table></div>",
        note="Plan de mise client: quel pari prendre, a quelle cote, et quel montant engager selon le profil choisi. La ligne de detail indique le match, le bookmaker, l'echeance et ou poser le pari.",
    )

    # --- Tickets combines ---------------------------------------------------
    parlays = parlays or []
    if parlays:
        ticket_rows = []
        for i, ticket in enumerate(parlays, start=1):
            legs_html = "<br>".join(
                f"{_pick_chip(leg['selection_code'], market_code=leg.get('market_code'))} {escape(leg['label'])} "
                f"<span class='muted'>@{leg['market_odd']:.2f} ({escape(str(leg['bookmaker']))})</span>"
                for leg in ticket["legs"]
            )
            # Jambes encodees pour le reglement du combine (fixture+marche).
            parlay_legs = [
                {
                    "fixture_id": leg.get("fixture_id"),
                    "market_code": leg.get("market_code", "1X2"),
                    "selection_code": leg["selection_code"],
                    "odd": leg["market_odd"],
                    "label": leg["label"],
                }
                for leg in ticket["legs"]
            ]
            prise_cell = _prise_button(
                title=f"Prendre le combine C{i}",
                subtitle=f"{len(parlay_legs)} jambes @ {ticket['combined_odd']:.2f}",
                suggested_odd=ticket["combined_odd"],
                parlay_legs=parlay_legs,
                cell=True,
                can_write=can_manage_positions(user),
            )
            ticket_rows.append(
                "<tr>"
                f"<td class='num'><strong>C{i}</strong></td>"
                + prise_cell
                + f"<td>{legs_html}</td>"
                f"<td class='num'><strong>{ticket['combined_odd']:.2f}</strong></td>"
                f"<td class='num'>{ticket['combined_probability'] * 100:.1f}</td>"
                f"<td class='num sig'><strong>{ticket['stake_amount']:.2f}$</strong></td>"
                f"<td class='num'>+{ticket['win_profit']:.2f}$</td>"
                f"<td class='num'>{ticket['expected_value'] * 100:+.1f}%</td>"
                "</tr>"
            )
        parlays_html = render_section(
            "02", "Tickets combines",
            "<div class='table-wrap'><table>"
            "<thead><tr><th class='num'>#</th><th>Prise</th><th>Jambes</th><th class='num'>Cote totale</th>"
            "<th class='num'>Proba %</th><th class='num'>Mise</th>"
            "<th class='num'>Gain si gagne</th><th class='num'>EV</th></tr></thead>"
            f"<tbody>{''.join(ticket_rows)}</tbody></table></div>",
            note="Bouton Prise = jouer le combine (cote combinee + mise reelles). Un combine gagne SEULEMENT si toutes ses jambes gagnent. Suivi dans le Back (filtre Combines). Jamais deux paris du meme match.",
        )
    else:
        parlays_html = render_section(
            "02", "Tickets combines",
            "<p class='empty'>Aucun combine ne bat ses jambes en simple en ce moment - "
            "les paris simples sont la meilleure strategie du jour.</p>",
        )

    repartition = plan["repartition"]
    repartition_html = render_section(
        "03", "Balance du portefeuille",
        "<div class='verdict-strip'>"
        f"<div class='cell'><div class='v'>{repartition['SAFE']}%</div><div class='l'>Sur (proba &ge; 55%)</div></div>"
        f"<div class='cell'><div class='v'>{repartition['MODERE']}%</div><div class='l'>Modere (40-55%)</div></div>"
        f"<div class='cell'><div class='v'>{repartition['RISQUE']}%</div><div class='l'>Risque (&lt; 40%)</div></div>"
        f"<div class='cell'><div class='v'>{simulation.get('expected_bets', 0)}</div><div class='l'>Paris attendus sur {plan['days']} j</div></div>"
        "</div>",
        note="Repartition de l'exposition par niveau de probabilite projetee. La balance emerge du Kelly : les paris surs recoivent naturellement plus, le profil regle l'agressivite globale.",
    )

    # --- Faits divers a surveiller (favoris long terme, meme sans edge) ------
    outright_watch = outright_watch or []
    if jours >= 30 and outright_watch:
        watch_rows = "".join(
            "<tr>"
            f"<td>{escape(str(w['market_label']))}</td>"
            f"<td><strong>{escape(str(w['subject_label']))}</strong></td>"
            f"<td class='num'>{w['model_pct']}</td>"
            + (
                f"<td class='num'>{float(w['best_odd']):.2f}</td>"
                if w.get("best_odd") else "<td class='num muted'>-</td>"
            )
            + _edge_cell(_to_float(w.get("edge_pct")))
            + f"<td class='muted'>{escape(str(w['bookmaker_name'] or '-'))}</td>"
            "</tr>"
            for w in outright_watch
        )
        watch_html = render_section(
            "04", "Faits divers a surveiller",
            "<div class='table-wrap'><table>"
            "<thead><tr><th>Marche</th><th>Favori du modele</th><th class='num'>Proba %</th>"
            "<th class='num'>Meilleure cote</th><th class='num'>Edge</th><th>Book</th></tr></thead>"
            f"<tbody>{watch_rows}</tbody></table></div>",
            note="Favoris long terme SANS value bet actif : proposes quand meme (edge gris = pas de value, le marche price juste). Un edge rouge ici deviendrait une position a miser au prochain scan.",
        )
        method_number = "05"
    else:
        watch_html = ""
        method_number = "04"

    simulations_label = f"{simulation.get('simulations', 0):,}".replace(",", " ")
    method_html = render_section(
        method_number, "Methode",
        "<p class='prose'>"
        f"Profil <strong>{escape(profile['label'])}</strong> : {escape(profile['description'])} "
        f"Kelly fractionnaire x{profile['kelly_multiplier']:g}, plafond {profile['stake_cap_pct']:g}% par pari, "
        f"exposition simultanee max {profile['exposure_cap_pct']:g}% de la bankroll. "
        f"Seuils : edge min {profile.get('min_edge_pct', 0):g}%, EV min {profile.get('min_ev_pct', 0):g}%, "
        f"cote max {profile.get('max_odd', '-')}, {profile.get('max_positions', '-')} positions max, "
        f"combines {'autorises' if profile.get('parlay_enabled') else 'desactives'}. "
        f"Caps risque : Sur {profile.get('category_caps_pct', {}).get('SAFE', 0):g}%, "
        f"Modere {profile.get('category_caps_pct', {}).get('MODERE', 0):g}%, "
        f"Risque {profile.get('category_caps_pct', {}).get('RISQUE', 0):g}% de bankroll. "
        "Les mises sont proportionnelles a la bankroll courante : la ruine totale est mathematiquement "
        "impossible, et les creux restent bornes. La projection Monte Carlo simule "
        f"{simulations_label} trajectoires de la periode (deals actifs + paris futurs au rythme "
        "reellement observe) avec les probabilites PROJETEES, pas celles du modele brut : les chiffres "
        "affiches sont volontairement conservateurs.</p>",
    )

    return f"""<!DOCTYPE html>
<html lang="fr">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Strategie de paris - Sports Prediction Engine</title>
  <style>{BASE_CSS}</style>
</head>
<body>
  <main class="shell">
    {render_sport_nav("football", "strategie", user)}
    <header class="matchline">
      <div>
        <h1>Strategie de paris</h1>
        <p class="meta">Bankroll {montant:g}$ - {escape(STRATEGY_PERIODS.get(periode, ('', 0))[0])} ({jours} jours) - profil {escape(profile['label'])}</p>
      </div>
      <div class="hero-visual football" aria-hidden="true"><span class="ball"></span></div>
    </header>
    {validation_notice}
    {form_html}
    {validation_form}
    {read_only_notice}
    {allocation_html}
    <div style="margin-top:24px">{strip}</div>
    {allocations_html}
    {parlays_html}
    {repartition_html}
    {watch_html}
    {method_html}
    {render_product_footer("football")}
  </main>
  {prise_modal_html(strategy_return, "")}
{dev_reload_script()}
</body>
</html>"""


def parse_post_body(environ: dict[str, Any]) -> dict[str, list[str]]:
    content_length = int(environ.get("CONTENT_LENGTH") or "0")
    raw_body = environ["wsgi.input"].read(content_length).decode("utf-8") if content_length else ""
    return parse_qs(raw_body, keep_blank_values=True)


POSITION_ACTIONS = {
    "toggle_taken", "take_position", "clear_position",
    "delete_ticket", "take_parlay", "golf_take_position",
    "cashout_position",
}
BACK_MUTATION_ACTIONS = {
    "toggle_taken", "save_note", "delete_ticket",
    "cashout_position", "delete_parlay",
}


def save_position_action(form_params: dict[str, list[str]], user: UserContext | None = None) -> bool:
    """Execute une action de prise/suppression sans dependre de la route.

    Important pour /bankroll : cette page a son propre routeur, donc elle doit
    enregistrer la prise AVANT de recalculer la strategie.
    """
    action = (form_params.get("action", [""])[0] or "").strip()
    if action not in POSITION_ACTIONS:
        return False
    require_permission(user, "POSITION_WRITE_OWN")
    actor_user_id = int(user.user_id if user else 1)

    key = (form_params.get("key", [""])[0] or "").strip()
    model_probability = None
    model_fair_odd = None
    selection_label = None
    line_value = None
    if action == "take_position" and not key:
        pick = (form_params.get("position_pick", [""])[0] or "").strip()
        bet_kind = (form_params.get("bet_kind", [""])[0] or "").strip()
        fixture_raw = (form_params.get("fixture_id", [""])[0] or "").strip()
        pick_parts = pick.split("|", 4)
        if len(pick_parts) == 5 and fixture_raw.isdigit():
            market_code, selection_code, prob_raw, fair_raw, selection_label = pick_parts
            if market_code == "HANDICAP":
                line_raw = (form_params.get("line", [""])[0] or "").strip()
                line_value = _to_float(line_raw)
                line_txt = _format_line_value(line_raw)
                if line_txt and line_txt not in str(selection_label):
                    selection_label = f"{selection_label} {line_txt}"
            key = f"{bet_kind}|f{int(fixture_raw)}|{market_code}|{selection_code}"
            model_probability = _to_float(prob_raw)
            model_fair_odd = _to_float(fair_raw)
    elif action == "take_position":
        selection_label = (form_params.get("selection_label", [""])[0] or "").strip() or None
        line_raw = (form_params.get("line", [""])[0] or "").strip()
        line_value = _to_float(line_raw)
        parts = key.split("|")
        if len(parts) == 4 and parts[2] == "HANDICAP" and selection_label:
            line_txt = _format_line_value(line_raw)
            if line_txt and line_txt not in selection_label:
                selection_label = f"{selection_label} {line_txt}"

    if action == "golf_take_position":
        golf_deal_raw = (form_params.get("golf_deal_id", [""])[0] or "").strip()
        if golf_deal_raw.isdigit():
            save_golf_position(
                (form_params.get("golf_deal_type", ["OUTRIGHT"])[0] or "OUTRIGHT").strip().upper(),
                int(golf_deal_raw),
                taken_odd=_to_float(form_params.get("taken_odd", [""])[0]),
                stake_amount=_to_float(form_params.get("stake_amount", [""])[0]),
                user_id=actor_user_id,
            )
    elif action == "take_parlay":
        save_parlay(
            (form_params.get("parlay", [""])[0] or ""),
            combined_odd=_to_float(form_params.get("taken_odd", [""])[0]),
            stake_amount=_to_float(form_params.get("stake_amount", [""])[0]),
            user_id=actor_user_id,
        )
    elif action == "delete_ticket":
        ticket_raw = (form_params.get("position_id", [""])[0] or "").strip()
        if ticket_raw.isdigit():
            delete_position_ticket(int(ticket_raw), user)
    elif action == "cashout_position":
        ticket_raw = (form_params.get("position_id", [""])[0] or "").strip()
        amount = _to_float(form_params.get("cashout_amount", [""])[0])
        if ticket_raw.isdigit() and amount is not None:
            cashout_position_ticket(int(ticket_raw), amount, user)
    elif key:
        if action == "take_position":
            save_bet_annotation(
                key,
                take_position=True,
                taken_odd=_to_float(form_params.get("taken_odd", [""])[0]),
                stake_amount=_to_float(form_params.get("stake_amount", [""])[0]),
                model_probability=model_probability,
                model_fair_odd=model_fair_odd,
                selection_label=selection_label,
                line=line_value,
                user_id=actor_user_id,
            )
        elif action == "clear_position":
            save_bet_annotation(key, clear_position=True, user_id=actor_user_id)
        else:
            save_bet_annotation(key, toggle_taken=True, user_id=actor_user_id)
    return True


def application(environ, start_response):
    method = environ.get("REQUEST_METHOD", "GET").upper()
    path = environ.get("PATH_INFO", "/") or "/"

    if path.rstrip("/") == "/__dev_version":
        start_response(
            "200 OK",
            [
                ("Content-Type", "text/plain; charset=utf-8"),
                ("Cache-Control", "no-store"),
            ],
        )
        return [DEV_SERVER_VERSION.encode("utf-8")]

    if path.rstrip("/") == "/health/live":
        return json_response(
            start_response,
            "200 OK",
            {
                "status": "live",
                **release_payload(),
            },
        )

    if path.rstrip("/") == "/health/ready":
        ready, payload = check_database_ready()
        return json_response(
            start_response,
            "200 OK" if ready else "503 Service Unavailable",
            {
                "status": "ready" if ready else "not_ready",
                **release_payload(),
                **payload,
            },
        )

    if path.rstrip("/") == "/release":
        return json_response(start_response, "200 OK", release_payload())

    # --- Assets statiques (visuels de marque, libres de droits) -------------
    if path.startswith("/static/"):
        name = os.path.basename(path)  # anti-traversal : basename seul
        types = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                 ".webp": "image/webp", ".svg": "image/svg+xml", ".ico": "image/x-icon"}
        ext = os.path.splitext(name)[1].lower()
        asset = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", name)
        if ext in types and os.path.isfile(asset):
            with open(asset, "rb") as fh:
                body = fh.read()
            start_response("200 OK", [
                ("Content-Type", types[ext]),
                ("Cache-Control", "public, max-age=604800"),
                ("Content-Length", str(len(body))),
            ])
            return [body]
        start_response("404 Not Found", [("Content-Type", "text/plain; charset=utf-8")])
        return [b"asset introuvable"]

    query_params = parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=True)

    if path.rstrip("/") == "/signup":
        if method == "POST":
            form = parse_post_body(environ)
            email = (form.get("email", [""])[0] or "").strip()
            display_name = (form.get("display_name", [""])[0] or "").strip()
            password = form.get("password", [""])[0] or ""
            confirm = form.get("password_confirm", [""])[0] or ""
            role_code = (form.get("role_code", ["CLIENT"])[0] or "CLIENT").strip()
            admin_key = form.get("admin_key", [""])[0] or ""
            try:
                if password != confirm:
                    raise ValueError("les deux mots de passe ne correspondent pas")
                user_id = signup_user_account(email, display_name, password, role_code, admin_key, environ)
            except ValueError as exc:
                html = render_signup_page(str(exc))
                start_response("400 Bad Request", [("Content-Type", "text/html; charset=utf-8")])
                return [html.encode("utf-8")]
            except Exception:
                html = render_signup_page("creation impossible — reessaie ou contacte l'administrateur")
                start_response("500 Internal Server Error", [("Content-Type", "text/html; charset=utf-8")])
                return [html.encode("utf-8")]
            token = create_session(user_id, environ)
            secure_flag = "; Secure" if get_env("APP_ENV", "local") not in ("local", "test") else ""
            cookie = (
                f"{SESSION_COOKIE_NAME}={token}; Path=/; HttpOnly; SameSite=Lax{secure_flag}"
            )
            return redirect_response(start_response, "/", [("Set-Cookie", cookie)])
        html = render_signup_page("")
        start_response("200 OK", [("Content-Type", "text/html; charset=utf-8")])
        return [html.encode("utf-8")]

    if path.rstrip("/") == "/login":
        next_url = _safe_next((query_params.get("next", ["/"])[0] or "/"))
        if method == "POST":
            form = parse_post_body(environ)
            email = (form.get("email", [""])[0] or "").strip()
            password = form.get("password", [""])[0] or ""
            next_url = _safe_next((form.get("next", [next_url])[0] or next_url))
            user = authenticate_user(email, password, environ)
            if user is None:
                html = render_login_page("Email ou mot de passe invalide.", next_url)
                start_response("401 Unauthorized", [("Content-Type", "text/html; charset=utf-8")])
                return [html.encode("utf-8")]
            token = create_session(user.user_id, environ)
            secure_flag = "; Secure" if get_env("APP_ENV", "local") not in ("local", "test") else ""
            cookie = (
                f"{SESSION_COOKIE_NAME}={token}; Path=/; HttpOnly; SameSite=Lax{secure_flag}"
            )
            return redirect_response(start_response, next_url, [("Set-Cookie", cookie)])
        html = render_login_page("", next_url)
        start_response("200 OK", [("Content-Type", "text/html; charset=utf-8")])
        return [html.encode("utf-8")]

    current_user = current_user_from_request(environ)

    if path.rstrip("/") == "/logout":
        revoke_session(environ, current_user)
        return redirect_response(
            start_response,
            "/login",
            [("Set-Cookie", f"{SESSION_COOKIE_NAME}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax")],
        )

    if current_user is None:
        return redirect_response(start_response, f"/login?next={quote(path + (('?' + environ.get('QUERY_STRING', '')) if environ.get('QUERY_STRING') else ''))}")

    if path.startswith("/admin/client/"):
        try:
            require_permission(current_user, "ADMIN_VIEW")
            user_id = int(path.split("/admin/client/", 1)[1].strip("/"))
            page_report = ""
            page_error = ""
            if method == "POST":
                form = parse_post_body(environ)
                action = (form.get("action", [""])[0] or "").strip()
                try:
                    if action == "update_client_profile":
                        save_admin_client_profile(current_user, user_id, form)
                        page_report = "Fiche client mise a jour."
                    elif action == "add_client_note":
                        add_admin_client_note(
                            current_user,
                            user_id,
                            form.get("note_kind", ["ADMIN"])[0] or "ADMIN",
                            form.get("note_body", [""])[0] or "",
                        )
                        page_report = "Note client ajoutee."
                except Exception as exc:
                    page_error = f"Operation client impossible : {exc}"
            data = load_admin_client_detail(user_id)
            if data is None:
                start_response("404 Not Found", [("Content-Type", "text/plain; charset=utf-8")])
                return ["Client introuvable".encode("utf-8")]
            html = render_admin_client_page(current_user, data, report=page_report, error=page_error)
            start_response("200 OK", [("Content-Type", "text/html; charset=utf-8")])
            return [html.encode("utf-8")]
        except PermissionError:
            start_response("403 Forbidden", [("Content-Type", "text/plain; charset=utf-8")])
            return ["Acces admin refuse".encode("utf-8")]

    if path.rstrip("/") == "/admin":
        try:
            require_permission(current_user, "ADMIN_VIEW")
            admin_report = ""
            admin_error = ""
            if method == "POST":
                form = parse_post_body(environ)
                if (form.get("action", [""])[0] or "") == "create_user":
                    try:
                        created_user_id = create_user_account(
                            current_user,
                            email=form.get("email", [""])[0] or "",
                            display_name=form.get("display_name", [""])[0] or "",
                            password=form.get("password", [""])[0] or "",
                            role_code=form.get("role_code", ["CLIENT"])[0] or "CLIENT",
                        )
                        admin_report = f"Utilisateur cree/mis a jour (ID {created_user_id})."
                    except Exception as exc:
                        admin_error = f"Creation utilisateur impossible : {exc}"
            html = render_admin_dashboard(
                current_user,
                load_admin_dashboard((query_params.get("q", [""])[0] or "")),
                report=admin_report,
                error=admin_error,
            )
            start_response("200 OK", [("Content-Type", "text/html; charset=utf-8")])
            return [html.encode("utf-8")]
        except PermissionError:
            start_response("403 Forbidden", [("Content-Type", "text/plain; charset=utf-8")])
            return ["Acces admin refuse".encode("utf-8")]

    # Route /match/{fixture_id} : page detail d'un match
    if path.startswith("/match/"):
        try:
            fixture_id = int(path.split("/match/", 1)[1].strip("/"))
        except ValueError:
            start_response("404 Not Found", [("Content-Type", "text/plain; charset=utf-8")])
            return ["Match introuvable".encode("utf-8")]
        try:
            data = load_match_detail(fixture_id)
        except Exception as exc:
            start_response("500 Internal Server Error", [("Content-Type", "text/plain; charset=utf-8")])
            return [f"Erreur: {exc}".encode("utf-8")]
        if data is None:
            start_response("404 Not Found", [("Content-Type", "text/plain; charset=utf-8")])
            return ["Match introuvable".encode("utf-8")]
        html = render_match_detail(data)
        start_response("200 OK", [("Content-Type", "text/html; charset=utf-8")])
        return [html.encode("utf-8")]

    # Route /golf/{golf_tournament_id} : page analyse d'un tournoi golf
    if path.startswith("/golf/"):
        try:
            golf_tournament_id = int(path.split("/golf/", 1)[1].strip("/"))
        except ValueError:
            start_response("404 Not Found", [("Content-Type", "text/plain; charset=utf-8")])
            return ["Tournoi golf introuvable".encode("utf-8")]
        try:
            data = load_golf_detail(golf_tournament_id)
        except Exception as exc:
            start_response("500 Internal Server Error", [("Content-Type", "text/plain; charset=utf-8")])
            return [f"Erreur: {exc}\n\n{traceback.format_exc()}".encode("utf-8")]
        if data is None:
            start_response("404 Not Found", [("Content-Type", "text/plain; charset=utf-8")])
            return ["Tournoi golf introuvable".encode("utf-8")]
        html = render_golf_detail(data, current_user)
        start_response("200 OK", [("Content-Type", "text/html; charset=utf-8")])
        return [html.encode("utf-8")]

    query_params = parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=True)

    # Route /back : espace de verification des performances.
    if path.rstrip("/") == "/back":
        # POST : case "position prise" ou note perso -> redirect (PRG).
        if method == "POST":
            form = parse_post_body(environ)
            action = (form.get("action", [""])[0] or "").strip()
            key = (form.get("key", [""])[0] or "").strip()
            redirect_qs = (form.get("qs", [""])[0] or "").strip()
            try:
                if action in BACK_MUTATION_ACTIONS:
                    require_permission(current_user, "POSITION_WRITE_OWN")
                if action == "toggle_taken" and key:
                    save_bet_annotation(key, toggle_taken=True, user_id=current_user.user_id)
                elif action == "save_note" and key:
                    save_bet_annotation(key, note=(form.get("note", [""])[0] or ""), user_id=current_user.user_id)
                elif action == "delete_ticket":
                    ticket_raw = (form.get("position_id", [""])[0] or "").strip()
                    if ticket_raw.isdigit():
                        delete_position_ticket(int(ticket_raw), current_user)
                elif action == "cashout_position":
                    ticket_raw = (form.get("position_id", [""])[0] or "").strip()
                    amount = _to_float(form.get("cashout_amount", [""])[0])
                    if ticket_raw.isdigit() and amount is not None:
                        cashout_position_ticket(int(ticket_raw), amount, current_user)
                elif action == "delete_parlay":
                    parlay_raw = (form.get("parlay_id", [""])[0] or "").strip()
                    if parlay_raw.isdigit():
                        delete_parlay(int(parlay_raw), current_user)
                elif action == "validate_back":
                    require_permission(current_user, "VALIDATION_REFRESH_OWN")
                    sport = (form.get("sport", ["all"])[0] or "all").strip().lower()
                    suffix = validate_back_payload(sport)["_summary_qs"]
                    redirect_qs = f"{redirect_qs}&{suffix}" if redirect_qs else suffix
            except Exception as exc:
                if action == "validate_back":
                    redirect_qs = f"{redirect_qs}&validation_error=1" if redirect_qs else "validation_error=1"
                else:
                    redirect_qs = merge_query_string(
                        redirect_qs,
                        {"action_error": str(exc)[:180]},
                    )
                # une annotation ratee ne casse pas la page
            location = "/back" + (f"?{redirect_qs}" if redirect_qs else "")
            start_response("303 See Other", [("Location", location)])
            return [b""]
        # Marche + Statut : selects simples ("all" = tous), stockes en liste
        # car le SQL accepte plusieurs valeurs.
        market_one = (query_params.get("market", ["all"])[0] or "all").strip()
        status_one = (query_params.get("status", ["all"])[0] or "all").strip()
        filters = {
            "kind": (query_params.get("kind", ["all"])[0] or "all"),
            "markets": [] if market_one == "all" else [market_one],
            "statuses": [] if status_one == "all" else [status_one],
            "league": (query_params.get("league", ["all"])[0] or "all"),
            "taken": (query_params.get("positions", query_params.get("taken", ["all"]))[0] or "all"),
            "sort": (query_params.get("sort", ["date_desc"])[0] or "date_desc"),
            # Sport : contexte de page, pilote par la nav (/back?sport=...).
            # sport_filter reste relu seulement pour compatibilite avec les
            # anciennes URLs avant suppression du filtre visible.
            "sport": (
                query_params.get("sport", query_params.get("sport_filter", ["football"]))[0]
                or "football"
            ).strip().lower(),
            # Calendrier : un JOUR precis, applique a la date du match
            # (date_kind=match) ou a la date de PRISE (date_kind=prise).
            "day": _valid_date_filter(query_params.get("day", [""])[0]),
            "date_kind": (query_params.get("date_kind", ["match"])[0] or "match"),
            "validated": (query_params.get("validated", [""])[0] or ""),
            "validation_error": (query_params.get("validation_error", [""])[0] or ""),
            "action_error": (query_params.get("action_error", [""])[0] or ""),
            "vf": (query_params.get("vf", ["0"])[0] or "0"),
            "vg": (query_params.get("vg", ["0"])[0] or "0"),
            "vs": (query_params.get("vs", ["0"])[0] or "0"),
            "admin_user_id": (query_params.get("admin_user_id", [""])[0] or ""),
        }
        try:
            admin_target_user_id = (query_params.get("admin_user_id", [""])[0] or "").strip()
            if current_user.is_admin and admin_target_user_id.isdigit():
                filters["user_id"] = admin_target_user_id
            else:
                filters["user_id"] = str(current_user.user_id)
            filters["is_admin"] = "1" if current_user.is_admin else "0"
            html = render_back_page(load_back_data(filters), filters, current_user)
        except Exception as exc:
            start_response("500 Internal Server Error", [("Content-Type", "text/plain; charset=utf-8")])
            return [f"Erreur: {exc}\n\n{traceback.format_exc()}".encode("utf-8")]
        start_response("200 OK", [("Content-Type", "text/html; charset=utf-8")])
        return [html.encode("utf-8")]

    # Route /bankroll/account : capital personnel et historique ledger.
    if path.rstrip("/") == "/bankroll/account":
        report_message = ""
        if method == "POST":
            form = parse_post_body(environ)
            if (form.get("action", [""])[0] or "") == "set_bankroll":
                try:
                    amount = float((form.get("amount", [""])[0] or "0").replace(",", "."))
                    reason = form.get("reason", [""])[0] or ""
                    set_user_bankroll(current_user, amount, reason)
                    report_message = "Bankroll mise a jour."
                except Exception as exc:
                    report_message = str(exc)
        html = render_bankroll_account_page(current_user, report_message)
        start_response("200 OK", [("Content-Type", "text/html; charset=utf-8")])
        return [html.encode("utf-8")]

    # Route /bankroll : plan de mise personnalise (bankroll, periode, profil).
    if path.rstrip("/") == "/bankroll":
        if method == "POST":
            form_params = parse_post_body(environ)
            action = (form_params.get("action", [""])[0] or "").strip()
            if action == "validate_back":
                return_to = (form_params.get("return_to", [""])[0] or "").strip()
                if not return_to.startswith("/bankroll"):
                    qs = environ.get("QUERY_STRING", "")
                    return_to = "/bankroll" + (f"?{qs}" if qs else "")
                try:
                    require_permission(current_user, "VALIDATION_REFRESH_OWN")
                    sport = (form_params.get("sport", ["football"])[0] or "football").strip().lower()
                    suffix = validate_back_payload(sport)["_summary_qs"]
                except Exception:
                    suffix = "validation_error=1"
                separator = "&" if "?" in return_to else "?"
                location = f"{return_to}{separator}{suffix}"
                start_response("303 See Other", [("Location", location)])
                return [b""]
            try:
                if save_position_action(form_params, current_user):
                    return_to = (form_params.get("return_to", [""])[0] or "").strip()
                    if return_to.startswith(("/back", "/bankroll", "/?sport=")):
                        location = return_to
                    else:
                        qs = environ.get("QUERY_STRING", "")
                        location = "/bankroll" + (f"?{qs}" if qs else "")
                    start_response("303 See Other", [("Location", location)])
                    return [b""]
            except Exception as exc:
                qs = environ.get("QUERY_STRING", "")
                location = "/bankroll" + (f"?{qs}" if qs else "")
                location = append_url_params(location, {"action_error": str(exc)[:180]})
                start_response("303 See Other", [("Location", location)])
                return [b""]
        # Variante GOLF : meme page strategie, sport a part (DA identique).
        if (query_params.get("sport", [""])[0] or "").strip().lower() == GOLF_SPORT_PARAM:
            golf_profile = (query_params.get("golf_profile", [DEFAULT_PROFILE])[0] or DEFAULT_PROFILE).strip()
            golf_periode = (query_params.get("periode", [DEFAULT_PERIOD])[0] or DEFAULT_PERIOD).strip()
            if golf_periode not in STRATEGY_PERIODS:
                golf_periode = DEFAULT_PERIOD
            requested_golf_bankroll = None
            raw_bankroll = (query_params.get("bankroll", [""])[0] or "").strip()
            try:
                requested_golf_bankroll = float(raw_bankroll) if raw_bankroll else None
            except (ValueError, TypeError):
                requested_golf_bankroll = None
            recalculate_budget = (query_params.get("recalculate", [""])[0] or "") == "1"
            bankroll_context = load_strategy_bankroll_context(
                current_user.user_id, GOLF_SPORT_PARAM, requested_golf_bankroll,
                lock_requested=(requested_golf_bankroll is not None and not recalculate_budget),
            )
            golf_bankroll = float(bankroll_context["strategy_bankroll"])
            try:
                golf_scope, golf_days = golf_scope_filters(golf_periode)
                html = render_golf_strategy_page(
                    load_golf_data("all", golf_scope, current_user.user_id),
                    golf_profile,
                    golf_bankroll,
                    golf_periode,
                    golf_days,
                    current_user,
                    bankroll_context,
                    validation_notice_from_params(query_params),
                )
            except Exception as exc:
                start_response("500 Internal Server Error", [("Content-Type", "text/plain; charset=utf-8")])
                return [f"Erreur: {exc}\n\n{traceback.format_exc()}".encode("utf-8")]
            start_response("200 OK", [("Content-Type", "text/html; charset=utf-8")])
            return [html.encode("utf-8")]
        requested_montant = None
        raw_montant = (query_params.get("montant", [""])[0] or "").strip()
        try:
            requested_montant = float(raw_montant) if raw_montant else None
        except (ValueError, TypeError):
            requested_montant = None
        recalculate_budget = (query_params.get("recalculate", [""])[0] or "") == "1"
        bankroll_context = load_strategy_bankroll_context(
            current_user.user_id, "football", requested_montant,
            lock_requested=(requested_montant is not None and not recalculate_budget),
        )
        montant = float(bankroll_context["strategy_bankroll"])
        periode = (query_params.get("periode", [DEFAULT_PERIOD])[0] or DEFAULT_PERIOD).strip()
        if periode not in STRATEGY_PERIODS:
            periode = DEFAULT_PERIOD
        jours = STRATEGY_PERIODS[periode][1]
        profil = (query_params.get("profil", [DEFAULT_PROFILE])[0] or DEFAULT_PROFILE).strip()
        if profil not in RISK_PROFILES:
            profil = DEFAULT_PROFILE
        competition = (query_params.get("competition", [""])[0] or "").strip()
        try:
            deals, bets_per_day, outright_watch = load_bankroll_inputs(
                days=jours, competition=competition or None, user_id=current_user.user_id,
            )
            plan = build_portfolio_plan(
                deals, bankroll=montant, days=jours,
                profile_code=profil, bets_per_day=bets_per_day,
            )
            parlays = build_parlay_suggestions(
                plan["lines"], montant, RISK_PROFILES[profil],
            )
            html = render_bankroll_page(
                plan, montant, jours, profil, periode,
                competition=competition, parlays=parlays,
                outright_watch=outright_watch,
                user=current_user,
                bankroll_context=bankroll_context,
                validation_notice=validation_notice_from_params(query_params),
            )
        except Exception as exc:
            start_response("500 Internal Server Error", [("Content-Type", "text/plain; charset=utf-8")])
            return [f"Erreur: {exc}\n\n{traceback.format_exc()}".encode("utf-8")]
        start_response("200 OK", [("Content-Type", "text/html; charset=utf-8")])
        return [html.encode("utf-8")]

    request_params = query_params
    report = None
    error_message = user_action_error(query_params)
    selected_sport = (query_params.get("sport", ["football"])[0] or "football").strip().lower()

    if method == "POST":
        form_params = parse_post_body(environ)
        request_params = form_params
        selected_sport = (form_params.get("sport", ["football"])[0] or "football").strip().lower()
        action = (form_params.get("action", [""])[0] or "").strip()

        # Prise / retrait de position (modal de la page principale ou de la
        # strategie) -> enregistre et redirige (PRG). return_to ramene a la
        # page d'origine (board "/" ou strategie "/bankroll?...").
        if action in POSITION_ACTIONS:
            action_error = None
            try:
                save_position_action(form_params, current_user)
            except Exception as exc:
                action_error = str(exc)[:180]
            # Retour a la page d'origine (defaut = board).
            return_to = (form_params.get("return_to", [""])[0] or "").strip()
            if return_to.startswith(("/back", "/bankroll", "/?sport=")):
                location = return_to
            else:
                location = "/?" + urlencode({"league": selected_league_from_params(form_params)})
            if action_error:
                location = append_url_params(location, {"action_error": action_error})
            start_response("303 See Other", [("Location", location)])
            return [b""]

        try:
            if action:
                # Le pipeline cible la competition selectionnee ; le mode
                # "Toutes competitions" declenche le balayage global 7 jours.
                action_league = selected_league_from_params(form_params)
                if selected_sport == GOLF_SPORT_PARAM:
                    require_permission(current_user, "CYCLE_RUN")
                    report = start_action_in_background(
                        action,
                        golf_tournament=(form_params.get("tournament", ["all"])[0] or "all").strip(),
                        golf_filters=golf_filters_from_params(form_params),
                    )
                else:
                    require_permission(current_user, "CYCLE_RUN")
                    report = start_action_in_background(
                        action,
                        league_name=None if action_league == ALL_LEAGUES else action_league,
                    )
        except Exception as exc:
            error_message = f"{exc}\n\n{traceback.format_exc()}"

    # Une action d'arriere-plan vient de finir ? Son rapport s'affiche ici.
    if report is None:
        report = consume_finished_action_report()

    active_view = (request_params.get("view", ["predictions"])[0] or "predictions").strip().lower()
    if active_view not in ("predictions", "deals"):
        active_view = "predictions"

    if selected_sport == GOLF_SPORT_PARAM:
        selected_tournament = (request_params.get("tournament", ["all"])[0] or "all").strip()
        golf_filters = golf_filters_from_params(request_params)
        golf_profile = (request_params.get("golf_profile", [DEFAULT_PROFILE])[0] or DEFAULT_PROFILE).strip()
        try:
            golf_bankroll = float(request_params.get("bankroll", ["100"])[0] or 100)
        except (ValueError, TypeError):
            golf_bankroll = 100.0
        try:
            golf_data = load_golf_data(selected_tournament, golf_filters, current_user.user_id)
            golf_data["view"] = active_view
            html = render_golf_page(golf_data, report, error_message,
                                    profile_code=golf_profile, bankroll=golf_bankroll, user=current_user)
        except Exception as exc:
            start_response("500 Internal Server Error", [("Content-Type", "text/plain; charset=utf-8")])
            return [f"Erreur: {exc}\n\n{traceback.format_exc()}".encode("utf-8")]
        start_response("200 OK", [("Content-Type", "text/html; charset=utf-8")])
        return [html.encode("utf-8")]

    selected_league = selected_league_from_params(request_params)

    # Mode faits divers : la page des marches long terme remplace le board.
    if selected_league == FAITS_DIVERS:
        try:
            html = render_outrights_page(load_outrights_data())
        except Exception as exc:
            start_response("500 Internal Server Error", [("Content-Type", "text/plain; charset=utf-8")])
            return [f"Erreur: {exc}\n\n{traceback.format_exc()}".encode("utf-8")]
        start_response("200 OK", [("Content-Type", "text/html; charset=utf-8")])
        return [html.encode("utf-8")]

    filters = prediction_filters_from_params(request_params)
    filters["view"] = active_view
    horizon_code = str(filters["horizon"])

    try:
        data = load_dashboard_data(selected_league, horizon_code, current_user.user_id)
        data["horizon"] = horizon_code
        data["filters"] = filters
        data["view"] = active_view
    except Exception as exc:
        error_message = f"{exc}\n\n{traceback.format_exc()}"
        data = {
            "metrics": [MetricCard("Base de donnees", "indisponible", "error")],
            "leagues": [selected_league],
            "runs": [],
            "model_run": None,
            "upcoming_matches": [],
            "ranked_deals": [],
            "horizon": horizon_code,
            "filters": filters,
            "view": active_view,
        }

    html = render_page(selected_league, data, report, error_message, current_user)
    start_response("200 OK", [("Content-Type", "text/html; charset=utf-8")])
    return [html.encode("utf-8")]


class SingleInstanceServer(_ThreadingMixIn, WSGIServer):
    # Multi-thread : chaque requete a son thread -> le site reste vivant
    # pendant les actions longues (chaque requete ouvre sa propre connexion
    # PostgreSQL, donc pas d'etat partage cote DB).
    daemon_threads = True
    # Sur Windows, wsgiref active allow_reuse_address par defaut : deux
    # dashboards peuvent alors se lier au MEME port 8501 et se voler les
    # requetes (vu en production le 2026-07-03). En l'interdisant, la
    # deuxieme instance echoue immediatement avec un message clair.
    allow_reuse_address = False

    def server_bind(self):
        # SO_EXCLUSIVEADDRUSE (Windows) : interdit le double-bind (le but
        # de E21) SANS etre bloque par les connexions TIME_WAIT residuelles
        # d'un arret recent — sinon le redemarrage echoue pendant ~2 minutes
        # alors qu'AUCUN serveur ne tourne (vu le 2026-07-05).
        import socket as _socket
        if hasattr(_socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(_socket.SOL_SOCKET, _socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def main() -> int:
    host = "127.0.0.1"
    port = 8501
    try:
        httpd = make_server(host, port, application, server_class=SingleInstanceServer)
    except OSError:
        print(f"ERREUR: le port {port} est deja occupe - un dashboard tourne probablement deja.")
        print(f"Ouvre simplement http://{host}:{port} dans le navigateur,")
        print("ou ferme l'instance existante (Ctrl+C dans son terminal) avant de relancer.")
        return 1
    with httpd:
        print(f"Dashboard V1 disponible sur http://{host}:{port}")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("Arret du dashboard.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
