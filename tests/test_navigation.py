import threading
from datetime import datetime, timedelta, timezone

from mock_gps import config, db, logger
from mock_gps.core import navigation, planner


def test_mrt_speed_is_capped_at_135_percent():
    assert navigation._movement_speed(1000, 1, 10, True) == 13.5


def test_non_mrt_is_not_accelerated_to_recover_debt():
    assert navigation._movement_speed(1000, 1, 10, False) == 10


def test_station_crossing_detects_segment_that_skips_radius(monkeypatch):
    monkeypatch.setattr(config, "MRT_STATIONS_DB", {"Test Station": [25.0, 121.0]})
    station = navigation._crossed_station((25.0, 120.999), (25.0, 121.001), set())
    assert station == "Test Station"


def test_visited_station_does_not_trigger_twice(monkeypatch):
    monkeypatch.setattr(config, "MRT_STATIONS_DB", {"Test Station": [25.0, 121.0]})
    assert navigation._crossed_station((25.0, 120.999), (25.0, 121.001), {"Test Station"}) is None


def test_route_delay_replan_threshold_is_strictly_over_60_seconds():
    planned = datetime(2026, 8, 3, 1, 0, tzinfo=timezone.utc)
    route = {"arrival_at": planned.isoformat()}
    assert navigation._route_delay_seconds(route, planned + timedelta(seconds=60)) == 60
    assert navigation._route_delay_seconds(route, planned + timedelta(seconds=61)) == 61


def test_projected_arrival_uses_current_plan_remaining_time():
    start = datetime(2026, 8, 3, 1, 0, tzinfo=timezone.utc)
    routes = [
        {"arrival_at": start.isoformat()},
        {"arrival_at": (start + timedelta(minutes=20)).isoformat()},
    ]
    actual = start + timedelta(minutes=2)
    assert navigation._projected_arrival(routes, 1, actual) == start + timedelta(minutes=22)


def test_worker_shutdown_stops_mission_context_without_db_poll():
    shutdown = threading.Event()
    shutdown.set()
    context = navigation.MissionContext(1, shutdown)
    assert context.should_continue() is False


def _plan():
    eta = db.utc_now()
    return {
        "initial_google_eta": eta,
        "routes": [{
            "origin": "25,121",
            "destination": "A",
            "arrival_at": eta,
            "departure_at": eta,
            "steps": [{"duration_seconds": 1, "points": [[25.0, 121.0], [25.1, 121.1]]}],
        }],
    }


def test_worker_plans_before_executing_mission(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "planning.sqlite3")
    db.init_db()
    mission_id = db.create_mission({
        "init_loc": "25,121", "stops": [{"name": "A", "mode": "walking"}],
    })
    mission = db.claim_next("worker")
    executed = []
    monkeypatch.setattr(navigation, "plan_mission", lambda *_args, **_kwargs: _plan())
    monkeypatch.setattr(navigation, "execute_mission", lambda planned, _shutdown: executed.append(planned))
    navigation._plan_and_execute_mission(mission, threading.Event())
    stored = db.get_mission(mission_id)
    assert stored["status"] == "running"
    assert stored["route_revision"] == 1
    assert executed[0]["plan"]["routes"]


def test_planning_failure_is_saved_on_mission(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "planning-failure.sqlite3")
    db.init_db()
    mission_id = db.create_mission({
        "init_loc": "25,121", "stops": [{"name": "A", "mode": "walking"}],
    })
    mission = db.claim_next("worker")
    def fail_planning(*_args, **_kwargs):
        raise RuntimeError("maps down")

    monkeypatch.setattr(navigation, "plan_mission", fail_planning)
    navigation._plan_and_execute_mission(mission, threading.Event())
    stored = db.get_mission(mission_id)
    assert stored["status"] == "failed"
    assert stored["last_error"] == "Planning failed: maps down"


def test_mission_initialization_failure_is_saved(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "execution-failure.sqlite3")
    db.init_db()
    mission_id = db.create_mission(
        {"init_loc": "25,121", "stops": [{"name": "A"}]}, _plan()
    )
    mission = db.claim_next("worker")
    monkeypatch.setattr(
        logger, "init_task_logs", lambda _mission_id, **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )
    navigation.execute_mission(mission, threading.Event())
    stored = db.get_mission(mission_id)
    assert stored["status"] == "failed"
    assert stored["last_error"] == "disk full"
    assert mission_id not in navigation._phone_health


def test_position_is_persisted_at_most_every_five_seconds(monkeypatch):
    context = navigation.MissionContext(1, threading.Event())
    writes = []
    times = iter([100.0, 102.0, 106.0])
    monkeypatch.setattr(navigation.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(navigation.time, "time", lambda: 1_000.0)
    monkeypatch.setattr(navigation.logger, "log_movement", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(navigation.db, "update_mission", lambda *_args, **kwargs: writes.append(kwargs))
    monkeypatch.setattr(navigation, "_queue_phone", lambda *_args: None)
    navigation.send_position(context, 25.0, 121.0, "one")
    navigation.send_position(context, 25.1, 121.1, "two")
    navigation.send_position(context, 25.2, 121.2, "three")
    assert writes == [
        {"last_lat": 25.0, "last_lng": 121.0},
        {"last_lat": 25.2, "last_lng": 121.2},
    ]


def test_sigterm_requests_graceful_worker_shutdown(monkeypatch):
    shutdown = threading.Event()
    installed = {}
    monkeypatch.setattr(navigation.signal, "getsignal", lambda _signum: "previous")
    monkeypatch.setattr(
        navigation.signal,
        "signal",
        lambda signum, handler: installed.__setitem__(signum, handler),
    )
    previous = navigation._install_shutdown_handlers(shutdown)
    installed[navigation.signal.SIGTERM]()
    assert shutdown.is_set()
    assert previous[navigation.signal.SIGTERM] == "previous"


def test_replan_keeps_the_current_stop_scheduled_departure():
    # plan_mission() applies stops[i].wait_time when leaving stop i, so a
    # replan that starts from "now" would silently drop it.
    arrival = datetime(2026, 8, 4, 6, 55, tzinfo=timezone.utc)
    stop = {"name": "Home", "wait_time": "15:30", "skip_if_late": False}
    resume_at = planner.scheduled_departure(arrival.astimezone(config.TIMEZONE), stop)
    assert resume_at.hour == 15
    assert resume_at.minute == 30
    assert resume_at.date() == arrival.astimezone(config.TIMEZONE).date()


def test_replan_departs_immediately_for_a_late_skippable_stop():
    arrival = datetime(2026, 8, 4, 10, 0, tzinfo=timezone.utc)
    local_arrival = arrival.astimezone(config.TIMEZONE)
    stop = {"name": "Home", "wait_time": "01:00", "skip_if_late": True}
    assert planner.scheduled_departure(local_arrival, stop) == local_arrival


def test_replan_uses_scheduled_departure_as_plan_start_time(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "replan.sqlite3")
    db.init_db()
    stops = [
        {"name": "A", "mode": "walking", "wait_time": "23:45", "skip_if_late": False},
        {"name": "B", "mode": "walking", "wait_time": "", "skip_if_late": False},
    ]
    plan = _plan()
    plan["routes"][0]["arrival_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=300)
    ).isoformat()
    plan["routes"].append(dict(plan["routes"][0], destination="B"))
    mission_id = db.create_mission({"init_loc": "25,121", "stops": stops}, plan)
    db.claim_next("worker")
    db.update_mission(mission_id, last_lat=25.0, last_lng=121.0)

    captured = {}

    def fake_plan(origin, remaining, *, start_time=None, **_kwargs):
        captured["origin"] = origin
        captured["remaining"] = remaining
        captured["start_time"] = start_time
        # End the mission here; otherwise execute_mission() would enter the
        # final-position hold loop and never return.
        db.update_mission(mission_id, cancel_requested=1)
        return _plan()

    monkeypatch.setattr(navigation, "plan_mission", fake_plan)
    monkeypatch.setattr(navigation, "move_step", lambda *_a, **_k: True)
    monkeypatch.setattr(navigation, "_wait_until", lambda *_a, **_k: True)
    monkeypatch.setattr(navigation, "send_position", lambda *_a, **_k: None)
    monkeypatch.setattr(navigation.logger, "init_task_logs", lambda *_a, **_k: {"session_dir": None})
    monkeypatch.setattr(navigation.logger, "write_mission_snapshot", lambda *_a: None)

    shutdown = threading.Event()
    mission = db.get_mission(mission_id)
    navigation.execute_mission(mission, shutdown)

    assert captured["start_time"] is not None
    assert captured["start_time"].hour == 23
    assert captured["start_time"].minute == 45
    assert [stop["name"] for stop in captured["remaining"]] == ["B"]
