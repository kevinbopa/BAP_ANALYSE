from __future__ import annotations

from pathlib import Path
import re
import sys


ROOT_DIR = Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = ROOT_DIR / "db" / "migrations"
MIGRATION_NAME_RE = re.compile(r"^\d{4}_[A-Za-z0-9_]+\.sql$")


RISK_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("DROP_TABLE", re.compile(r"\bDROP\s+TABLE\b", re.IGNORECASE)),
    ("DROP_COLUMN", re.compile(r"\bDROP\s+COLUMN\b", re.IGNORECASE)),
    (
        "RENAME_COLUMN",
        re.compile(r"\bALTER\s+TABLE\b.*\bRENAME\s+COLUMN\b", re.IGNORECASE),
    ),
    (
        "SET_NOT_NULL",
        re.compile(r"\bALTER\s+COLUMN\b.*\bSET\s+NOT\s+NULL\b", re.IGNORECASE),
    ),
    (
        "ALTER_COLUMN_TYPE",
        re.compile(r"\bALTER\s+COLUMN\b.*\bTYPE\b", re.IGNORECASE),
    ),
)


ALLOWED_RISKS: dict[str, dict[str, tuple[re.Pattern[str], ...]]] = {
    "0009_v1_player_match_data.sql": {
        "DROP_TABLE": (
            re.compile(r"DROP TABLE core\.fixture_lineups;", re.IGNORECASE),
        ),
    },
    "0038_v4_auth_bankroll_rbac.sql": {
        "SET_NOT_NULL": (
            re.compile(r"ALTER COLUMN user_id SET NOT NULL;", re.IGNORECASE),
        ),
    },
    "0041_v4_ingestion_run_observability.sql": {
        "SET_NOT_NULL": (
            re.compile(r"ALTER COLUMN trigger_source SET NOT NULL,", re.IGNORECASE),
            re.compile(r"ALTER COLUMN heartbeat_at SET NOT NULL,", re.IGNORECASE),
            re.compile(r"ALTER COLUMN host_name SET NOT NULL;", re.IGNORECASE),
        ),
    },
}


def iter_migration_files() -> list[Path]:
    files = sorted(MIGRATIONS_DIR.glob("*.sql"), key=lambda item: item.name)
    invalid = [path.name for path in files if not MIGRATION_NAME_RE.match(path.name)]
    if invalid:
        raise ValueError(f"Invalid migration filenames: {', '.join(invalid)}")
    if not files:
        raise ValueError(f"No migration files found in {MIGRATIONS_DIR}")
    return files


def is_allowed(file_name: str, risk_name: str, line: str) -> bool:
    file_allowances = ALLOWED_RISKS.get(file_name, {})
    for pattern in file_allowances.get(risk_name, ()):
        if pattern.search(line):
            return True
    return False


def main() -> int:
    try:
        files = iter_migration_files()
    except ValueError as exc:
        print(str(exc))
        return 2

    findings: list[str] = []
    for path in files:
        for lineno, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            stripped = raw_line.strip()
            if not stripped or stripped.startswith("--"):
                continue
            for risk_name, pattern in RISK_PATTERNS:
                if pattern.search(stripped) and not is_allowed(path.name, risk_name, stripped):
                    findings.append(f"{path.name}:{lineno}: {risk_name}: {stripped}")

    if findings:
        print("Unsafe migration operations detected:")
        for finding in findings:
            print(f"- {finding}")
        return 1

    print(f"Migration safety check succeeded for {len(files)} files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
