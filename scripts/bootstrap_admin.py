from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import secrets
import sys


ROOT_DIR = Path(__file__).resolve().parents[1]
PREDICTION_SRC = ROOT_DIR / "services" / "prediction" / "src"
if str(PREDICTION_SRC) not in sys.path:
    sys.path.insert(0, str(PREDICTION_SRC))

from spe_prediction.config import get_env
from spe_prediction.db import DatabaseSettings, connect_db


PBKDF2_ITERATIONS = 260_000


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("ascii"), PBKDF2_ITERATIONS
    ).hex()
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt}${digest}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Create or update the first BP//EDGE admin user.")
    parser.add_argument("--email", default=get_env("BOOTSTRAP_ADMIN_EMAIL", "admin@bp-edge.local"))
    parser.add_argument("--name", default=get_env("BOOTSTRAP_ADMIN_NAME", "BP Edge Admin"))
    parser.add_argument("--password", default=get_env("BOOTSTRAP_ADMIN_PASSWORD", ""))
    args = parser.parse_args()

    email = args.email.strip().lower()
    name = args.name.strip() or "BP Edge Admin"
    password = args.password or os.getenv("BOOTSTRAP_ADMIN_PASSWORD", "")
    min_len = int(get_env("PASSWORD_MIN_LENGTH", "10") or "10")
    if len(password) < min_len:
        print(f"Password too short: expected at least {min_len} characters.")
        print("Set BOOTSTRAP_ADMIN_PASSWORD in .env or pass --password.")
        return 2

    password_hash = hash_password(password)
    connection = connect_db(DatabaseSettings.from_env())
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO app_auth.users (email, display_name, password_hash, role_code, status_code)
                VALUES (%s, %s, %s, 'ADMIN', 'ACTIVE')
                ON CONFLICT (lower(email)) WHERE deleted_at IS NULL
                DO UPDATE SET
                    display_name = EXCLUDED.display_name,
                    password_hash = EXCLUDED.password_hash,
                    role_code = 'ADMIN',
                    status_code = 'ACTIVE',
                    updated_at = now(),
                    deleted_at = NULL
                RETURNING user_id
                """,
                (email, name, password_hash),
            )
            user_id = int(cursor.fetchone()[0])
            cursor.execute(
                """
                INSERT INTO model.user_bankrolls (user_id, current_amount)
                VALUES (%s, 1000)
                ON CONFLICT (user_id) WHERE status_code = 'ACTIVE'
                DO NOTHING
                """,
                (user_id,),
            )
            cursor.execute(
                """
                INSERT INTO app_auth.audit_log (actor_user_id, target_user_id, action_code, entity_type, entity_id)
                VALUES (%s, %s, 'ADMIN_BOOTSTRAP', 'USER', %s)
                """,
                (user_id, user_id, str(user_id)),
            )
        connection.commit()
    finally:
        connection.close()

    print(f"Admin ready: {email} (user_id={user_id})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
