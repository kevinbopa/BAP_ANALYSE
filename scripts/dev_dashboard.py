from __future__ import annotations

import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "apps" / "dashboard" / "app.py"
WATCH_ROOTS = [
    ROOT / "apps",
    ROOT / "services",
    ROOT / "db",
    ROOT / "scripts",
    ROOT / "tests",
    ROOT / "package.json",
]
WATCH_EXTENSIONS = {
    ".py",
    ".sql",
    ".json",
    ".md",
    ".txt",
    ".toml",
    ".yml",
    ".yaml",
    ".html",
    ".css",
    ".js",
}
IGNORED_DIRS = {
    ".git",
    ".agents",
    ".codex",
    ".vscode",
    "__pycache__",
    ".pytest_cache",
    "node_modules",
    "logs",
    "graphify-out",
}
HOST = "127.0.0.1"
PORT = 8501


def is_port_open(host: str = HOST, port: int = PORT, timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def wait_for_port_free(host: str = HOST, port: int = PORT, timeout: float = 8.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not is_port_open(host, port):
            return True
        time.sleep(0.2)
    return not is_port_open(host, port)


def listening_pids(port: int = PORT) -> set[int]:
    """Retourne les PID en LISTENING sur le port, sans droits admin."""
    try:
        result = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"],
            capture_output=True,
            text=True,
            errors="ignore",
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return set()
    pids: set[int] = set()
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[0].upper() != "TCP":
            continue
        local_address = parts[1]
        state = parts[3].upper()
        pid_raw = parts[4]
        if state != "LISTENING":
            continue
        if local_address.rsplit(":", 1)[-1] != str(port):
            continue
        if pid_raw.isdigit() and int(pid_raw) > 0:
            pids.add(int(pid_raw))
    return pids


def kill_process_tree(pid: int) -> None:
    if pid == os.getpid():
        return
    try:
        if os.name == "nt":
            subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    f"Stop-Process -Id {pid} -Force -ErrorAction SilentlyContinue",
                ],
                capture_output=True,
                text=True,
                timeout=8,
            )
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                text=True,
                timeout=8,
            )
        else:
            os.kill(pid, signal.SIGTERM)
    except (OSError, subprocess.TimeoutExpired):
        pass


def ensure_dashboard_port_available() -> None:
    """`npm run dev` doit toujours repartir proprement.

    Si une ancienne instance Python a gele, elle garde 8501 mais ne sert plus
    l'app. Le runner prend donc possession du port au demarrage, au lieu de
    demander a l'utilisateur de chasser les PID a la main.
    """
    pids = listening_pids()
    if pids:
        print(f"[dev] Port {PORT} deja utilise par PID {', '.join(map(str, sorted(pids)))}.")
        print("[dev] Nettoyage automatique de l'ancienne instance dashboard...")
        for pid in sorted(pids):
            kill_process_tree(pid)
    if not wait_for_port_free(timeout=10.0):
        raise RuntimeError(
            f"Impossible de liberer le port {PORT}. Ferme l'application qui l'utilise, "
            "puis relance `npm run dev`."
        )


def iter_watched_files() -> list[Path]:
    files: list[Path] = []
    for root in WATCH_ROOTS:
        if root.is_file():
            files.append(root)
            continue
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if any(part in IGNORED_DIRS for part in path.parts):
                continue
            if path.suffix.lower() in WATCH_EXTENSIONS:
                files.append(path)
    return files


def snapshot() -> dict[Path, int]:
    state: dict[Path, int] = {}
    for path in iter_watched_files():
        try:
            state[path] = path.stat().st_mtime_ns
        except OSError:
            continue
    return state


def start_server() -> subprocess.Popen:
    ensure_dashboard_port_available()
    env = os.environ.copy()
    env["SPE_DEV_RELOAD"] = "1"
    env["SPE_DEV_VERSION"] = str(int(time.time() * 1000))
    print(f"[dev] BP//EDGE dashboard: http://{HOST}:{PORT}")
    print("[dev] Live reload actif. Modifie un fichier, le serveur redemarre.")
    return subprocess.Popen(
        [sys.executable, str(APP)],
        cwd=str(ROOT),
        env=env,
    )


def stop_server(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=4)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=4)
    except OSError:
        pass
    wait_for_port_free(timeout=5.0)


def raise_keyboard_interrupt() -> None:
    raise KeyboardInterrupt


def main() -> int:
    process: subprocess.Popen | None = None
    try:
        process = start_server()
        current = snapshot()
        while True:
            time.sleep(1.0)
            if process.poll() is not None:
                print(f"[dev] Serveur arrete avec code {process.returncode}. Redemarrage...")
                if process.returncode not in (0, None):
                    time.sleep(1.0)
                process = start_server()
                current = snapshot()
                continue

            fresh = snapshot()
            if fresh != current:
                changed = sorted(
                    str(path.relative_to(ROOT))
                    for path, mtime in fresh.items()
                    if current.get(path) != mtime
                )
                if not changed:
                    changed = sorted(
                        str(path.relative_to(ROOT))
                        for path in set(current).symmetric_difference(fresh)
                    )
                print("[dev] Changement detecte:")
                for item in changed[:8]:
                    print(f"[dev]  - {item}")
                if len(changed) > 8:
                    print(f"[dev]  - ... +{len(changed) - 8}")
                stop_server(process)
                if not wait_for_port_free():
                    ensure_dashboard_port_available()
                process = start_server()
                current = fresh
    except KeyboardInterrupt:
        print("\n[dev] Arret demande.")
    except RuntimeError as exc:
        print(f"[dev] {exc}")
        return 1
    finally:
        if process is not None:
            stop_server(process)
    return 0


if __name__ == "__main__":
    if os.name == "nt":
        signal.signal(signal.SIGTERM, lambda *_args: raise_keyboard_interrupt())
    raise SystemExit(main())
