"""Single-file launcher for Mock GPS Follow Live Nav.

Run this file to start the dashboard and the mission worker in one process.
Stopping it (Ctrl+C, or VS Code's stop button) stops both.
"""

import os
import sys
import threading
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
os.chdir(BASE_DIR)
sys.path.insert(0, str(BASE_DIR))

_LOCK_PATH = BASE_DIR / "data" / "instance.lock"
_lock_handle = None


def _fail(message: str) -> None:
    print(f"[SYSTEM] {message}", file=sys.stderr, flush=True)
    raise SystemExit(1)


def acquire_instance_lock() -> bool:
    """Hold an OS-level lock for the process lifetime.

    The OS releases it even when the process is killed, which neither a PID file
    nor a bound port can guarantee on Windows.
    """
    global _lock_handle
    _LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    handle = _LOCK_PATH.open("a+b")
    handle.seek(0)
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return False
    _lock_handle = handle
    return True


def banner(host: str, port: int) -> str:
    lines = [
        "=====================================================",
        "[SYSTEM] Mock GPS Follow Live Nav starting (web + mission worker)",
        f"[SYSTEM] Dashboard : http://127.0.0.1:{port}/map",
    ]
    if host in {"127.0.0.1", "localhost", "::1"}:
        lines.append("[SYSTEM] Network   : local only (set BIND_HOST to your Tailscale IP to share)")
    elif host == "0.0.0.0":
        lines.append(f"[SYSTEM] Network   : all interfaces on port {port} -- exposed to the whole LAN")
    else:
        lines.append(f"[SYSTEM] Network   : http://{host}:{port}/map")
    lines.append("[SYSTEM] Stop      : press Ctrl+C, or use the stop button in VS Code")
    lines.append("=====================================================")
    return "\n".join(lines)


def main() -> int:
    try:
        from waitress import create_server
    except ModuleNotFoundError:
        _fail(
            "Dependencies are not installed. Run once:\n"
            "  py -3.12 -m venv .venv\n"
            "  .venv\\Scripts\\python -m pip install -r requirements-lock.txt"
        )

    if not acquire_instance_lock():
        _fail(f"Mock GPS Follow Live Nav is already running in {BASE_DIR}.")

    from mock_gps import config, create_app, db
    from mock_gps.core.navigation import run_worker

    host = os.getenv("BIND_HOST", "127.0.0.1").strip().strip('"') or "127.0.0.1"
    port = int(os.getenv("FLASK_PORT", "5050"))
    listen = config.listen_spec(host, port)

    app = create_app()
    try:
        server = create_server(app, listen=" ".join(listen), threads=8)
    except OSError as exc:
        _fail(
            f"Cannot bind {', '.join(listen)} ({exc}).\n"
            "         Check that BIND_HOST is an address this PC actually owns "
            "(Tailscale must be connected), and that FLASK_PORT is free."
        )

    # We hold the instance lock, so any surviving lease belongs to a process
    # that was killed; taking it over avoids a 30-second wait on every restart.
    db.clear_worker_lease()

    def start_web() -> None:
        # Start accepting tasks only after orphan recovery; otherwise a fresh
        # submission can be mistaken for a mission left behind by a restart.
        threading.Thread(target=server.run, name="web", daemon=True).start()
        print(banner(host, port), flush=True)

    try:
        # The worker owns the main thread so its existing SIGINT/SIGTERM
        # handlers drive shutdown for the whole process.
        run_worker(on_ready=start_web)
    finally:
        server.close()
        print("[SYSTEM] Mock GPS Follow Live Nav stopped.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
