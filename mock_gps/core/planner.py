import math
import re
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from html import unescape
from typing import Any

import polyline

from mock_gps import config

_HTML_TAG = re.compile(r"<[^>]+>")
PRECISION_WALK_SPEED_MPS = 1.4


class PlanningCancelled(Exception):
    """The owner cancelled planning; this is not a routing failure."""


def _instruction_text(value: str) -> str:
    return unescape(_HTML_TAG.sub("", value)).strip()


def _select_route(routes: list[dict], transit_type: str) -> dict:
    if transit_type == "MRT":
        for route in routes:
            if any(
                step.get("transit_details", {}).get("line", {}).get("vehicle", {}).get("type") == "SUBWAY"
                for leg in route.get("legs", []) for step in leg.get("steps", [])
            ):
                return route
    return routes[0]


def scheduled_departure(arrival: datetime, stop: dict) -> datetime:
    """When the mission may leave `stop` after arriving at `arrival`.

    `arrival` must already be in the configured local timezone, because the
    wait_time is resolved against that date. Navigation needs this when it
    replans mid-mission.
    """
    value = stop.get("wait_time", "")
    if not value:
        return arrival
    target = datetime.strptime(value.replace("24:", "00:"), "%H:%M").time()
    candidate = datetime.combine(arrival.date(), target, tzinfo=arrival.tzinfo)
    if candidate <= arrival:
        if stop.get("skip_if_late"):
            return arrival
        candidate += timedelta(days=1)
    return candidate


def _utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def _distance_meters(start: list[float], end: list[float]) -> float:
    radius = 6371000.0
    lat1, lng1 = map(math.radians, start)
    lat2, lng2 = map(math.radians, end)
    delta_lat = lat2 - lat1
    delta_lng = lng2 - lng1
    value = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lng / 2) ** 2
    )
    return radius * 2 * math.atan2(math.sqrt(value), math.sqrt(1 - value))


def _precision_alignment_step(steps: list[dict], leg: dict, coord: str) -> dict | None:
    if not coord:
        return None
    target = [float(value.strip()) for value in coord.split(",", 1)]
    route_points = [point for step in steps for point in step["points"]]
    if route_points:
        start = route_points[-1]
    else:
        end_location = leg.get("end_location", {})
        if "lat" not in end_location or "lng" not in end_location:
            raise RuntimeError("Google route has no endpoint for precise-coordinate alignment")
        start = [float(end_location["lat"]), float(end_location["lng"])]
    distance = _distance_meters(start, target)
    duration = max(1, math.ceil(distance / PRECISION_WALK_SPEED_MPS))
    return {
        "index": len(steps),
        "travel_mode": "WALKING",
        "vehicle_type": "",
        "line": "",
        "duration_seconds": duration,
        "distance_meters": round(distance),
        "points": [start, target],
        "instruction": "Walk directly to the precise coordinate",
        "precision_alignment": True,
    }


def plan_mission(
    init_loc: str, stops: list[dict], *, start_time: datetime | None = None,
    should_continue: Callable[[], bool] | None = None, max_seconds: float = 300,
) -> dict[str, Any]:
    if not config.gmaps_client:
        raise RuntimeError("Google Maps client is not configured")
    deadline = time.monotonic() + max_seconds

    def check_planning() -> None:
        if should_continue and not should_continue():
            raise PlanningCancelled()
        if time.monotonic() >= deadline:
            raise RuntimeError("Mission planning exceeded its time budget")

    departure = start_time or config.local_now()
    origin = init_loc
    routes: list[dict] = []
    for index, stop in enumerate(stops):
        check_planning()
        mode = stop.get("mode", "walking")
        if mode not in {"walking", "transit"}:
            raise ValueError("mode must be walking or transit")
        transit_type = stop.get("transit_type", "")
        kwargs: dict[str, Any] = {
            "mode": mode,
            "departure_time": departure,
            "language": "en-US",
            "alternatives": True,
        }
        if mode == "transit" and transit_type in {"MRT", "BUS"}:
            kwargs["transit_mode"] = ["subway" if transit_type == "MRT" else "bus"]
        destination = stop["name"]
        precision_coord = stop.get("coord", "")
        options = config.gmaps_client.directions(origin, destination, **kwargs)
        check_planning()
        if not options:
            raise RuntimeError(f"Google returned no route for stop {index + 1}: {stop['name']}")
        selected = _select_route(options, transit_type)
        leg = selected["legs"][0]
        duration = int(leg.get("duration", {}).get("value", 0))
        if duration <= 0:
            raise RuntimeError(f"Google returned an invalid duration for stop {index + 1}: {stop['name']}")
        steps = []
        for step_index, step in enumerate(leg.get("steps", [])):
            transit = step.get("transit_details", {}) or {}
            vehicle_type = transit.get("line", {}).get("vehicle", {}).get("type", "")
            points = polyline.decode(step.get("polyline", {}).get("points", ""))
            steps.append({
                "index": step_index,
                "travel_mode": step.get("travel_mode", ""),
                "vehicle_type": vehicle_type,
                "line": transit.get("line", {}).get("short_name", ""),
                "duration_seconds": int(step.get("duration", {}).get("value", 0)),
                "distance_meters": int(step.get("distance", {}).get("value", 0)),
                "points": [[lat, lng] for lat, lng in points],
                "instruction": _instruction_text(step.get("html_instructions", "")),
                "departure_stop": transit.get("departure_stop", {}).get("name", ""),
                "arrival_stop": transit.get("arrival_stop", {}).get("name", ""),
                "departure_time": _transit_time(transit.get("departure_time")),
                "arrival_time": _transit_time(transit.get("arrival_time")),
                "num_stops": transit.get("num_stops", 0),
                "vehicle_name": transit.get("line", {}).get("vehicle", {}).get("name", ""),
            })
        alignment = _precision_alignment_step(steps, leg, precision_coord)
        if alignment:
            steps.append(alignment)
            duration += alignment["duration_seconds"]
        distance_meters = int(leg.get("distance", {}).get("value", 0))
        if alignment:
            distance_meters += alignment["distance_meters"]
        arrival = departure + timedelta(seconds=duration)
        google_arrival = _transit_time(leg.get("arrival_time"))
        if google_arrival:
            arrival = datetime.fromisoformat(google_arrival).astimezone(departure.tzinfo)
            if alignment:
                arrival += timedelta(seconds=alignment["duration_seconds"])
            duration = max(1, math.ceil((arrival - departure).total_seconds()))
        routes.append({
            "origin": origin,
            "destination": destination,
            "precision_coord": precision_coord,
            "mode": mode,
            "transit_type": transit_type,
            "departure_at": _utc_iso(departure),
            "arrival_at": _utc_iso(arrival),
            "distance_meters": distance_meters,
            "duration_seconds": duration,
            "start_address": leg.get("start_address", ""),
            "end_address": leg.get("end_address", ""),
            "steps": steps,
        })
        departure = scheduled_departure(arrival, stop)
        origin = precision_coord or destination
    return {
        "planned_at": _utc_iso(start_time or config.local_now()),
        "initial_google_eta": routes[-1]["arrival_at"],
        "routes": routes,
    }


def _transit_time(value: dict | None) -> str:
    if not value or "value" not in value:
        return ""
    return _utc_iso(datetime.fromtimestamp(value["value"], timezone.utc))
