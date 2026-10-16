import math
import queue
import random
import signal
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from mock_gps import config, db, history, logger
from mock_gps.core.planner import PlanningCancelled, plan_mission, scheduled_departure

PHONE_URL = f"http://{config.PHONE_TAILSCALE_IP}:8080/gps" if config.PHONE_TAILSCALE_IP else ""
_phone_queue: queue.Queue[tuple[int, float, float] | None] = queue.Queue(maxsize=1)
_phone_thread: threading.Thread | None = None
_worker_started = False
_worker_heartbeat = 0.0
_phone_heartbeat = 0.0
_phone_worker_lock = threading.Lock()
_phone_health: dict[int, tuple[str, float]] = {}


def get_distance_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    value = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return radius * 2 * math.atan2(math.sqrt(value), math.sqrt(1 - value))


def _record_phone_health(mission_id: int, status: str, *, error: str | None = None) -> None:
    now = time.monotonic()
    previous_status, previous_write = _phone_health.get(mission_id, ("", 0.0))
    if status == previous_status and now - previous_write < 10.0:
        return
    values: dict[str, Any] = {"phone_status": status}
    if status == "ok":
        values["phone_last_success"] = db.utc_now()
    elif status == "unreachable":
        values["phone_last_failure"] = db.utc_now()
        if error:
            logger.log_sys(f"Phone update failed: {error}", "warning")
    db.update_mission(mission_id, **values)
    _phone_health[mission_id] = (status, now)


def _phone_loop() -> None:
    global _phone_heartbeat
    with requests.Session() as session:
        while True:
            _phone_heartbeat = time.monotonic()
            try:
                item = _phone_queue.get(timeout=1)
            except queue.Empty:
                continue
            if item is None:
                return
            mission_id, lat, lng = item
            try:
                if not db.phone_delivery_allowed(mission_id):
                    continue
                if not PHONE_URL:
                    _record_phone_health(mission_id, "standby")
                    continue
                try:
                    response = session.get(PHONE_URL, params={"lat": lat, "lng": lng}, timeout=0.8)
                    response.raise_for_status()
                    status, error = "ok", None
                except requests.RequestException as exc:
                    status, error = "unreachable", str(exc)
                if db.phone_delivery_allowed(mission_id):
                    _record_phone_health(mission_id, status, error=error)
            except Exception as exc:
                logger.log_sys(f"Phone sender deferred an update: {exc}", "warning")


def start_phone_worker() -> None:
    global _phone_thread
    with _phone_worker_lock:
        if _phone_thread and _phone_thread.is_alive():
            return
        _phone_thread = threading.Thread(target=_phone_loop, name="phone-sender", daemon=True)
        _phone_thread.start()


def worker_health() -> dict:
    now = time.monotonic()
    worker_ok = _worker_started and now - _worker_heartbeat < 35
    phone_ok = bool(_phone_thread and _phone_thread.is_alive() and now - _phone_heartbeat < 35)
    return {"status": "ok" if worker_ok and phone_ok else "degraded",
            "worker": worker_ok, "phone_sender": phone_ok}


def _queue_phone(mission_id: int, lat: float, lng: float) -> None:
    start_phone_worker()
    item = (mission_id, lat, lng)
    try:
        _phone_queue.put_nowait(item)
    except queue.Full:
        try:
            _phone_queue.get_nowait()
        except queue.Empty:
            pass
        try:
            _phone_queue.put_nowait(item)
        except queue.Full:
            pass


@dataclass
class MissionContext:
    mission_id: int
    worker_shutdown: threading.Event
    cancel_event: threading.Event = field(default_factory=threading.Event)
    last_cancel_poll: float = 0.0
    last_recorded_at: float | None = None
    last_recorded_coord: tuple[float, float] | None = None
    last_position: tuple[float, float] | None = None
    last_position_write: float = 0.0

    def should_continue(self) -> bool:
        if self.worker_shutdown.is_set():
            self.cancel_event.set()
            return False
        now = time.monotonic()
        if now - self.last_cancel_poll >= 0.25:
            self.last_cancel_poll = now
            if db.is_cancel_requested(self.mission_id):
                self.cancel_event.set()
        return not self.cancel_event.is_set()

    def wait(
        self,
        seconds: float,
        action: str,
        *,
        note: str = "",
        record_boundaries: bool = True,
    ) -> bool:
        deadline = time.monotonic() + max(0.0, seconds)
        next_phone = time.monotonic()
        next_heartbeat = next_phone + 60.0
        if self.last_position and record_boundaries:
            send_position(self, *self.last_position, f"{action} Start", note=note, record=True)
        while self.should_continue() and time.monotonic() < deadline:
            now = time.monotonic()
            if self.last_position and now >= next_phone:
                heartbeat = now >= next_heartbeat
                send_position(
                    self,
                    *self.last_position,
                    f"{action} Heartbeat" if heartbeat else action,
                    note=note,
                    record=heartbeat,
                )
                next_phone = now + 1.0
                if heartbeat:
                    next_heartbeat = now + 60.0
            self.cancel_event.wait(max(0.005, min(0.1, deadline - time.monotonic())))
        if self.should_continue() and self.last_position and record_boundaries:
            send_position(self, *self.last_position, f"{action} End", note=note, record=True)
        return self.should_continue()


def send_position(
    ctx: MissionContext,
    lat: float,
    lng: float,
    action: str,
    *,
    record: bool = True,
    note: str = "",
) -> None:
    now_wall = time.time()
    distance = None
    if ctx.last_recorded_coord:
        distance = get_distance_meters(*ctx.last_recorded_coord, lat, lng)
    if record:
        delta = None if ctx.last_recorded_at is None else now_wall - ctx.last_recorded_at
        logger.log_movement(
            lat, lng, action, note, sent_at=now_wall,
            delta_seconds=delta, distance_meters=distance,
        )
        ctx.last_recorded_at = now_wall
        ctx.last_recorded_coord = (lat, lng)
    ctx.last_position = (lat, lng)
    now = time.monotonic()
    if now - ctx.last_position_write >= 5.0:
        db.update_mission(ctx.mission_id, last_lat=lat, last_lng=lng)
        ctx.last_position_write = now
    _queue_phone(ctx.mission_id, lat, lng)


def persist_position(ctx: MissionContext) -> None:
    if ctx.last_position:
        db.update_mission(ctx.mission_id, last_lat=ctx.last_position[0], last_lng=ctx.last_position[1])


def _open_mission_logs(mission: dict) -> None:
    session_id = mission.get("log_session")
    expected = (logger.LOG_ROOT / session_id).resolve() if session_id else None
    if expected and logger.current_session_dir and expected == logger.current_session_dir.resolve():
        return
    status = logger.init_task_logs(mission["id"], session_id=session_id)
    db.update_mission(mission["id"], log_session=status["session_dir"])
    history.persist_mission(mission["id"])


def _route_segments(points: list[list[float]]) -> tuple[list[float], float]:
    distances = [get_distance_meters(*points[i], *points[i + 1]) for i in range(len(points) - 1)]
    return distances, sum(distances)


def _point_at_distance(
    points: list[list[float]], segments: list[float], target: float
) -> tuple[float, float]:
    covered = 0.0
    for index, length in enumerate(segments):
        if covered + length >= target:
            ratio = 0 if length <= 0 else (target - covered) / length
            return (
                points[index][0] + (points[index + 1][0] - points[index][0]) * ratio,
                points[index][1] + (points[index + 1][1] - points[index][1]) * ratio,
            )
        covered += length
    return tuple(points[-1])


def _point_segment_distance(
    point: list[float], start: tuple[float, float], end: tuple[float, float]
) -> float:
    lat_scale = 111320.0
    lng_scale = lat_scale * math.cos(math.radians(point[0]))
    ax, ay = (start[1] - point[1]) * lng_scale, (start[0] - point[0]) * lat_scale
    bx, by = (end[1] - point[1]) * lng_scale, (end[0] - point[0]) * lat_scale
    dx, dy = bx - ax, by - ay
    denominator = dx * dx + dy * dy
    ratio = 0.0 if denominator == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / denominator))
    return math.hypot(ax + ratio * dx, ay + ratio * dy)


def _crossed_station(
    previous: tuple[float, float], current: tuple[float, float], visited: set[str]
) -> str | None:
    for name, coords in config.MRT_STATIONS_DB.items():
        if name not in visited and _point_segment_distance(coords, previous, current) <= 50.0:
            return name
    return None


def _movement_speed(
    remaining: float, remaining_time: float, nominal_speed: float, is_mrt: bool
) -> float:
    required_speed = remaining / max(0.001, remaining_time)
    return min(required_speed, nominal_speed * 1.35) if is_mrt else nominal_speed


def move_step(ctx: MissionContext, step: dict, deadline: float) -> bool:
    points = step.get("points", [])
    if len(points) < 2:
        return ctx.should_continue()
    segments, total = _route_segments(points)
    if total <= 0:
        return ctx.should_continue()
    duration = max(1.0, float(step.get("duration_seconds", 1)))
    nominal_speed = total / duration
    is_mrt = step.get("vehicle_type") == "SUBWAY"
    if step.get("precision_alignment"):
        action = "Precision Alignment"
        logger.log_route(
            f"Walking {step.get('distance_meters', 0)}m directly to the precise coordinate."
        )
    else:
        action = f"Taking MRT {step.get('line') or ''}".strip() if is_mrt else (
            "Walking" if step.get("travel_mode") == "WALKING" else "Taking Bus"
        )
    progress = 0.0
    previous = tuple(points[0])
    visited: set[str] = set()
    last_tick = time.monotonic()
    while ctx.should_continue() and progress < total:
        tick_started = time.monotonic()
        tick_elapsed = max(0.001, tick_started - last_tick)
        last_tick = tick_started
        remaining = total - progress
        speed = _movement_speed(remaining, deadline - tick_started, nominal_speed, is_mrt)
        progress = min(total, progress + speed * tick_elapsed)
        current = _point_at_distance(points, segments, progress)
        if is_mrt:
            station = _crossed_station(previous, current, visited)
            if station:
                visited.add(station)
                dwell = random.randint(8, 12)
                logger.log_route(f"Arrived at [{station}], stopping for {dwell}s.")
                send_position(ctx, *current, "Station Arrival", note=station)
                if not ctx.wait(dwell, "Station Stop", note=station, record_boundaries=False):
                    return False
                send_position(ctx, *current, "Station Departure", note=station)
                last_tick = time.monotonic()
        send_position(ctx, *current, action)
        previous = current
        elapsed = time.monotonic() - tick_started
        if elapsed < 1.0:
            ctx.cancel_event.wait(1.0 - elapsed)
    return ctx.should_continue()


def _wait_until(ctx: MissionContext, iso_value: str, action: str) -> bool:
    target = datetime.fromisoformat(iso_value)
    seconds = max(0.0, (target - datetime.now(timezone.utc)).total_seconds())
    return ctx.wait(seconds, action) if seconds > 0 else ctx.should_continue()


def _projected_arrival(routes: list[dict], completed_index: int, actual_arrival: datetime) -> datetime:
    if completed_index >= len(routes):
        return actual_arrival
    completed_planned = datetime.fromisoformat(routes[completed_index - 1]["arrival_at"])
    final_planned = datetime.fromisoformat(routes[-1]["arrival_at"])
    return actual_arrival + max(timedelta(0), final_planned - completed_planned)


def _route_delay_seconds(route: dict, actual_arrival: datetime) -> float:
    return max(0.0, (actual_arrival - datetime.fromisoformat(route["arrival_at"])).total_seconds())


def execute_mission(mission: dict[str, Any], worker_shutdown: threading.Event) -> None:
    ctx = MissionContext(mission["id"], worker_shutdown)
    try:
        _open_mission_logs(mission)
        plan = mission["plan"]
        routes = plan["routes"]
        init_lat, init_lng = [
            float(value.strip()) for value in mission["payload"]["init_loc"].split(",", 1)
        ]
        send_position(ctx, init_lat, init_lng, "Mission Start")
        route_index = 0
        while route_index < len(routes) and ctx.should_continue():
            route = routes[route_index]
            db.update_mission(ctx.mission_id, current_target=route["destination"])
            if route_index == 0 and not _wait_until(ctx, route["departure_at"], "Scheduled Wait"):
                break
            step_deadline = time.monotonic()
            for step in route["steps"]:
                if step.get("departure_time"):
                    if not _wait_until(ctx, step["departure_time"], "Transit Wait"):
                        break
                    step_deadline = time.monotonic()
                step_deadline += max(1, step["duration_seconds"])
                if not move_step(ctx, step, step_deadline):
                    break
            if not ctx.should_continue():
                break

            persist_position(ctx)
            route_index += 1
            actual_arrival = datetime.now(timezone.utc)
            route_delay = _route_delay_seconds(route, actual_arrival)
            projected = _projected_arrival(routes, route_index, actual_arrival)
            initial_eta = datetime.fromisoformat(mission["initial_google_eta"])
            projected_debt = max(0.0, (projected - initial_eta).total_seconds())
            db.update_mission(
                ctx.mission_id,
                completed_stops=route_index,
                schedule_debt_seconds=projected_debt,
            )

            current_stop = mission["payload"]["stops"][route_index - 1]
            resume_at = scheduled_departure(actual_arrival.astimezone(config.TIMEZONE), current_stop)
            if not _wait_until(ctx, resume_at.isoformat(), "Stop Wait"):
                break
            history.persist_mission(ctx.mission_id)
            # A missed scheduled departure can move to tomorrow even if the
            # arrival was only seconds late. Replan that case as well.
            shifted = route_index < len(routes) and abs((
                resume_at - datetime.fromisoformat(routes[route_index]["departure_at"])
            ).total_seconds()) > 60
            if (route_delay > 60.0 or shifted) and route_index < len(routes):
                latest = db.get_mission(ctx.mission_id, include_plan=False)
                remaining_stops = mission["payload"]["stops"][route_index:]
                position = ctx.last_position or (latest["last_lat"], latest["last_lng"])
                origin = f"{position[0]},{position[1]}"
                try:
                    replanned = plan_mission(
                        origin, remaining_stops, start_time=resume_at, should_continue=ctx.should_continue,
                    )
                    routes = routes[:route_index] + replanned["routes"]
                    plan["routes"] = routes
                    latest_eta = replanned["initial_google_eta"]
                    debt = max(
                        0.0,
                        (datetime.fromisoformat(latest_eta) - initial_eta).total_seconds(),
                    )
                    db.update_mission(
                        ctx.mission_id,
                        plan_json=plan,
                        latest_google_eta=latest_eta,
                        route_revision=latest["route_revision"] + 1,
                        planned_route_points=db.plan_point_count(plan),
                        schedule_debt_seconds=debt,
                        status="running",
                        last_error=None,
                    )
                    history.persist_mission(ctx.mission_id)
                except PlanningCancelled:
                    ctx.cancel_event.set()
                    break
                except Exception as exc:
                    db.update_mission(
                        ctx.mission_id,
                        status="degraded",
                        last_error=f"Replan failed: {exc}",
                    )

        if not ctx.should_continue():
            status = "interrupted" if worker_shutdown.is_set() else "aborted"
            db.update_mission(
                ctx.mission_id,
                status=status,
                finished_at=db.utc_now(),
                worker_id=None,
                is_holding_final_position=0,
            )
            return

        persist_position(ctx)
        final_position = ctx.last_position
        if final_position is None:
            raise RuntimeError("Mission completed without a position")
        db.update_mission(
            ctx.mission_id,
            status="completed",
            finished_at=db.utc_now(),
            current_target="Mission Complete",
            is_holding_final_position=1,
        )
        logger.log_route("Mission complete. Holding final position.")
        history.persist_mission(ctx.mission_id)
        send_position(ctx, *final_position, "Mission Complete")
        next_heartbeat = time.monotonic() + 60.0
        while ctx.should_continue():
            now = time.monotonic()
            record = now >= next_heartbeat
            send_position(
                ctx,
                *final_position,
                "Completed Heartbeat",
                record=record,
            )
            if record:
                next_heartbeat = now + 60.0
            ctx.cancel_event.wait(1.0)
        # Stopping the final-position hold ends the hold, not the mission: the
        # run itself succeeded and history must not show it as STOPPED.
        db.update_mission(
            ctx.mission_id,
            status="completed",
            worker_id=None,
            is_holding_final_position=0,
        )
    except Exception as exc:
        logger.log_sys(f"Mission worker failed: {exc}", "error")
        db.update_mission(
            ctx.mission_id,
            status="failed",
            finished_at=db.utc_now(),
            last_error=str(exc),
            worker_id=None,
            is_holding_final_position=0,
        )
    finally:
        # Drain any coordinate still queued for this mission; _phone_loop would
        # otherwise write phone_status back onto a mission that already ended.
        while True:
            try:
                _phone_queue.get_nowait()
            except queue.Empty:
                break
        _phone_health.pop(ctx.mission_id, None)
        try:
            persist_position(ctx)
            history.persist_mission(ctx.mission_id)
        finally:
            logger.close_task_logs()


def _plan_and_execute_mission(
    mission: dict[str, Any], worker_shutdown: threading.Event
) -> None:
    mission_id = mission["id"]
    if db.is_cancel_requested(mission_id) or worker_shutdown.is_set():
        db.update_mission(
            mission_id,
            status="stopped" if not worker_shutdown.is_set() else "interrupted",
            finished_at=db.utc_now(),
            worker_id=None,
        )
        history.persist_mission(mission_id)
        return
    try:
        _open_mission_logs(mission)
        payload = mission["payload"]
        plan = plan_mission(
            payload["init_loc"], payload["stops"],
            should_continue=lambda: not worker_shutdown.is_set() and not db.is_cancel_requested(mission_id),
        )
        if db.is_cancel_requested(mission_id) or worker_shutdown.is_set():
            db.update_mission(
                mission_id,
                status="stopped" if not worker_shutdown.is_set() else "interrupted",
                finished_at=db.utc_now(),
                worker_id=None,
            )
            return
        db.update_mission(
            mission_id,
            status="running",
            started_at=db.utc_now(),
            plan_json=plan,
            initial_google_eta=plan["initial_google_eta"],
            latest_google_eta=plan["initial_google_eta"],
            planned_route_points=db.plan_point_count(plan),
            route_revision=1,
            last_error=None,
        )
        planned = db.get_mission(mission_id)
        if planned:
            execute_mission(planned, worker_shutdown)
    except PlanningCancelled:
        db.update_mission(mission_id, status="interrupted" if worker_shutdown.is_set() else "stopped",
                          finished_at=db.utc_now(), worker_id=None)
    except Exception as exc:
        logger.log_sys(f"Mission planning failed: {exc}", "error")
        db.update_mission(
            mission_id,
            status="failed",
            finished_at=db.utc_now(),
            last_error=f"Planning failed: {exc}",
            worker_id=None,
            is_holding_final_position=0,
        )

    finally:
        try:
            history.persist_mission(mission_id)
        finally:
            logger.close_task_logs()


def _install_shutdown_handlers(worker_shutdown: threading.Event) -> dict[int, Any]:
    previous_handlers: dict[int, Any] = {}
    if threading.current_thread() is not threading.main_thread():
        return previous_handlers

    def request_shutdown(_signum=None, _frame=None) -> None:
        worker_shutdown.set()

    for signum in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[signum] = signal.getsignal(signum)
        signal.signal(signum, request_shutdown)
    return previous_handlers


def _renew_lease(worker_id: str, worker_shutdown: threading.Event) -> None:
    global _worker_heartbeat, _worker_started
    while not worker_shutdown.wait(10.0):
        try:
            if not db.renew_worker(worker_id, db.utc_epoch_ms() + 30_000):
                raise RuntimeError("Worker lease was lost")
            _worker_heartbeat = time.monotonic()
            start_phone_worker()
        except Exception as exc:
            logger.log_sys(f"Worker lease renewal failed: {exc}", "error")
            _worker_started = False
            worker_shutdown.set()
            return


def run_worker(poll_interval: float = 0.5, *, on_ready: Callable[[], None] | None = None) -> None:
    global _worker_started, _worker_heartbeat
    # create_app() already set the logging handlers up for this process; the
    # worker shares them because start_local.py runs both in one process.
    if config.TIMEZONE_WARNING:
        logger.log_sys(config.TIMEZONE_WARNING, "warning")
    db.init_db()
    worker_id = f"worker-{uuid.uuid4()}"
    worker_shutdown = threading.Event()
    acquisition_deadline = time.monotonic() + 35.0
    while not db.acquire_worker(worker_id, db.utc_epoch_ms() + 30_000):
        if time.monotonic() >= acquisition_deadline:
            raise RuntimeError("Another mission worker currently owns the global worker lease")
        worker_shutdown.wait(1.0)

    # The lease is ours, so no other worker can legitimately be executing a
    # mission. Anything still marked active belongs to a run that was killed.
    orphaned_ids = db.active_mission_ids()
    reclaimed = db.interrupt_active_missions("Worker restarted; the previous run did not finish")
    for mission_id in orphaned_ids:
        history.persist_mission(mission_id)
    if reclaimed:
        logger.log_sys(f"Reclaimed {reclaimed} orphaned mission(s) on worker startup", "warning")

    _worker_started = True
    _worker_heartbeat = time.monotonic()
    previous_handlers = _install_shutdown_handlers(worker_shutdown)

    threading.Thread(target=_renew_lease, args=(worker_id, worker_shutdown), name="worker-lease", daemon=True).start()

    def maintain_history() -> None:
        while not worker_shutdown.is_set():
            try:
                history.archive_expired_sessions(30)
                history.cleanup_archive_cache()
            except Exception as exc:
                logger.log_sys(f"History maintenance deferred: {exc}", "warning")
            worker_shutdown.wait(3600.0)

    threading.Thread(target=maintain_history, name="history-maintenance", daemon=True).start()
    start_phone_worker()
    logger.log_sys(f"Mission worker started: {worker_id}")
    try:
        if on_ready:
            on_ready()
        while not worker_shutdown.is_set():
            mission = db.claim_next(worker_id)
            if mission:
                if mission["status"] == "planning":
                    _plan_and_execute_mission(mission, worker_shutdown)
                else:
                    execute_mission(mission, worker_shutdown)
            else:
                worker_shutdown.wait(poll_interval)
    finally:
        worker_shutdown.set()
        _worker_started = False
        try:
            pending_ids = db.active_mission_ids()
            db.interrupt_active_missions("Application stopped")
            for mission_id in pending_ids:
                history.persist_mission(mission_id)
        finally:
            try:
                db.release_worker(worker_id)
            finally:
                for signum, previous in previous_handlers.items():
                    signal.signal(signum, previous)
