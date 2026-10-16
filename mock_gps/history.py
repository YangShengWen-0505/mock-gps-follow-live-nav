"""Mission history: session listing, replay, and ZIP archiving.

Everything here reads what logger.py wrote. The two are split because archiving
is a storage concern that merely happens to live under logs/ -- it shares no
state with the logging handlers beyond the root directory.

logger is imported as a module, not by name, so tests that monkeypatch
logger.LOG_ROOT reach this side too.
"""

import csv
import io
import json
import os
import shutil
import threading
import zipfile
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mock_gps import config, db, logger

SESSION_LOCK = threading.RLock()

ARCHIVE_ROOT = logger.LOG_ROOT / "archives"
ARCHIVE_CACHE_ROOT = logger.LOG_ROOT / ".archive-cache"
HISTORY_FILES = {
    "all": "all.log",
    "route": "route.log",
    "error": "error.log",
    "security": "security.log",
    "movement": "movement.csv",
    "mission": "mission.json",
}


def _validate_history_parts(date_value: str, session_value: str) -> None:
    try:
        datetime.strptime(date_value, "%Y-%m-%d")
    except (TypeError, ValueError) as exc:
        raise FileNotFoundError("History session not found") from exc
    if not session_value or any(char not in "0123456789-" for char in session_value):
        raise FileNotFoundError("History session not found")


def _resolve_history_session(date_value: str, session_value: str) -> Path:
    _validate_history_parts(date_value, session_value)
    session_dir = (logger.LOG_ROOT / date_value / session_value).resolve()
    if logger.LOG_ROOT.resolve() not in session_dir.parents or not session_dir.is_dir():
        raise FileNotFoundError("History session not found")
    return session_dir


def _read_mission_metadata(session_dir: Path) -> dict:
    path = session_dir / "mission.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _list_unarchived_sessions(limit: int | None = 100) -> list[dict]:
    sessions: list[dict] = []
    if not logger.LOG_ROOT.exists():
        return sessions
    for date_dir in sorted(logger.LOG_ROOT.iterdir(), reverse=True):
        if not date_dir.is_dir() or date_dir.name.startswith(".") or date_dir == ARCHIVE_ROOT:
            continue
        try:
            # Match archive_expired_sessions(): only real date directories are
            # sessions, so unrelated folders under logs/ are never listed.
            datetime.strptime(date_dir.name, "%Y-%m-%d")
        except ValueError:
            continue
        for session_dir in sorted(date_dir.iterdir(), reverse=True):
            movement = session_dir / "movement.csv"
            if not session_dir.is_dir() or not movement.exists():
                continue
            metadata = _read_mission_metadata(session_dir)
            mission = metadata.get("mission", {})
            payload = mission.get("payload", mission)
            stat = movement.stat()
            sessions.append({
                "date": date_dir.name,
                "session": session_dir.name,
                "id": f"{date_dir.name}/{session_dir.name}",
                "created_at": metadata.get("created_at"),
                "start": payload.get("init_loc", ""),
                "stops": len(payload.get("stops", [])),
                "csv_size": stat.st_size,
                "updated_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
                "archived": False,
            })
            if limit is not None and len(sessions) >= limit:
                return sessions
    return sessions


def _archive_meta_path(archive_path: Path) -> Path:
    return archive_path.with_suffix(".meta.json")


def _session_started_at(date_value: str, session_value: str) -> str | None:
    """Session start time recovered from the archive name, as UTC ISO.

    Session directories are named with local time, so the name is a reliable
    stand-in whenever the sidecar cannot supply the real timestamp.
    """
    parts = session_value.split("-")
    if len(parts) < 3:
        return None
    try:
        stamp = datetime.strptime(f"{date_value} {'-'.join(parts[:3])}", "%Y-%m-%d %H-%M-%S")
    except ValueError:
        return None
    return stamp.replace(tzinfo=config.TIMEZONE).astimezone(timezone.utc).isoformat()


def _archive_metadata() -> list[dict]:
    if not ARCHIVE_ROOT.exists():
        return []
    result = []
    for path in sorted(ARCHIVE_ROOT.glob("*.zip"), reverse=True):
        if "__" not in path.stem:
            continue
        date_value, session_value = path.stem.split("__", 1)
        stat = path.stat()
        entry = {
            "date": date_value,
            "session": session_value,
            "id": f"{date_value}/{session_value}",
            "archived": True,
            "created_at": None,
            "start": "",
            "stops": 0,
            "csv_size": stat.st_size,
        }
        # The sidecar is written at archive time so listing never has to open
        # every zip just to show the origin and stop count.
        meta_path = _archive_meta_path(path)
        if meta_path.exists():
            try:
                entry.update(json.loads(meta_path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                pass
        if not entry.get("updated_at"):
            # Never fall back to the zip's own mtime: that is when the sweep
            # ran, so a month-old session would sort above today's missions.
            # Archives written before the sidecar carried updated_at land here.
            entry["updated_at"] = _session_started_at(date_value, session_value) or (
                datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat()
            )
        result.append(entry)
    return result


def list_history_sessions(limit: int = 100, offset: int = 0, *, sort_by: str = "updated_at") -> list[dict]:
    # Both sources must be enumerated before slicing: taking unarchived first
    # meant a backlog of 100 live sessions hid every archived one permanently.
    sessions = _list_unarchived_sessions(limit + offset if sort_by == "id" else None) + _archive_metadata()
    sessions.sort(key=lambda item: item.get(sort_by) or "", reverse=True)
    return sessions[offset : offset + limit]


def _archive_path(date_value: str, session_value: str) -> Path:
    _validate_history_parts(date_value, session_value)
    return ARCHIVE_ROOT / f"{date_value}__{session_value}.zip"


def _touch_cache(target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    (target / ".touched").touch()


def resolve_history_file(date_value: str, session_value: str, kind: str) -> Path:
    filename = HISTORY_FILES.get(kind)
    if not filename:
        raise ValueError("Unsupported history file")
    try:
        path = _resolve_history_session(date_value, session_value) / filename
        if not path.exists():
            raise FileNotFoundError(f"History {kind} data not found")
        return path
    except FileNotFoundError:
        archive = _archive_path(date_value, session_value)
        if not archive.exists():
            raise
        target = ARCHIVE_CACHE_ROOT / date_value / session_value
        path = target / filename
        _touch_cache(target)
        if not path.exists() or path.stat().st_mtime_ns < archive.stat().st_mtime_ns:
            # Waitress runs 8 threads; two concurrent requests would otherwise
            # interleave writes into the same file. Same-directory replace is
            # atomic, so a reader either sees no file or a complete one.
            temp = path.with_name(f"{path.name}.{os.getpid()}-{threading.get_ident()}.tmp")
            try:
                with zipfile.ZipFile(archive, "r") as bundle:
                    with bundle.open(filename) as source, temp.open("wb") as destination:
                        shutil.copyfileobj(source, destination)
                os.replace(temp, path)
            except KeyError as exc:
                temp.unlink(missing_ok=True)
                raise FileNotFoundError(f"Archived {kind} data not found") from exc
            except BaseException:
                temp.unlink(missing_ok=True)
                raise
        return path


def resolve_history_movement(date_value: str, session_value: str) -> Path:
    return resolve_history_file(date_value, session_value, "movement")


def history_log_session_id(date_value: str, session_value: str) -> str:
    _validate_history_parts(date_value, session_value)
    return f"{date_value}/{session_value}"


def read_history_log(date_value: str, session_value: str, log_name: str) -> str:
    return read_log_tail(resolve_history_file(date_value, session_value, log_name))


def read_movement_records(path: Path, offset: int = 0, limit: int = 250) -> dict:
    if not path.exists():
        raise FileNotFoundError("Movement data not found")
    size = path.stat().st_size
    offset = max(0, int(offset))
    limit = max(1, min(int(limit), 1000))
    if offset > size:
        offset = 0
    records = []
    with path.open("rb") as handle:
        handle.seek(offset)
        if offset == 0:
            handle.readline()
        while len(records) < limit:
            row_offset = handle.tell()
            raw = handle.readline()
            if raw and not raw.endswith(b"\n"):
                handle.seek(row_offset)
                break
            if not raw:
                break
            try:
                row = next(csv.reader(io.StringIO(raw.decode("utf-8"))))
                values = dict(zip(logger.MOVEMENT_FIELDS, row, strict=False))
                records.append({
                    "sequence": int(values["Sequence"]),
                    "time": values["TimestampISO"],
                    "lat": float(values["Latitude"]),
                    "lng": float(values["Longitude"]),
                    "action": values["Action"],
                    "note": values["Note"],
                })
            except (KeyError, TypeError, ValueError, csv.Error):
                continue
        next_offset = handle.tell()
    return {
        "records": records,
        "next_offset": next_offset,
        "stream_id": f"{path.parent.parent.name}/{path.parent.name}",
        "has_more": next_offset < size and bool(records),
    }


def _session_parts(session_dir: str | Path) -> tuple[str, str]:
    parts = db.session_key(str(session_dir)).split("/")
    return parts[0], parts[1]


def movement_path_for_session(session_dir: str | Path) -> Path:
    path = Path(session_dir).resolve() / "movement.csv"
    if logger.LOG_ROOT.resolve() in path.parents and path.exists():
        return path
    # The session may have been archived while it was still the latest mission,
    # which happens once no new mission runs for the retention period.
    return resolve_history_movement(*_session_parts(session_dir))


def log_path_for_session(session_dir: str | Path, log_name: str) -> Path:
    filename = HISTORY_FILES.get(log_name)
    if log_name not in {"all", "route", "error", "security"} or not filename:
        raise ValueError("Unsupported log name")
    path = Path(session_dir).resolve() / filename
    if logger.LOG_ROOT.resolve() in path.parents and path.exists():
        return path
    return resolve_history_file(*_session_parts(session_dir), log_name)


def cleanup_archive_cache(max_age_seconds: int = 86_400) -> int:
    # A day is far longer than any single browsing session, which closes the
    # window where a cache directory could be removed while it is being paged.
    if not ARCHIVE_CACHE_ROOT.exists():
        return 0
    cutoff = datetime.now().timestamp() - max_age_seconds
    removed = 0
    for marker in ARCHIVE_CACHE_ROOT.rglob(".touched"):
        if marker.stat().st_mtime < cutoff:
            shutil.rmtree(marker.parent, ignore_errors=True)
            removed += 1
    return removed


def _verified_archive(path: Path) -> bool:
    try:
        with zipfile.ZipFile(path, "r") as bundle:
            return bundle.testzip() is None and "movement.csv" in bundle.namelist()
    except (OSError, zipfile.BadZipFile):
        return False


def _write_archive_metadata(archive_path: Path, session_dir: Path) -> None:
    """Snapshot the listing fields while the session directory still exists."""
    try:
        metadata = _read_mission_metadata(session_dir)
        mission = metadata.get("mission", {})
        payload = mission.get("payload", mission)
        movement = session_dir / "movement.csv"
        entry = {
            "created_at": metadata.get("created_at"),
            "start": payload.get("init_loc", ""),
            "stops": len(payload.get("stops", [])),
            "csv_size": movement.stat().st_size if movement.exists() else 0,
        }
        if movement.exists():
            # The zip is written today, so its mtime says nothing about when the
            # run happened. Without the real session time here, archiving a
            # month-old session would sort it above today's missions.
            entry["updated_at"] = datetime.fromtimestamp(
                movement.stat().st_mtime, timezone.utc
            ).isoformat()
        _archive_meta_path(archive_path).write_text(
            json.dumps(entry, ensure_ascii=False), encoding="utf-8"
        )
    except OSError as exc:
        # The zip is already verified; a missing sidecar only degrades listing.
        logger.log_sys(f"Archive metadata write failed for {archive_path.name}: {exc}", "warning")


def read_log_tail(path: Path, max_bytes: int = 256 * 1024) -> str:
    with path.open("rb") as handle:
        size = path.stat().st_size
        handle.seek(max(0, size - max_bytes))
        if size > max_bytes:
            handle.readline()
        data = handle.read(max_bytes).decode("utf-8", errors="replace")
    notice = "[Showing the latest 256 KiB; older entries remain in rotated log files.]\n"
    return (notice if size > max_bytes else "") + data


def persist_mission(mission_id: int) -> None:
    try:
        _persist_mission(mission_id)
    except OSError as exc:
        # SQLite is authoritative; a full disk must not make an accepted API
        # request look rejected or turn a completed navigation into a failure.
        logger.log_sys(f"Mission {mission_id} snapshot deferred: {exc}", "warning")


def _persist_mission(mission_id: int) -> None:
    """Write a portable snapshot, rereading under the lock to avoid stale writers."""
    with SESSION_LOCK:
        mission = db.get_mission(mission_id)
        if not mission or not mission.get("log_session"):
            return
        date_value, session_value = _session_parts(mission["log_session"])
        _validate_history_parts(date_value, session_value)
        directory = logger.LOG_ROOT / date_value / session_value
        directory.mkdir(parents=True, exist_ok=True)
        movement = directory / "movement.csv"
        if not movement.exists():
            with movement.open("x", encoding="utf-8", newline="") as handle:
                csv.writer(handle).writerow(logger.MOVEMENT_FIELDS)
        target = directory / "mission.json"
        temporary = directory / "mission.json.tmp"
        temporary.write_text(json.dumps({"created_at": mission["created_at"], "mission": mission},
                                        ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(target)


def snapshot_mission(date_value: str, session_value: str) -> dict | None:
    try:
        value = json.loads(resolve_history_file(date_value, session_value, "mission").read_text(encoding="utf-8"))
        mission = value.get("mission", {})
        return mission if "status" in mission and "id" in mission else None
    except (FileNotFoundError, ValueError):
        return None


def _archive_matches(archive: Path, directory: Path) -> bool:
    with zipfile.ZipFile(archive) as bundle:
        files = [path for path in directory.rglob("*") if path.is_file()]
        if set(bundle.namelist()) != {path.relative_to(directory).as_posix() for path in files}:
            return False
        for path in files:
            checksum = 0
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    checksum = zlib.crc32(chunk, checksum)
            info = bundle.getinfo(path.relative_to(directory).as_posix())
            if info.file_size != path.stat().st_size or info.CRC != checksum:
                return False
    return True


def archive_expired_sessions(days: int = 30) -> list[str]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    ARCHIVE_ROOT.mkdir(parents=True, exist_ok=True)
    archived: list[str] = []
    for date_dir in list(logger.LOG_ROOT.iterdir()) if logger.LOG_ROOT.exists() else []:
        if not date_dir.is_dir() or date_dir.is_symlink():
            continue
        try:
            date_value = datetime.strptime(date_dir.name, "%Y-%m-%d").replace(tzinfo=config.TIMEZONE)
        except ValueError:
            continue
        if date_value >= cutoff:
            continue
        for session_dir in list(date_dir.iterdir()):
            with SESSION_LOCK:
                if not session_dir.is_dir() or session_dir.is_symlink():
                    continue
                if logger.current_session_dir and session_dir.resolve() == logger.current_session_dir.resolve():
                    continue
                mission = _read_mission_metadata(session_dir).get("mission", {})
                if mission.get("status") in {"planning", "queued", "running", "degraded"} or mission.get(
                    "is_holding_final_position"
                ):
                    continue
                archive_path = ARCHIVE_ROOT / f"{date_dir.name}__{session_dir.name}.zip"
                temp_path = archive_path.with_suffix(".zip.tmp")
                try:
                    # Always rebuild: an old valid archive can predate a failed
                    # deletion and must not authorize deleting newer records.
                    with zipfile.ZipFile(temp_path, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
                        for file in session_dir.rglob("*"):
                            if file.is_symlink():
                                raise RuntimeError("Refusing to archive a symlink")
                            if file.is_file():
                                bundle.write(file, file.relative_to(session_dir))
                    if not _verified_archive(temp_path) or not _archive_matches(temp_path, session_dir):
                        raise RuntimeError(f"Archive verification failed: {session_dir}")
                    temp_path.replace(archive_path)
                    _write_archive_metadata(archive_path, session_dir)
                    if logger.LOG_ROOT.resolve() not in session_dir.resolve().parents:
                        raise RuntimeError("Session escaped the log root")
                    shutil.rmtree(session_dir)
                    archived.append(archive_path.stem)
                except Exception as exc:
                    logger.log_sys(f"History archive deferred for {session_dir}: {exc}", "warning")
                finally:
                    temp_path.unlink(missing_ok=True)
        try:
            if not any(date_dir.iterdir()):
                date_dir.rmdir()
        except OSError:
            pass
    return archived
