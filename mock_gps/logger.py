"""Process and per-mission logging.

Mission history and archiving live in history.py.
"""

import csv
import json
import logging
import os
import sys
import threading
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

from mock_gps import config

BASE_DIR = Path(__file__).resolve().parent.parent
LOG_ROOT = Path(os.getenv("MOCK_GPS_LOG_ROOT", BASE_DIR / "logs"))
MOVEMENT_FIELDS = [
    "Sequence", "Timestamp", "Latitude", "Longitude", "Action", "Note",
    "TimestampISO", "DeltaSeconds", "DistanceMeters",
]

current_session_dir: Path | None = None
current_csv_file: Path | None = None
_csv_file_handle = None
_csv_writer = None
_movement_sequence = 0
_csv_lock = threading.Lock()
_handler_lock = threading.Lock()
_process_handler: logging.Handler | None = None

sys_logger = logging.getLogger("MockGps.system")
route_logger = logging.getLogger("MockGps.route")
security_logger = logging.getLogger("MockGps.security")


class TerminalFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno >= logging.WARNING or record.name == route_logger.name


class _ConfiguredTimezoneFormatter(logging.Formatter):
    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        value = datetime.fromtimestamp(record.created, config.TIMEZONE)
        return value.strftime(datefmt) if datefmt else value.isoformat(timespec="milliseconds")


def _formatter(with_date: bool = False) -> logging.Formatter:
    datefmt = "%Y-%m-%d %H:%M:%S" if with_date else "%H:%M:%S"
    return _ConfiguredTimezoneFormatter(
        "[%(asctime)s] [%(levelname)s] %(message)s",
        datefmt=datefmt,
    )


def _file_handler(path: Path, level: int, with_date: bool = False) -> logging.FileHandler:
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(path, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
    handler.setLevel(level)
    handler.setFormatter(_formatter(with_date=with_date))
    return handler


def _rotating_handler(path: Path, level: int) -> RotatingFileHandler:
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        path, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    handler.setLevel(level)
    handler.setFormatter(_formatter(with_date=True))
    return handler


def _terminal_handler() -> logging.StreamHandler:
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.addFilter(TerminalFilter())
    return handler


def _attach_handlers(
    logger_obj: logging.Logger,
    handlers: list[logging.Handler],
    level: int = logging.DEBUG,
) -> None:
    removed: list[logging.Handler] = []
    for old_handler in logger_obj.handlers[:]:
        if old_handler not in handlers:
            removed.append(old_handler)
        logger_obj.removeHandler(old_handler)
    logger_obj.setLevel(level)
    logger_obj.propagate = False
    for handler in handlers:
        logger_obj.addHandler(handler)
    active = {
        handler
        for managed_logger in (sys_logger, route_logger, security_logger)
        for handler in managed_logger.handlers
    }
    for old_handler in removed:
        if old_handler not in active:
            old_handler.close()


def init_server_logs() -> None:
    """Attach the always-on process log.

    start_local.py is the only entry point, so the web and the worker always
    share this one file.
    """
    global _process_handler
    LOG_ROOT.mkdir(parents=True, exist_ok=True)
    with _handler_lock:
        _process_handler = _rotating_handler(LOG_ROOT / "app.log", logging.DEBUG)
        _attach_handlers(sys_logger, [_process_handler, _terminal_handler()])
        _attach_handlers(security_logger, [_process_handler])
        _attach_handlers(route_logger, [_process_handler, _terminal_handler()])


def _close_csv() -> None:
    global _csv_file_handle, _csv_writer
    with _csv_lock:
        if _csv_file_handle:
            try:
                _csv_file_handle.flush()
                _csv_file_handle.close()
            finally:
                _csv_file_handle = None
                _csv_writer = None


def init_task_logs(mission_id: int | None = None, *, session_id: str | None = None) -> dict[str, str | None]:
    global current_session_dir, current_csv_file
    global _csv_file_handle, _csv_writer, _movement_sequence

    _close_csv()
    now = config.local_now()
    suffix = f"-{mission_id}" if mission_id is not None else ""
    current_session_dir = LOG_ROOT / now.strftime("%Y-%m-%d") / f"{now.strftime('%H-%M-%S')}{suffix}"
    if session_id:
        current_session_dir = LOG_ROOT / session_id
    current_session_dir.mkdir(parents=True, exist_ok=True)
    current_csv_file = current_session_dir / "movement.csv"
    _movement_sequence = 0
    new_file = not current_csv_file.exists() or current_csv_file.stat().st_size == 0

    with _csv_lock:
        _csv_file_handle = current_csv_file.open(mode="a", newline="", encoding="utf-8")
        _csv_writer = csv.writer(_csv_file_handle)
        if new_file:
            _csv_writer.writerow(MOVEMENT_FIELDS)
        _csv_file_handle.flush()

    all_handler = _file_handler(current_session_dir / "all.log", logging.DEBUG)
    error_handler = _file_handler(current_session_dir / "error.log", logging.WARNING)
    route_handler = _file_handler(current_session_dir / "route.log", logging.INFO)
    security_handler = _file_handler(current_session_dir / "security.log", logging.INFO)
    process_handlers = [_process_handler] if _process_handler else []
    with _handler_lock:
        _attach_handlers(sys_logger, [all_handler, error_handler, *process_handlers, _terminal_handler()])
        _attach_handlers(route_logger, [route_handler, all_handler, *process_handlers, _terminal_handler()])
        _attach_handlers(security_logger, [security_handler, all_handler, *process_handlers])

    log_sys(f"Task log session created: {current_session_dir}")
    return get_log_status()


def close_task_logs() -> None:
    global current_session_dir, current_csv_file
    _close_csv()
    with _handler_lock:
        handlers = [_process_handler] if _process_handler else []
        _attach_handlers(sys_logger, [*handlers, _terminal_handler()])
        _attach_handlers(route_logger, [*handlers, _terminal_handler()])
        _attach_handlers(security_logger, handlers)
        current_session_dir = None
        current_csv_file = None


def get_log_status() -> dict[str, str | None]:
    return {
        "session_dir": str(current_session_dir.resolve()) if current_session_dir else None,
        "movement_csv": str(current_csv_file.resolve()) if current_csv_file else None,
    }


def write_mission_snapshot(mission: dict) -> None:
    if not current_session_dir:
        return
    try:
        snapshot = {"created_at": datetime.now(timezone.utc).isoformat(), "mission": mission}
        (current_session_dir / "mission.json").write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as exc:
        log_sys(f"Mission snapshot write failed: {exc}", "warning")


def log_sys(message: str, level: str = "info") -> None:
    getattr(sys_logger, level.lower(), sys_logger.info)(message)


def log_route(message: str) -> None:
    route_logger.info("[ROUTE] %s", message)


def log_security(message: str, level: str = "info") -> None:
    getattr(security_logger, level.lower(), security_logger.info)(message)


def log_movement(
    lat: float,
    lng: float,
    action: str,
    note: str = "",
    *,
    sent_at: float | None = None,
    delta_seconds: float | None = None,
    distance_meters: float | None = None,
) -> None:
    global _movement_sequence
    if not _csv_writer or not _csv_file_handle:
        return
    sent_dt = datetime.fromtimestamp(sent_at, timezone.utc) if sent_at else datetime.now(timezone.utc)
    timestamp_iso = sent_dt.isoformat(timespec="milliseconds")
    timestamp = sent_dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] + "Z"
    delta_value = "" if delta_seconds is None else f"{delta_seconds:.3f}"
    distance_value = "" if distance_meters is None else f"{distance_meters:.2f}"
    safe_action = str(action).replace("\r", " ").replace("\n", " ")
    safe_note = str(note).replace("\r", " ").replace("\n", " ")
    try:
        with _csv_lock:
            _movement_sequence += 1
            _csv_writer.writerow([
                _movement_sequence, timestamp, f"{float(lat):.7f}", f"{float(lng):.7f}",
                safe_action, safe_note, timestamp_iso, delta_value, distance_value,
            ])
            _csv_file_handle.flush()
    except Exception as exc:
        log_sys(f"CSV write failed: {exc}", "error")
