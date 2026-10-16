"""Never let tests write mission data into the developer's installation."""

import os

import pytest

for name in ("API_SECRET_KEY", "API_ACCESS_KEY", "FLASK_SESSION_SECRET"):
    os.environ[name] = f"test-{name.lower()}"
os.environ["GOOGLE_MAPS_API_KEY"] = ""
os.environ["PHONE_TAILSCALE_IP"] = ""


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path, monkeypatch):
    from mock_gps import db, history, logger

    logger.close_task_logs()
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    monkeypatch.setattr(logger, "LOG_ROOT", tmp_path / "logs")
    monkeypatch.setattr(history, "ARCHIVE_ROOT", tmp_path / "logs" / "archives")
    monkeypatch.setattr(history, "ARCHIVE_CACHE_ROOT", tmp_path / "logs" / ".archive-cache")
    yield
    logger.close_task_logs()
