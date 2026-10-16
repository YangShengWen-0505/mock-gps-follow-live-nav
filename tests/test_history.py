import csv
import json
import os
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

from mock_gps import history, logger


def _row(sequence: int, action: str = "Walking", note: str = "") -> list[str]:
    return [
        str(sequence), "2026-01-01 00:00:00.000Z", "25", "121", action, note,
        "2026-01-01T00:00:00.000+00:00", "1.000", "0.00",
    ]


def _session_with_movement(root: Path, date_value: str, session_value: str, stops: int = 2) -> Path:
    session = root / date_value / session_value
    session.mkdir(parents=True)
    with (session / "movement.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(logger.MOVEMENT_FIELDS)
        writer.writerow(_row(1))
    (session / "mission.json").write_text(
        json.dumps({
            "created_at": f"{date_value}T12:00:00+00:00",
            "mission": {"payload": {
                "init_loc": "25,121",
                "stops": [{"name": f"S{i}"} for i in range(stops)],
            }},
        }),
        encoding="utf-8",
    )
    (session / "route.log").write_text("route data", encoding="utf-8")
    return session


def _history_roots(tmp_path, monkeypatch) -> Path:
    log_root = tmp_path / "logs"
    monkeypatch.setattr(logger, "LOG_ROOT", log_root)
    monkeypatch.setattr(history, "ARCHIVE_ROOT", log_root / "archives")
    monkeypatch.setattr(history, "ARCHIVE_CACHE_ROOT", log_root / ".archive-cache")
    return log_root


def test_movement_cursor_preserves_sequence_across_pages(tmp_path):
    path = tmp_path / "movement.csv"
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(logger.MOVEMENT_FIELDS)
        writer.writerow(_row(1))
        writer.writerow(_row(2, "MRT", "台北車站"))
    first = history.read_movement_records(path, 0, limit=1)
    second = history.read_movement_records(path, first["next_offset"], limit=1)
    assert [first["records"][0]["sequence"], second["records"][0]["sequence"]] == [1, 2]
    assert second["records"][0]["note"] == "台北車站"
    assert first["has_more"] is True
    assert second["has_more"] is False


def test_cursor_resets_when_offset_exceeds_file(tmp_path):
    path = tmp_path / "movement.csv"
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(logger.MOVEMENT_FIELDS)
        writer.writerow(_row(1))
    page = history.read_movement_records(path, 99999)
    assert page["records"][0]["sequence"] == 1


def test_expired_session_is_verified_archived_and_all_files_replayable(tmp_path, monkeypatch):
    log_root = _history_roots(tmp_path, monkeypatch)
    old_date = (datetime.now() - timedelta(days=31)).strftime("%Y-%m-%d")
    session = _session_with_movement(log_root, old_date, "12-00-00")
    archived = history.archive_expired_sessions(30)
    assert archived == [f"{old_date}__12-00-00"]
    assert not session.exists()
    assert history.read_history_log(old_date, "12-00-00", "route") == "route data"
    restored = history.resolve_history_movement(old_date, "12-00-00")
    assert restored.exists()
    assert (restored.parent / ".touched").exists()


def test_invalid_existing_archive_is_rebuilt_before_source_deletion(tmp_path, monkeypatch):
    log_root = _history_roots(tmp_path, monkeypatch)
    archive_root = log_root / "archives"
    old_date = (datetime.now() - timedelta(days=31)).strftime("%Y-%m-%d")
    session = log_root / old_date / "12-00-00"
    session.mkdir(parents=True)
    (session / "movement.csv").write_text("broken source", encoding="utf-8")
    archive_root.mkdir(parents=True)
    (archive_root / f"{old_date}__12-00-00.zip").write_bytes(b"not a zip")
    assert history.archive_expired_sessions(30) == [f"{old_date}__12-00-00"]
    assert not session.exists()
    assert history._verified_archive(archive_root / f"{old_date}__12-00-00.zip")


def test_archived_sessions_stay_visible_when_unarchived_fill_the_limit(tmp_path, monkeypatch):
    log_root = _history_roots(tmp_path, monkeypatch)
    old_date = (datetime.now() - timedelta(days=31)).strftime("%Y-%m-%d")
    _session_with_movement(log_root, old_date, "12-00-00")
    assert history.archive_expired_sessions(30)

    for index in range(3):
        _session_with_movement(log_root, "2026-08-04", f"10-00-0{index}")

    assert len(history.list_history_sessions(limit=2)) == 2
    listed = history.list_history_sessions(limit=10)
    # The archived session must remain reachable even though three unarchived
    # ones exist, and the merged list must be newest-first.
    assert f"{old_date}/12-00-00" in [item["id"] for item in listed]
    timestamps = [item["updated_at"] for item in listed]
    assert timestamps == sorted(timestamps, reverse=True)


def test_archiving_does_not_restamp_an_old_session_as_the_newest(tmp_path, monkeypatch):
    # The zip is written today, so using its mtime as the sort key made a
    # month-old session jump above every mission that ran today.
    log_root = _history_roots(tmp_path, monkeypatch)
    old_date = (datetime.now() - timedelta(days=45)).strftime("%Y-%m-%d")
    today = datetime.now().strftime("%Y-%m-%d")
    old_session = _session_with_movement(log_root, old_date, "12-00-00")
    # Do not depend on two file writes landing in different filesystem ticks.
    old_stamp = datetime.strptime(old_date, "%Y-%m-%d").timestamp()
    os.utime(old_session / "movement.csv", (old_stamp, old_stamp))
    _session_with_movement(log_root, today, "08-00-00")

    assert history.archive_expired_sessions(30) == [f"{old_date}__12-00-00"]

    listed = history.list_history_sessions()
    assert [item["id"] for item in listed] == [f"{today}/08-00-00", f"{old_date}/12-00-00"]
    archived = next(item for item in listed if item["archived"])
    assert archived["updated_at"] < next(
        item for item in listed if not item["archived"]
    )["updated_at"]


def test_archive_without_a_sidecar_timestamp_falls_back_to_its_session_name(tmp_path, monkeypatch):
    # Archives written before the sidecar carried updated_at must not sort by
    # the zip's mtime, which is when the sweep ran rather than when the run was.
    log_root = _history_roots(tmp_path, monkeypatch)
    old_date = (datetime.now() - timedelta(days=45)).strftime("%Y-%m-%d")
    _session_with_movement(log_root, old_date, "12-00-00")
    assert history.archive_expired_sessions(30)
    meta = history.ARCHIVE_ROOT / f"{old_date}__12-00-00.meta.json"
    stored = json.loads(meta.read_text(encoding="utf-8"))
    stored.pop("updated_at", None)
    meta.write_text(json.dumps(stored), encoding="utf-8")

    entry = next(item for item in history.list_history_sessions() if item["archived"])
    assert entry["updated_at"].startswith(old_date)


def test_history_listing_supports_offset_paging(tmp_path, monkeypatch):
    log_root = _history_roots(tmp_path, monkeypatch)
    for index in range(4):
        _session_with_movement(log_root, "2026-08-04", f"10-00-0{index}")
    page_one = history.list_history_sessions(limit=2, offset=0)
    page_two = history.list_history_sessions(limit=2, offset=2)
    assert len(page_one) == len(page_two) == 2
    assert not {item["id"] for item in page_one} & {item["id"] for item in page_two}


def test_archive_sidecar_preserves_origin_and_stop_count(tmp_path, monkeypatch):
    log_root = _history_roots(tmp_path, monkeypatch)
    old_date = (datetime.now() - timedelta(days=31)).strftime("%Y-%m-%d")
    _session_with_movement(log_root, old_date, "12-00-00", stops=3)
    assert history.archive_expired_sessions(30)

    entry = next(item for item in history.list_history_sessions() if item["archived"])
    assert entry["start"] == "25,121"
    assert entry["stops"] == 3
    assert entry["created_at"] == f"{old_date}T12:00:00+00:00"


def test_archived_extraction_is_atomic_and_complete(tmp_path, monkeypatch):
    log_root = _history_roots(tmp_path, monkeypatch)
    old_date = (datetime.now() - timedelta(days=31)).strftime("%Y-%m-%d")
    _session_with_movement(log_root, old_date, "12-00-00")
    assert history.archive_expired_sessions(30)

    restored = history.resolve_history_movement(old_date, "12-00-00")
    assert restored.read_text(encoding="utf-8").startswith("Sequence,")
    # No partially written temp files may survive a successful extraction.
    assert not list(restored.parent.glob("*.tmp"))


def test_current_session_logs_fall_back_to_the_archive(tmp_path, monkeypatch):
    log_root = _history_roots(tmp_path, monkeypatch)
    old_date = (datetime.now() - timedelta(days=31)).strftime("%Y-%m-%d")
    session = _session_with_movement(log_root, old_date, "12-00-00")
    stored_session_dir = str(session.resolve())
    assert history.archive_expired_sessions(30)
    assert not session.exists()

    # /api/log/<name> and /api/movements/current resolve by the stored path.
    assert history.log_path_for_session(stored_session_dir, "route").read_text(
        encoding="utf-8"
    ) == "route data"
    assert history.movement_path_for_session(stored_session_dir).exists()


def test_non_date_directories_are_not_listed_as_sessions(tmp_path, monkeypatch):
    log_root = _history_roots(tmp_path, monkeypatch)
    _session_with_movement(log_root, "2026-08-04", "10-00-00")
    stray = log_root / "scratch" / "notes"
    stray.mkdir(parents=True)
    (stray / "movement.csv").write_text("Sequence\n", encoding="utf-8")
    assert [item["id"] for item in history.list_history_sessions()] == ["2026-08-04/10-00-00"]


def test_archive_rebuilds_valid_but_stale_zip_before_deleting_source(tmp_path, monkeypatch):
    root = _history_roots(tmp_path, monkeypatch)
    date = (datetime.now() - timedelta(days=45)).strftime("%Y-%m-%d")
    session = _session_with_movement(root, date, "12-00-00")
    history.ARCHIVE_ROOT.mkdir()
    archive = history.ARCHIVE_ROOT / f"{date}__12-00-00.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("movement.csv", "stale")
    (session / "route.log").write_text("new data after previous archive", encoding="utf-8")
    assert history.archive_expired_sessions()
    assert history.read_history_log(date, "12-00-00", "route") == "new data after previous archive"


def test_archive_excludes_active_and_open_sessions(tmp_path, monkeypatch):
    root = _history_roots(tmp_path, monkeypatch)
    date = (datetime.now() - timedelta(days=45)).strftime("%Y-%m-%d")
    active = _session_with_movement(root, date, "12-00-00")
    (active / "mission.json").write_text(json.dumps({"mission": {"status": "running"}}), encoding="utf-8")
    opened = _session_with_movement(root, date, "13-00-00")
    monkeypatch.setattr(logger, "current_session_dir", opened)
    assert history.archive_expired_sessions() == []
    assert active.exists() and opened.exists()


def test_archive_keeps_source_if_verification_detects_change(tmp_path, monkeypatch):
    root = _history_roots(tmp_path, monkeypatch)
    date = (datetime.now() - timedelta(days=45)).strftime("%Y-%m-%d")
    session = _session_with_movement(root, date, "12-00-00")
    monkeypatch.setattr(history, "_archive_matches", lambda *_a: False)
    assert history.archive_expired_sessions() == []
    assert (session / "movement.csv").exists()
