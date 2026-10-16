import queue
import threading
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

from mock_gps import config, create_app, db, history, logger
from mock_gps.api import middleware, routes
from mock_gps.api.ratelimit import RateLimiter
from mock_gps.core import navigation, planner


def make_mission(stops=None):
    db.init_db()
    now = db.utc_now()
    stops = stops or [{"name": "A", "mode": "walking"}]
    plan = {"initial_google_eta": now, "routes": [{
        "destination": stop["name"], "departure_at": now, "arrival_at": now,
        "steps": [{"duration_seconds": 1, "points": [[25, 121], [25.001, 121.001]]}],
    } for stop in stops]}
    mid = db.create_mission({"init_loc": "25,121", "stops": stops}, plan)
    return db.claim_next("test-worker"), mid


def test_completed_hold_uses_endpoint_even_between_database_writes(monkeypatch):
    mission, mid = make_mission()
    delivered = []
    monkeypatch.setattr(navigation.time, "monotonic", lambda: 100)
    monkeypatch.setattr(navigation, "_queue_phone", lambda _id, lat, lng: delivered.append((lat, lng)))
    monkeypatch.setattr(navigation, "_wait_until", lambda *_a: True)

    def move(ctx, *_args):
        navigation.send_position(ctx, 25.001, 121.001, "Walking")
        return True

    monkeypatch.setattr(navigation, "move_step", move)
    monkeypatch.setattr(navigation.MissionContext, "should_continue", lambda _self: len(delivered) < 3)
    navigation.execute_mission(mission, threading.Event())
    assert delivered == [(25, 121), (25.001, 121.001), (25.001, 121.001)]
    saved = db.get_mission(mid)
    assert saved["status"] == "completed"
    assert saved["last_lat"] == 25.001
    assert not saved["is_holding_final_position"]
    snapshot = history.snapshot_mission(*saved["log_session"].split("/"))
    assert snapshot["status"] == "completed"
    assert logger.current_session_dir is None


def test_final_stop_wait_occurs_before_completion(monkeypatch):
    mission, mid = make_mission([{"name": "A", "wait_time": "00:00", "skip_if_late": False}])
    waits = []
    monkeypatch.setattr(navigation, "_queue_phone", lambda *_a: None)
    monkeypatch.setattr(navigation, "move_step", lambda *_a: True)

    def wait(ctx, target, action):
        waits.append((target, action, db.get_mission(mid)["status"]))
        if action == "Stop Wait":
            ctx.cancel_event.set()
            return False
        return True

    monkeypatch.setattr(navigation, "_wait_until", wait)
    navigation.execute_mission(mission, threading.Event())
    target, action, status = waits[-1]
    assert action == "Stop Wait" and status == "running"
    assert datetime.fromisoformat(target) > config.local_now()
    assert db.get_mission(mid)["status"] == "aborted"


def test_transit_wait_precedes_movement(monkeypatch):
    mission, _ = make_mission()
    departure = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    mission["plan"]["routes"][0]["steps"][0]["departure_time"] = departure
    actions = []
    monkeypatch.setattr(navigation, "_queue_phone", lambda *_a: None)
    monkeypatch.setattr(navigation, "_wait_until", lambda _c, _t, action: actions.append(action) or True)

    def move(ctx, *_a):
        actions.append("move")
        ctx.cancel_event.set()
        return False

    monkeypatch.setattr(navigation, "move_step", move)
    navigation.execute_mission(mission, threading.Event())
    assert actions.index("Transit Wait") < actions.index("move")


def test_restart_preserves_completed_outcome_and_ends_hold():
    _, mid = make_mission()
    db.update_mission(mid, status="completed", finished_at=db.utc_now(), is_holding_final_position=1)
    db.interrupt_active_missions("restart")
    saved = db.get_mission(mid)
    assert saved["status"] == "completed"
    assert not saved["is_holding_final_position"]


def test_lightweight_read_does_not_select_plan_json():
    _, mid = make_mission()
    sql = []
    with db.connect() as connection:
        connection.set_trace_callback(sql.append)
        try:
            assert "plan" not in db.get_mission(mid, include_plan=False)
            db.latest_mission(include_plan=False, include_payload=False)
        finally:
            connection.set_trace_callback(None)
    assert all("plan_json" not in query for query in sql if query.startswith("SELECT"))


def test_legacy_absolute_session_migrates_and_survives_move():
    _, mid = make_mission()
    key = db.get_mission(mid)["log_session"]
    with db.connect() as connection:
        connection.execute("UPDATE missions SET log_session=? WHERE id=?", ("C:\\old\\logs\\" + key, mid))
    db.init_db()
    assert db.mission_by_log_session("D:/new/logs/" + key)["id"] == mid


def test_rate_limit_blocks_correct_guesses_until_window_expires(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr("mock_gps.api.ratelimit.time.monotonic", lambda: clock[0])
    app = create_app()
    monkeypatch.setattr(routes, "login_limiter", RateLimiter())
    monkeypatch.setattr(middleware, "api_limiter", RateLimiter())
    with app.test_client() as client:
        for _ in range(5):
            assert client.post("/login", data={"api_key": "wrong"}).status_code == 401
            assert client.post("/stop_task", headers={"API-ACCESS-KEY": "wrong"}).status_code == 401
        login = client.post("/login", data={"api_key": config.API_SECRET_KEY})
        api = client.post("/stop_task", headers={"API-ACCESS-KEY": config.API_ACCESS_KEY})
        assert login.status_code == api.status_code == 429
        assert int(login.headers["Retry-After"]) == 300
        clock[0] = 300
        assert client.post("/login", data={"api_key": config.API_SECRET_KEY}).status_code == 302
        assert client.post("/stop_task", headers={"API-ACCESS-KEY": config.API_ACCESS_KEY}).status_code == 200


@pytest.mark.parametrize("field,value", [("name", None), ("name", {}), ("skip_if_late", "false")])
def test_invalid_field_types_are_rejected(field, value):
    with pytest.raises(ValueError):
        routes._validate_stop({"name": "A", "mode": "walking", field: value}, 0)


def test_removed_mode_is_rejected_before_routing(monkeypatch):
    maps = Mock()
    monkeypatch.setattr(config, "gmaps_client", maps)
    with pytest.raises(ValueError, match="mode must be"):
        routes._validate_stop({"name": "A", "mode": "motorcycle"}, 0)
    with pytest.raises(ValueError, match="mode must be"):
        planner.plan_mission("25,121", [{"name": "A", "mode": "motorcycle"}])
    maps.directions.assert_not_called()


def test_planning_stops_before_next_api_request(monkeypatch):
    cancelled = [False]

    def request(*_a, **_k):
        cancelled[0] = True
        return []

    maps = Mock(directions=Mock(side_effect=request))
    monkeypatch.setattr(config, "gmaps_client", maps)
    with pytest.raises(planner.PlanningCancelled):
        planner.plan_mission("25,121", [{"name": "A"}, {"name": "B"}],
                             should_continue=lambda: not cancelled[0])
    assert maps.directions.call_count == 1


def test_planning_timeout_prevents_first_request(monkeypatch):
    maps = Mock()
    monkeypatch.setattr(config, "gmaps_client", maps)
    with pytest.raises(RuntimeError, match="time budget"):
        planner.plan_mission("25,121", [{"name": "A"}], max_seconds=0)
    maps.directions.assert_not_called()


def test_planning_failure_has_history_and_snapshot(monkeypatch):
    app = create_app()
    with app.test_client() as client:
        response = client.post("/start_task", json={"init_loc": "25,121", "stops": [
            {"name": "A", "mode": "walking"},
        ]}, headers={"API-ACCESS-KEY": config.API_ACCESS_KEY})
        mid = response.json["mission_id"]
        monkeypatch.setattr(navigation, "plan_mission", Mock(side_effect=RuntimeError("no route")))
        navigation._plan_and_execute_mission(db.claim_next("test"), threading.Event())
        listing = client.get("/api/history", headers={"API-ACCESS-KEY": config.API_ACCESS_KEY}).json
        assert listing[0]["status"] == "failed"
        snapshot = history.snapshot_mission(*db.get_mission(mid)["log_session"].split("/"))
        assert snapshot["status"] == "failed"
        assert "no route" in snapshot["last_error"]


def test_snapshot_can_supply_history_without_database_row():
    app = create_app()
    _, mid = make_mission()
    history.persist_mission(mid)
    key = db.get_mission(mid)["log_session"]
    with db.connect() as connection:
        connection.execute("DELETE FROM missions WHERE id=?", (mid,))
    with app.test_client() as client:
        response = client.get(f"/api/history/{key}/status", headers={"API-ACCESS-KEY": config.API_ACCESS_KEY})
        assert response.status_code == 200
        assert response.json["mission_id"] == mid


def test_phone_sender_survives_database_failure(monkeypatch):
    items = queue.Queue()
    items.put((1, 25, 121))
    items.put((2, 25, 121))
    items.put(None)
    recorded = []
    monkeypatch.setattr(navigation, "_phone_queue", items)
    monkeypatch.setattr(navigation, "PHONE_URL", "")
    monkeypatch.setattr(db, "phone_delivery_allowed", Mock(side_effect=[RuntimeError("locked"), True]))
    monkeypatch.setattr(navigation, "_record_phone_health", lambda mid, _s: recorded.append(mid))
    navigation._phone_loop()
    assert recorded == [2]


def test_history_api_pages_do_not_lose_same_second_sessions():
    app = create_app()
    for index in range(105):
        db.create_mission({"init_loc": "25,121", "stops": [{"name": str(index)}]})
    with app.test_client() as client:
        headers = {"API-ACCESS-KEY": config.API_ACCESS_KEY}
        first = client.get("/api/history?limit=100", headers=headers).json
        second = client.get("/api/history?limit=100&offset=100", headers=headers).json
    assert len(first) == 100 and len(second) == 5
    assert len({entry["id"] for entry in first + second}) == 105


def test_log_tail_is_bounded(tmp_path):
    path = tmp_path / "all.log"
    path.write_text("old line\n" * 100_000 + "newest\n", encoding="utf-8")
    tail = history.read_log_tail(path)
    assert len(tail.encode()) < 257 * 1024
    assert tail.splitlines()[-1] == "newest"


def test_partial_csv_line_is_not_consumed(tmp_path):
    path = tmp_path / "movement.csv"
    path.write_bytes(b"header\n1,partial")
    result = history.read_movement_records(path)
    assert result["next_offset"] == len(b"header\n")
    assert not result["has_more"]


def test_lease_database_error_requests_shutdown(monkeypatch):
    shutdown = Mock()
    shutdown.wait.return_value = False
    monkeypatch.setattr(db, "renew_worker", Mock(side_effect=RuntimeError("database locked")))
    navigation._renew_lease("worker", shutdown)
    shutdown.set.assert_called_once()


def test_shutdown_finalizes_unclaimed_task_after_readiness(monkeypatch):
    captured = {}

    def install(shutdown):
        captured["shutdown"] = shutdown
        return {}

    def ready():
        captured["id"] = db.create_mission({"init_loc": "25,121", "stops": [{"name": "A"}]})
        captured["shutdown"].set()

    monkeypatch.setattr(navigation, "_install_shutdown_handlers", install)
    monkeypatch.setattr(navigation, "start_phone_worker", lambda: None)
    monkeypatch.setattr(navigation.threading, "Thread", Mock())
    navigation.run_worker(on_ready=ready)
    saved = db.get_mission(captured["id"])
    assert saved["status"] == "interrupted"
    assert not navigation.worker_health()["worker"]
    assert history.snapshot_mission(*saved["log_session"].split("/"))["status"] == "interrupted"


def test_transit_plan_preserves_schedule_and_waiting_duration(monkeypatch):
    start = datetime(2026, 10, 16, 9, tzinfo=timezone.utc)
    board = start + timedelta(minutes=5)
    arrival = board + timedelta(minutes=10)
    maps = Mock(directions=Mock(return_value=[{"legs": [{
        "duration": {"value": 600}, "arrival_time": {"value": arrival.timestamp()},
        "steps": [{"travel_mode": "TRANSIT", "duration": {"value": 600},
                   "polyline": {"points": "_p~iF~ps|U_ulLnnqC"},
                   "transit_details": {"departure_time": {"value": board.timestamp()},
                                       "arrival_time": {"value": arrival.timestamp()},
                                       "departure_stop": {"name": "Start Station"},
                                       "arrival_stop": {"name": "End Station"}, "num_stops": 3}}],
    }]}]))
    monkeypatch.setattr(config, "gmaps_client", maps)
    plan = planner.plan_mission("25,121", [{"name": "A", "mode": "transit"}], start_time=start)
    route = plan["routes"][0]
    assert route["duration_seconds"] == 900
    assert datetime.fromisoformat(route["arrival_at"]) == arrival
    assert datetime.fromisoformat(route["steps"][0]["departure_time"]) == board
    assert route["steps"][0]["departure_stop"] == "Start Station"


def test_phone_thread_restart_checks_liveness(monkeypatch):
    monkeypatch.setattr(navigation, "_phone_thread", Mock(is_alive=lambda: False))
    thread = Mock()
    monkeypatch.setattr(navigation.threading, "Thread", Mock(return_value=thread))
    navigation.start_phone_worker()
    thread.start.assert_called_once()


def test_health_reports_current_and_stale_worker_heartbeats(monkeypatch):
    monkeypatch.setattr(navigation, "_worker_started", True)
    monkeypatch.setattr(navigation, "_phone_thread", Mock(is_alive=lambda: True))
    monkeypatch.setattr(navigation, "_worker_heartbeat", 100)
    monkeypatch.setattr(navigation, "_phone_heartbeat", 100)
    monkeypatch.setattr(navigation.time, "monotonic", lambda: 110)
    assert navigation.worker_health()["status"] == "ok"
    monkeypatch.setattr(navigation.time, "monotonic", lambda: 140)
    assert navigation.worker_health()["status"] == "degraded"


def test_starting_after_an_archived_mission_does_not_recreate_empty_recording(monkeypatch):
    app = create_app()
    _, mid = make_mission()
    old = datetime.now(timezone.utc) - timedelta(days=45)
    key = f"{old:%Y-%m-%d}/12-00-00-{mid}"
    db.update_mission(mid, status="completed", worker_id=None, log_session=key)
    history.persist_mission(mid)
    source = logger.LOG_ROOT / key / "movement.csv"
    with source.open("a", encoding="utf-8") as handle:
        handle.write("saved-coordinate\n")
    assert history.archive_expired_sessions()
    with app.test_client() as client:
        result = client.post("/start_task", headers={"API-ACCESS-KEY": config.API_ACCESS_KEY},
                             json={"init_loc": "25,121", "stops": [{"name": "B", "mode": "walking"}]})
        assert result.status_code == 202
    assert not source.exists()
    assert "saved-coordinate" in history.resolve_history_movement(*key.split("/")).read_text(encoding="utf-8")


def test_ended_missions_cannot_receive_phone_delivery():
    _, mid = make_mission()
    assert db.phone_delivery_allowed(mid)
    db.update_mission(mid, status="failed")
    assert not db.phone_delivery_allowed(mid)
    db.update_mission(mid, status="completed", is_holding_final_position=1)
    assert db.phone_delivery_allowed(mid)
    db.update_mission(mid, cancel_requested=1)
    assert not db.phone_delivery_allowed(mid)
