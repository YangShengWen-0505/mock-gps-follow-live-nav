import csv
import json
from pathlib import Path

import pytest

from mock_gps import config, create_app, db, history, logger
from mock_gps.api import middleware, routes


def _plan(destination: str = "A") -> dict:
    eta = db.utc_now()
    return {
        "initial_google_eta": eta,
        "routes": [{
            "origin": "start",
            "destination": destination,
            "arrival_at": eta,
            "departure_at": eta,
            "steps": [{"points": [[25.0, 121.0], [25.1, 121.1]]}],
        }],
    }


def _app(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "api.sqlite3")
    monkeypatch.setattr(logger, "LOG_ROOT", tmp_path / "logs")
    monkeypatch.setattr(history, "ARCHIVE_ROOT", tmp_path / "logs" / "archives")
    monkeypatch.setattr(history, "ARCHIVE_CACHE_ROOT", tmp_path / "logs" / ".archive-cache")
    app = create_app()
    app.config.update(TESTING=True)
    return app


def _login(client):
    with client.session_transaction() as browser_session:
        browser_session["authenticated"] = True


def test_status_is_idle_without_missions(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    with app.test_client() as client:
        _login(client)
        response = client.get("/api/system_status")
    assert response.status_code == 200
    assert response.get_json()["mission_stats"]["status"] == "idle"


def test_legacy_csv_and_stop_get_are_removed(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    with app.test_client() as client:
        assert client.get("/api/csv?start_line=1").status_code == 404
        assert client.get("/stop_task").status_code == 405


def _reset_rate_limits():
    routes.login_limiter.reset()
    middleware.api_limiter.reset()


@pytest.fixture(autouse=True)
def reset_rate_limits():
    _reset_rate_limits()
    yield
    _reset_rate_limits()


def test_start_task_returns_202_before_planning(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    payload = {"init_loc": "25,121", "stops": [{"name": "A", "mode": "walking"}]}
    with app.test_client() as client:
        response = client.post(
            "/start_task",
            json=payload,
            headers={middleware.API_KEY_HEADER: config.API_ACCESS_KEY},
        )
    assert response.status_code == 202
    assert response.get_json()["initial_google_eta"] is None
    mission = db.latest_mission()
    assert mission["status"] == "planning"
    assert mission["plan"] == {}


def test_health_is_public(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    with app.test_client() as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}


def test_dashboard_exposes_configured_display_timezone(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    with app.test_client() as client:
        _login(client)
        response = client.get("/map")
    assert response.status_code == 200
    assert f'"timezone": "{config.TIMEZONE}"' in response.get_data(as_text=True)


def test_dashboard_exposes_configured_carto_basemap_key(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "CARTO_BASEMAP_API_KEY", "carto-test-key")
    with app.test_client() as client:
        _login(client)
        response = client.get("/map")
    assert response.status_code == 200
    assert '"carto_basemap_api_key": "carto-test-key"' in response.get_data(as_text=True)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"init_loc": "91,121", "stops": [{"name": "A", "mode": "walking"}]}, "out of range"),
        ({"init_loc": "25,121", "stops": [{"name": "A", "mode": "flying"}]}, "mode must be"),
        ({"init_loc": "25,121", "stops": [{"name": "A", "mode": "transit", "transit_type": "TRAIN"}]}, "transit_type"),
        ({"init_loc": "25,121", "stops": [{"name": "A", "mode": "walking", "wait_time": "99:99"}]}, "HH:MM"),
        ({"init_loc": "25,121", "stops": [{"name": str(i), "mode": "walking"} for i in range(51)]}, "cannot exceed 50"),
    ],
)
def test_start_task_rejects_unsafe_payloads(tmp_path, monkeypatch, payload, message):
    app = _app(tmp_path, monkeypatch)
    with app.test_client() as client:
        response = client.post(
            "/start_task", json=payload, headers={middleware.API_KEY_HEADER: config.API_ACCESS_KEY}
        )
    assert response.status_code == 400
    assert message in response.get_json()["error"]


def test_invalid_api_key_is_rate_limited_separately(tmp_path, monkeypatch):
    _reset_rate_limits()
    app = _app(tmp_path, monkeypatch)
    with app.test_client() as client:
        statuses = [
            client.get("/api/system_status", headers={middleware.API_KEY_HEADER: f"wrong-{i}"}).status_code
            for i in range(6)
        ]
    assert statuses == [401, 401, 401, 401, 401, 429]
    assert not routes.login_limiter


def test_dashboard_login_has_its_own_rate_limit(tmp_path, monkeypatch):
    _reset_rate_limits()
    app = _app(tmp_path, monkeypatch)
    with app.test_client() as client:
        statuses = [client.post("/login", data={"api_key": f"wrong-{i}"}).status_code for i in range(6)]
    assert statuses == [401, 401, 401, 401, 401, 429]
    assert not middleware.api_limiter


def test_successful_credentials_clear_their_own_failure_buckets(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    with app.test_client() as client:
        for index in range(5):
            assert client.post("/login", data={"api_key": f"wrong-{index}"}).status_code == 401
        assert client.post("/login", data={"api_key": config.API_SECRET_KEY}).status_code == 302

        for index in range(5):
            assert client.post(
                "/stop_task", headers={middleware.API_KEY_HEADER: f"wrong-{index}"}
            ).status_code == 401
        assert client.post(
            "/stop_task", headers={middleware.API_KEY_HEADER: config.API_ACCESS_KEY}
        ).status_code == 200


def test_route_token_changes_between_missions(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    first = db.create_mission({"init_loc": "25,121", "stops": [{"name": "A"}]}, _plan("A"))
    with app.test_client() as client:
        _login(client)
        first_page = client.get("/api/planned_route").get_json()
        db.update_mission(first, status="completed")
        db.create_mission({"init_loc": "25,121", "stops": [{"name": "B"}]}, _plan("B"))
        response = client.get(f"/api/planned_route?route_token={first_page['route_token']}")
    assert response.status_code == 200
    assert response.get_json()["route_token"] != first_page["route_token"]


def test_history_page_and_apis_are_fixed_to_selected_session(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    date_value, session_value = "2026-08-03", "12-00-00-1"
    session_dir = Path(logger.LOG_ROOT) / date_value / session_value
    session_dir.mkdir(parents=True)
    with (session_dir / "movement.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(logger.MOVEMENT_FIELDS)
        writer.writerow([1, "2026-08-03 04:00:00.000Z", 25, 121, "Walking", "", db.utc_now(), "", ""])
    for name in ("all", "route", "error", "security"):
        (session_dir / f"{name}.log").write_text(f"{session_value}:{name}", encoding="utf-8")
    mission_id = db.create_mission(
        {"init_loc": "25,121", "stops": [{"name": "Historical"}]}, _plan("Historical")
    )
    db.update_mission(mission_id, log_session=str(session_dir.resolve()), status="completed")
    db.create_mission({"init_loc": "24,120", "stops": [{"name": "Live"}]}, _plan("Live"))
    with app.test_client() as client:
        _login(client)
        page = client.get(f"/history/{date_value}/{session_value}")
        status = client.get(f"/api/history/{date_value}/{session_value}/status").get_json()
        route = client.get(f"/api/history/{date_value}/{session_value}/planned_route").get_json()
        log = client.get(f"/api/history/{date_value}/{session_value}/log/route")
    assert page.status_code == 200
    assert "HISTORY" in page.get_data(as_text=True)
    assert status["mission_id"] == mission_id
    assert route["route_token"].startswith(f"{mission_id}:")
    assert log.get_data(as_text=True) == f"{session_value}:route"


def test_history_navigation_reports_an_empty_plan_instead_of_crashing(tmp_path, monkeypatch):
    # A mission stored before its plan existed has no "routes" key, and the
    # sibling planned_route endpoint already answers 200 for the same input.
    app = _app(tmp_path, monkeypatch)
    date_value, session_value = "2026-08-03", "14-00-00-1"
    session_dir = Path(logger.LOG_ROOT) / date_value / session_value
    session_dir.mkdir(parents=True)
    with (session_dir / "movement.csv").open("w", encoding="utf-8", newline="") as handle:
        csv.writer(handle).writerow(logger.MOVEMENT_FIELDS)
    mission_id = db.create_mission({"init_loc": "25,121", "stops": [{"name": "A"}]})
    db.update_mission(mission_id, log_session=str(session_dir.resolve()), status="failed")

    with app.test_client() as client:
        _login(client)
        navigation = client.get(f"/api/history/{date_value}/{session_value}/navigation")
        route = client.get(f"/api/history/{date_value}/{session_value}/planned_route")
    assert navigation.status_code == 200
    assert navigation.get_json() == []
    assert route.status_code == 200
    assert route.get_json()["points"] == []


def test_planned_route_survives_the_mission_vanishing_mid_request(tmp_path, monkeypatch):
    # /api/planned_route reads the token and the mission separately, and the
    # dashboard polls it every 1.5s -- so it regularly lands in the window where
    # the worker marked the run interrupted between the two reads.
    app = _app(tmp_path, monkeypatch)
    db.create_mission({"init_loc": "25,121", "stops": [{"name": "A"}]}, _plan("A"))
    monkeypatch.setattr(db, "latest_live_mission", lambda **_kwargs: None)
    with app.test_client() as client:
        _login(client)
        response = client.get("/api/planned_route")
    assert response.status_code == 200
    assert response.get_json() == {"route_token": "", "points": []}


def test_missing_history_log_does_not_fall_back_to_live(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    date_value, session_value = "2026-08-03", "13-00-00-1"
    session_dir = Path(logger.LOG_ROOT) / date_value / session_value
    session_dir.mkdir(parents=True)
    with (session_dir / "movement.csv").open("w", encoding="utf-8", newline="") as handle:
        csv.writer(handle).writerow(logger.MOVEMENT_FIELDS)
    with app.test_client() as client:
        _login(client)
        response = client.get(f"/api/history/{date_value}/{session_value}/log/security")
    assert response.status_code == 404
    assert "no data" in response.get_data(as_text=True).lower()


def test_macrodroid_stop_action_uses_post():
    data = json.loads(Path("macrodroid-example.category").read_text(encoding="utf-8"))
    requests = []

    def collect(value):
        if isinstance(value, dict):
            request_config = value.get("requestConfig")
            if isinstance(request_config, dict):
                requests.append(request_config)
            for child in value.values():
                collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)

    collect(data)
    start = next(item for item in requests if item.get("urlToOpen", "").endswith("/start_task"))
    stop = next(item for item in requests if item.get("urlToOpen", "").endswith("/stop_task"))
    assert start["requestTimeOutSeconds"] == 30
    assert stop["requestType"] == 1


def test_non_ascii_credentials_are_rejected_not_crashed(tmp_path, monkeypatch):
    # hmac.compare_digest() raises TypeError on non-ASCII str, which turned a
    # mistyped password into an unauthenticated 500 on both entry points.
    app = _app(tmp_path, monkeypatch)
    with app.test_client() as client:
        login = client.post("/login", data={"api_key": "密碼1234"})
        api = client.get(
            "/api/system_status",
            headers={middleware.API_KEY_HEADER: "密碼1234".encode().decode("latin-1")},
        )
    assert login.status_code == 401
    assert api.status_code == 401


def test_non_ascii_attempts_still_throttle_and_are_logged(tmp_path, monkeypatch):
    # The TypeError used to escape before record_failure() and log_security(),
    # so these attempts bypassed both throttles and left no security trail.
    app = _app(tmp_path, monkeypatch)
    logged = []
    monkeypatch.setattr(logger, "log_security", lambda message, level="info": logged.append(message))
    with app.test_client() as client:
        statuses = [client.post("/login", data={"api_key": f"密碼{index}"}).status_code
                    for index in range(6)]
    assert statuses == [401, 401, 401, 401, 401, 429]
    assert sum("login failed" in message for message in logged) == 6


def test_non_ascii_secret_in_the_environment_still_authenticates(tmp_path, monkeypatch):
    # A non-ASCII value in .env must work, not merely fail without crashing.
    monkeypatch.setattr(config, "API_SECRET_KEY", "通關密語")
    app = _app(tmp_path, monkeypatch)
    with app.test_client() as client:
        assert client.post("/login", data={"api_key": "通關密語"}).status_code == 302
        assert client.post("/login", data={"api_key": "通關密碼"}).status_code == 401


def test_expired_dashboard_session_always_gets_401_never_429(tmp_path, monkeypatch):
    # The dashboard runs three concurrent pollers and only redirects to /login
    # on 401, so a throttled 429 would strand the user on an unauthenticated page.
    app = _app(tmp_path, monkeypatch)
    with app.test_client() as client:
        statuses = [client.get("/api/system_status").status_code for _ in range(10)]
    assert statuses == [401] * 10
    assert not middleware.api_limiter


def test_session_login_clears_earlier_api_failures(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    with app.test_client() as client:
        for index in range(5):
            assert client.get(
                "/api/system_status", headers={middleware.API_KEY_HEADER: f"wrong-{index}"}
            ).status_code == 401
        assert middleware.api_limiter
        _login(client)
        assert client.get("/api/system_status").status_code == 200
        assert not middleware.api_limiter


def test_matching_route_token_returns_304_without_body(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    db.create_mission({"init_loc": "25,121", "stops": [{"name": "A"}]}, _plan("A"))
    with app.test_client() as client:
        _login(client)
        token = client.get("/api/planned_route").get_json()["route_token"]
        response = client.get(f"/api/planned_route?route_token={token}")
    assert response.status_code == 304
    assert response.get_data() == b""


def test_route_token_matches_the_full_payload_token(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    db.create_mission({"init_loc": "25,121", "stops": [{"name": "A"}]}, _plan("A"))
    with app.test_client() as client:
        _login(client)
        payload = client.get("/api/planned_route").get_json()
    assert db.latest_route_token() == payload["route_token"]


def test_vendor_assets_are_cacheable_while_pages_are_not(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    with app.test_client() as client:
        vendor = client.get("/static/vendor/leaflet/leaflet.js")
        own = client.get("/static/js/api.js")
        page = client.get("/login")
    assert "immutable" in vendor.headers["Cache-Control"]
    assert own.headers["Cache-Control"] == "no-cache"
    assert page.headers["Cache-Control"] == "no-store"


def test_history_listing_accepts_offset(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    captured = {}
    monkeypatch.setattr(
        history, "list_history_sessions",
        lambda limit=100, offset=0: captured.update(limit=limit, offset=offset) or [],
    )
    with app.test_client() as client:
        _login(client)
        assert client.get("/api/history?limit=20&offset=40").status_code == 200
    assert captured == {"limit": 20, "offset": 40}


def _interrupted_mission(tmp_path) -> int:
    mission_id = db.create_mission({"init_loc": "25,121", "stops": [{"name": "A"}]}, _plan("A"))
    db.claim_next("worker")
    # Exactly what run_worker() does when it reclaims an orphan on startup.
    db.interrupt_active_missions("Worker restarted; the previous run did not finish")
    return mission_id


def test_restart_interrupted_mission_is_not_shown_on_the_dashboard(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    _interrupted_mission(tmp_path)
    with app.test_client() as client:
        _login(client)
        status = client.get("/api/system_status").get_json()
        route = client.get("/api/planned_route").get_json()
        navigation = client.get("/api/navigation_history").get_json()
        movements = client.get("/api/movements/current").get_json()
    assert status["mission_stats"]["status"] == "idle"
    assert status["mission_active"] is False
    assert "mission_id" not in status
    assert route == {"route_token": "", "points": []}
    assert navigation == []
    assert movements["records"] == [] and movements["stream_id"] is None


def test_interrupted_mission_does_not_reappear_through_the_route_token(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    _interrupted_mission(tmp_path)
    assert db.latest_route_token() is None
    with app.test_client() as client:
        _login(client)
        # An empty token must not match the "no mission" reply and 304 the map
        # into keeping the interrupted route on screen.
        assert client.get("/api/planned_route?route_token=").status_code == 200


def test_finished_missions_still_show_their_outcome(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    for outcome in ("completed", "stopped", "aborted", "failed"):
        mission_id = db.create_mission(
            {"init_loc": "25,121", "stops": [{"name": outcome}]}, _plan(outcome)
        )
        db.update_mission(mission_id, status=outcome, finished_at=db.utc_now())
        with app.test_client() as client:
            _login(client)
            status = client.get("/api/system_status").get_json()
        assert status["mission_stats"]["status"] == outcome, outcome
        assert status["mission_id"] == mission_id


def test_mission_form_still_prefills_after_an_interrupted_run(tmp_path, monkeypatch):
    # The form is for resubmitting, so it keeps the last stops even though the
    # dashboard itself reports idle.
    app = _app(tmp_path, monkeypatch)
    _interrupted_mission(tmp_path)
    with app.test_client() as client:
        _login(client)
        payload = client.get("/api/mission").get_json()
    assert [stop["name"] for stop in payload["stops"]] == ["A"]
