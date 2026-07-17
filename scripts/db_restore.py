from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys


ROOT_DIR = Path(__file__).resolve().parents[1]
PREDICTION_SRC = ROOT_DIR / "services" / "prediction" / "src"
if str(PREDICTION_SRC) not in sys.path:
    sys.path.insert(0, str(PREDICTION_SRC))
if str(ROOT_DIR / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT_DIR / "scripts"))

from postgres_cli import is_localhost, load_db_settings, resolve_pg_binary, resolve_setting


def build_command(
    mode: str,
    settings,
    docker_container: str | None,
    pg_restore_path: str | None,
) -> tuple[list[str], dict[str, str]]:
    env = {}
    common = [
        "--clean",
        "--if-exists",
        "--no-owner",
        "--no-privileges",
        "--single-transaction",
        "--exit-on-error",
        "--host",
        settings.host,
        "--port",
        str(settings.port),
        "--username",
        settings.user,
        "--dbname",
        settings.dbname,
    ]
    if mode == "binary":
        if not pg_restore_path:
            raise RuntimeError("pg_restore is not available in PATH and no pg bin directory was configured.")
        env["PGPASSWORD"] = settings.password
        return ([pg_restore_path, *common], env)
    if not docker_container:
        raise RuntimeError("Docker mode requires a container name.")
    return (
        [
            "docker",
            "exec",
            "-e",
            f"PGPASSWORD={settings.password}",
            "-i",
            docker_container,
            "pg_restore",
            *common,
        ],
        env,
    )


def pick_mode(requested: str, host: str, pg_restore_path: str | None, docker_container: str | None) -> str:
    if requested in {"binary", "docker"}:
        return requested
    if pg_restore_path:
        return "binary"
    if docker_container and is_localhost(host):
        return "docker"
    raise RuntimeError(
        "No restore mechanism available. Install pg_restore or configure a local Docker PostgreSQL container."
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Restore a PostgreSQL custom-format backup.")
    parser.add_argument("--input", required=True, help="Path to the .dump backup file.")
    parser.add_argument("--prefix", default="POSTGRES_MIGRATE", help="Primary env prefix for DB credentials.")
    parser.add_argument("--fallback-prefix", default="POSTGRES", help="Fallback env prefix for DB credentials.")
    parser.add_argument("--mode", choices=("auto", "binary", "docker"), default="auto")
    parser.add_argument("--docker-container", default="", help="Docker container name for local fallback.")
    parser.add_argument("--yes", action="store_true", help="Confirm the destructive restore.")
    args = parser.parse_args()

    if not args.yes:
        raise SystemExit("Refusing to restore without --yes.")

    input_path = Path(args.input).resolve()
    if not input_path.exists():
        raise SystemExit(f"Backup file not found: {input_path}")

    settings = load_db_settings(args.prefix, args.fallback_prefix, admin_defaults=True)
    pg_bin_dir = resolve_setting("PG_BIN_DIR", args.prefix, args.fallback_prefix, "")
    docker_container = args.docker_container or resolve_setting(
        "DOCKER_CONTAINER", args.prefix, args.fallback_prefix, "spe-postgres"
    )
    pg_restore_path = resolve_pg_binary("pg_restore", pg_bin_dir)
    mode = pick_mode(args.mode, settings.host, pg_restore_path, docker_container)
    command, extra_env = build_command(mode, settings, docker_container, pg_restore_path)

    env = None
    if extra_env:
        env = dict(os.environ)
        env.update(extra_env)

    with input_path.open("rb") as handle:
        result = subprocess.run(command, stdin=handle, stderr=subprocess.PIPE, env=env)
    if result.returncode != 0:
        raise SystemExit(result.stderr.decode("utf-8", errors="replace").strip() or "Restore failed.")

    print(f"Restore completed from: {input_path}")
    print(f"Mode: {mode}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
