from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import sys
from pathlib import Path
import re


ROOT_DIR = Path(__file__).resolve().parents[1]
PREDICTION_SRC = ROOT_DIR / "services" / "prediction" / "src"
if str(PREDICTION_SRC) not in sys.path:
    sys.path.insert(0, str(PREDICTION_SRC))
if str(ROOT_DIR / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT_DIR / "scripts"))

from postgres_cli import load_db_settings


LEDGER_TABLE = "public.schema_migrations"
MIGRATION_NAME_RE = re.compile(r"^\d{4}_[A-Za-z0-9_]+\.sql$")


@dataclass(frozen=True)
class MigrationFile:
    name: str
    path: Path
    checksum_sha256: str


def compute_checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_migration_files(migrations_dir: Path) -> list[MigrationFile]:
    files = sorted(migrations_dir.glob("*.sql"), key=lambda item: item.name)
    if not files:
        raise ValueError(f"No SQL migration files found in {migrations_dir}")

    seen_names: set[str] = set()
    migrations: list[MigrationFile] = []
    for path in files:
        if not MIGRATION_NAME_RE.match(path.name):
            raise ValueError(
                f"Invalid migration filename '{path.name}'. Expected 'NNNN_description.sql'."
            )
        if path.name in seen_names:
            raise ValueError(f"Duplicate migration filename '{path.name}'.")
        seen_names.add(path.name)
        migrations.append(
            MigrationFile(
                name=path.name,
                path=path,
                checksum_sha256=compute_checksum(path),
            )
        )
    return migrations


def prepare_ledger(connection) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {LEDGER_TABLE} (
                migration_name text PRIMARY KEY,
                checksum_sha256 text,
                applied_at timestamptz NOT NULL DEFAULT now()
            );
            """
        )
        cursor.execute(
            f"ALTER TABLE {LEDGER_TABLE} ADD COLUMN IF NOT EXISTS checksum_sha256 text;"
        )
    connection.commit()


def fetch_applied_migrations(connection) -> dict[str, str]:
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT migration_name, COALESCE(checksum_sha256, '')
            FROM {LEDGER_TABLE}
            ORDER BY migration_name
            """
        )
        rows = cursor.fetchall()
    return {str(name): str(checksum or "") for name, checksum in rows}


def validate_against_ledger(
    migrations: list[MigrationFile], applied: dict[str, str]
) -> list[MigrationFile]:
    missing_checksums: list[MigrationFile] = []
    for migration in migrations:
        recorded = applied.get(migration.name)
        if recorded is None:
            continue
        if recorded and recorded != migration.checksum_sha256:
            raise RuntimeError(
                f"Checksum drift detected for {migration.name}: "
                f"ledger={recorded}, file={migration.checksum_sha256}"
            )
        if not recorded:
            missing_checksums.append(migration)
    return missing_checksums


def select_pending_migrations(
    migrations: list[MigrationFile], applied: dict[str, str], target: str | None
) -> list[MigrationFile]:
    pending: list[MigrationFile] = []
    target_found = target is None
    for migration in migrations:
        if target is not None and migration.name == target:
            target_found = True
        if target is not None and not target_found:
            if migration.name not in applied:
                pending.append(migration)
            continue
        if target is None or migration.name <= target:
            if migration.name not in applied:
                pending.append(migration)
    if target is not None and not any(m.name == target for m in migrations):
        raise ValueError(f"Target migration '{target}' not found in {ROOT_DIR / 'db' / 'migrations'}")
    return pending


def backfill_checksums(connection, migrations: list[MigrationFile]) -> int:
    updated = 0
    with connection.cursor() as cursor:
        for migration in migrations:
            cursor.execute(
                f"""
                UPDATE {LEDGER_TABLE}
                SET checksum_sha256 = %s
                WHERE migration_name = %s
                  AND (checksum_sha256 IS NULL OR checksum_sha256 = '')
                """,
                (migration.checksum_sha256, migration.name),
            )
            updated += cursor.rowcount
    connection.commit()
    return updated


def apply_migration(connection, migration: MigrationFile) -> None:
    sql = migration.path.read_text(encoding="utf-8")
    with connection.cursor() as cursor:
        cursor.execute(sql)
        cursor.execute(
            f"""
            INSERT INTO {LEDGER_TABLE} (migration_name, checksum_sha256)
            VALUES (%s, %s)
            ON CONFLICT (migration_name) DO UPDATE
            SET checksum_sha256 = EXCLUDED.checksum_sha256
            """,
            (migration.name, migration.checksum_sha256),
        )
    connection.commit()


def print_plan(migrations: list[MigrationFile], applied: dict[str, str], target: str | None) -> None:
    for migration in migrations:
        if target is not None and migration.name > target:
            status = "DEFERRED"
        else:
            status = "APPLIED" if migration.name in applied else "PENDING"
        print(f"{status:8} {migration.name}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply SQL migrations from db/migrations.")
    parser.add_argument(
        "--migrations-dir",
        default=str(ROOT_DIR / "db" / "migrations"),
        help="Directory containing numbered .sql migration files.",
    )
    parser.add_argument(
        "--prefix",
        default="POSTGRES_MIGRATE",
        help="Primary environment variable prefix for DB credentials.",
    )
    parser.add_argument(
        "--fallback-prefix",
        default="POSTGRES",
        help="Fallback environment variable prefix when the primary one is absent.",
    )
    parser.add_argument(
        "--plan",
        action="store_true",
        help="Show applied/pending migrations without changing the database.",
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Validate migration filenames and ordering without connecting to the database.",
    )
    parser.add_argument(
        "--target",
        default=None,
        help="Apply or plan only up to and including the named migration file.",
    )
    args = parser.parse_args()

    migrations_dir = Path(args.migrations_dir).resolve()
    migrations = load_migration_files(migrations_dir)

    if args.validate_only:
        print(f"Validated {len(migrations)} migration files in {migrations_dir}.")
        return 0

    settings = load_db_settings(args.prefix, args.fallback_prefix, admin_defaults=True)
    import psycopg

    connection = psycopg.connect(settings.dsn())
    try:
        prepare_ledger(connection)
        applied = fetch_applied_migrations(connection)
        missing_checksums = validate_against_ledger(migrations, applied)
        if args.plan:
            print_plan(migrations, applied, args.target)
            if missing_checksums:
                print(f"INFO     {len(missing_checksums)} applied migrations will get checksum backfill.")
            return 0

        if missing_checksums:
            updated = backfill_checksums(connection, missing_checksums)
            if updated:
                print(f"Backfilled checksums for {updated} applied migrations.")
            applied = fetch_applied_migrations(connection)

        pending = select_pending_migrations(migrations, applied, args.target)
        if not pending:
            print("No pending migrations.")
            return 0

        for migration in pending:
            print(f"Applying {migration.name}...")
            apply_migration(connection, migration)
        print(f"Applied {len(pending)} migration(s).")
        return 0
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
