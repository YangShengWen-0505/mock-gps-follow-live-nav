import { apiFetch, fetchJson } from "./api.js";
import { contextLabel, dataUrls, isHistoryPage } from "./context.js";
import {
    $, escapeHtml, extractTime, formatDateTime, formatDuration,
    openModal, renderEmpty, renderError, showToast,
} from "./ui.js";
import { updatePlannedRoute } from "./mapView.js";

export async function openMissionModal() {
    const container = $("mission-body");
    openModal("mission-modal");
    container.innerHTML = '<div class="empty-state">Loading schedule...</div>';

    try {
        const data = await fetchJson("/api/mission", "Unable to load the current mission schedule.");
        if (!data?.stops?.length) {
            renderEmpty(container, "No active mission schedule is available.");
            return;
        }

        const rows = data.stops.map((stop, index) => `
            <div class="stop-card">
                <div class="stop-card-header">
                    <span class="stop-badge">${index + 1}</span>
                    <span class="stop-name">${escapeHtml(stop.name)}</span>
                </div>
                <div class="stop-details">
                    <div>Mode: ${escapeHtml(stop.mode)} ${stop.transit_type ? `[${escapeHtml(stop.transit_type)}]` : ""}</div>
                    <div>Wait: ${escapeHtml(stop.wait_time || "None")}</div>
                    <div>Coordinate: ${escapeHtml(stop.coord || "Auto")}</div>
                </div>
            </div>
        `).join("");

        container.innerHTML = `<div class="init-loc-banner">Start: ${escapeHtml(data.init_loc)}</div>${rows}`;
    } catch (error) {
        renderError(container, error.message || "Unable to load the current mission schedule.");
    }
}

let historyOffset = 0;
let historyRequest = 0;

export async function openHistoryModal(offset = 0) {
    historyOffset = Math.max(0, offset);
    const version = ++historyRequest;
    const container = $("history-body");
    openModal("history-modal");
    container.innerHTML = '<div class="empty-state">Loading mission history...</div>';

    try {
        const sessions = await fetchJson(`/api/history?limit=100&offset=${historyOffset}`, "Unable to load mission history.");
        if (version !== historyRequest) return;
        if (!sessions.length && historyOffset === 0) {
            renderEmpty(container, "No mission history is available yet.");
            return;
        }

        container.innerHTML = sessions.map((session) => `
            <a class="history-row" href="/history/${encodeURIComponent(session.date)}/${encodeURIComponent(session.session)}" target="_blank" rel="noopener">
                <div class="history-main">
                    <span class="stop-badge"><i class="fa-solid fa-route"></i></span>
                    <span class="history-title">${escapeHtml(session.id)}</span>
                </div>
                <div class="history-meta">
                    <div>Updated: ${escapeHtml(formatDateTime(session.updated_at))}</div>
                    <div>Status: ${escapeHtml(session.status || "Recorded")}</div>
                    <div>Stops: ${session.stops ?? 0} | Size: ${Math.round((session.csv_size || 0) / 1024)} KB</div>
                    <div>Start: ${escapeHtml(session.start || "Unknown")}</div>
                </div>
            </a>
        `).join("") + `<div class="csv-pager">
            <button class="action-btn" id="historyPrev" ${historyOffset === 0 ? "disabled" : ""}>Previous</button>
            <span>Page ${Math.floor(historyOffset / 100) + 1}</span>
            <button class="action-btn" id="historyNext" ${sessions.length < 100 ? "disabled" : ""}>Next</button>
        </div>`;
        $("historyPrev").addEventListener("click", () => openHistoryModal(historyOffset - 100));
        $("historyNext").addEventListener("click", () => openHistoryModal(historyOffset + 100));
    } catch (error) {
        if (version !== historyRequest) return;
        renderError(container, error.message || "Unable to load mission history.");
    }
}

function segmentLabel(segment) {
    if (segment.vehicle_type === "SUBWAY") return "MRT";
    if (segment.vehicle_type === "BUS") return "Bus";
    const type = String(segment.type || segment.travel_mode || "move").toLowerCase();
    if (type === "walk") return "Walking";
    if (type === "mrt") return "MRT";
    if (type === "bus") return "Bus";
    if (type === "transit") return "Transit";
    return type ? type.charAt(0).toUpperCase() + type.slice(1) : "Move";
}

function renderTransitMeta(segment) {
    const parts = [];
    const line = segment.line || segment.line_short_name || segment.line_name;
    if (line) parts.push(`Route: ${escapeHtml(line)}`);
    if (segment.vehicle_name || segment.vehicle_type) {
        parts.push(`Vehicle: ${escapeHtml(segment.vehicle_name || segment.vehicle_type)}`);
    }
    if (segment.departure_stop || segment.arrival_stop) {
        parts.push(`Stops: ${escapeHtml(segment.departure_stop || "-")} -> ${escapeHtml(segment.arrival_stop || "-")}`);
    }
    if (segment.departure_time || segment.arrival_time) {
        parts.push(`Time: ${escapeHtml(segment.departure_time || "-")} -> ${escapeHtml(segment.arrival_time || "-")}`);
    }
    if (segment.num_stops) parts.push(`${Number(segment.num_stops)} stops`);
    return parts.map((part) => `<div>${part}</div>`).join("");
}

export async function openNavigationHistoryModal() {
    const container = $("navigation-body");
    openModal("navigation-modal");
    container.innerHTML = '<div class="empty-state">Loading navigation history...</div>';

    try {
        const routes = await fetchJson(dataUrls.navigation, "Unable to load navigation history.");
        if (!routes.length) {
            renderEmpty(container, `No navigation history is available for ${contextLabel()}.`);
            return;
        }

        container.innerHTML = routes.map((route, routeIndex) => {
            const segments = Array.isArray(route.steps) ? route.steps : [];
            const segmentRows = segments.map((segment) => `
                <div class="navigation-step ${escapeHtml(segment.type || "")}">
                    <div class="navigation-step-head">
                        <span class="stop-badge">${Number(segment.index || 0) || ""}</span>
                        <div>
                            <div class="navigation-step-title">${escapeHtml(segmentLabel(segment))}${segment.line ? ` ${escapeHtml(segment.line)}` : ""}</div>
                            <div class="navigation-step-sub">${Number(segment.distance_meters || 0).toLocaleString()} m | ${formatDuration(segment.duration_seconds || 0)}</div>
                        </div>
                    </div>
                    <div class="navigation-step-body">
                        ${segment.instruction ? `<div>${escapeHtml(segment.instruction)}</div>` : ""}
                        ${renderTransitMeta(segment)}
                    </div>
                </div>
            `).join("");

            return `
                <section class="navigation-route">
                    <div class="navigation-route-head">
                        <div>
                            <div class="history-title">Route ${routeIndex + 1}: ${escapeHtml(route.origin)} -> ${escapeHtml(route.destination)}</div>
                            <div class="history-meta compact">
                                <div>Mode: ${escapeHtml(route.mode || "-")} ${route.transit_type ? `(${escapeHtml(route.transit_type)})` : ""}</div>
                                <div>Distance: ${Number(route.distance_meters || 0).toLocaleString()} m | Duration: ${formatDuration(route.duration_seconds || 0)}</div>
                                <div>Departure: ${escapeHtml(formatDateTime(route.departure_at, "-"))} | Arrival: ${escapeHtml(formatDateTime(route.arrival_at, "-"))}</div>
                            </div>
                        </div>
                    </div>
                    <div class="navigation-steps">${segmentRows || '<div class="empty-state">No step details were returned.</div>'}</div>
                </section>
            `;
        }).join("");
    } catch (error) {
        renderError(container, error.message || "Unable to load navigation history.");
    }
}

export async function refreshSystemStatus() {
    try {
        const data = await fetchJson(dataUrls.status, `Unable to load system status for ${contextLabel()}.`);
        const statusText = (data.mission_stats?.status || (data.mission_active ? "running" : "idle")).toUpperCase();
        const status = $("mission-status");
        status.textContent = statusText;
        status.className = `status-pill ${statusText.toLowerCase()}`;
        $("mission-state-text").textContent = data.is_holding_final_position ? `${statusText} · FINAL ACTIVE` : statusText;
        $("p2p-state-text").textContent = (data.phone_status || "standby").toUpperCase();

        $("mission-progress").textContent = `${data.mission_stats.completed_stops}/${data.mission_stats.total_stops}`;
        $("mission-target").textContent = data.mission_stats.current_target || "Idle";
        $("tunnel-url").textContent = data.p2p_target ? `P2P Target: ${data.p2p_target}` : "Tailscale standby";
        $("elapsed-time").textContent = formatDuration(data.elapsed_seconds);
        $("initial-eta").textContent = data.initial_google_eta ? extractTime(data.initial_google_eta) : "--";
        $("latest-eta").textContent = data.latest_google_eta ? extractTime(data.latest_google_eta) : "--";
        $("route-points").textContent = data.planned_route_points || 0;
        $("schedule-debt").textContent = `${Math.round(data.mission_stats?.schedule_debt_seconds || 0)}s`;
        const missionError = $("mission-error");
        missionError.textContent = data.mission_stats?.last_error || "";
        missionError.hidden = !missionError.textContent;
        $("log-session").textContent = data.log_session || "No active log session";
        return true;
    } catch (error) {
        showToast(error.message || "Unable to load system status.", "error");
        return false;
    }
}

export async function refreshPlannedRoute() {
    try {
        const token = isHistoryPage ? "" : (refreshPlannedRoute.routeToken || "");
        const separator = dataUrls.route.includes("?") ? "&" : "?";
        const response = await apiFetch(`${dataUrls.route}${separator}route_token=${encodeURIComponent(token)}`);
        if (response.status === 304) return;
        if (!response.ok) throw new Error("Unable to load the planned route.");
        const data = await response.json();
        refreshPlannedRoute.routeToken = data.route_token;
        updatePlannedRoute(data.points);
        return true;
    } catch (error) {
        showToast(error.message || "Unable to load the planned route.", "error");
        return false;
    }
}
