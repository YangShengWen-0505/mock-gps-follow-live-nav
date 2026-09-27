import { $, escapeHtml, formatDateTime } from "./ui.js";

export const colors = {
    walk: "#30D158",
    bus: "#FF9F0A",
    mrt: "#0A84FF",
    wait: "#BF5AF2",
    special: "#FF453A",
};

let map;
let currentBaseLayer;
let polylinesLayer;
let pointsLayer;
let plannedRouteLayer;
let twinMarker;
let plannedRouteDecorator = null;
let activePolyline = null;
let activePolylineColor = null;
let lastDrawnRecord = null;
let mapTotalDistance = 0;
let pointStride = 1;
const POINT_STRIDE_TARGET = 5000;

function getIconHtml(faIcon) {
    return `<div style="background:rgba(0,0,0,.62);border:1px solid rgba(255,255,255,.22);border-radius:50%;width:30px;height:30px;display:flex;align-items:center;justify-content:center;color:#fff;font-size:14px;box-shadow:0 4px 10px rgba(0,0,0,.32);"><i class="${faIcon}"></i></div>`;
}

const icons = {
    walk: () => L.divIcon({ html: getIconHtml("fa-solid fa-person-walking"), className: "", iconSize: [30, 30], iconAnchor: [15, 15] }),
    bus: () => L.divIcon({ html: getIconHtml("fa-solid fa-bus"), className: "", iconSize: [30, 30], iconAnchor: [15, 15] }),
    mrt: () => L.divIcon({ html: getIconHtml("fa-solid fa-train-subway"), className: "", iconSize: [30, 30], iconAnchor: [15, 15] }),
    wait: () => L.divIcon({ html: getIconHtml("fa-solid fa-hourglass-half"), className: "", iconSize: [30, 30], iconAnchor: [15, 15] }),
};

export function categorizeAction(action) {
    const value = String(action || "").toLowerCase();
    if (value.includes("walk")) return "walk";
    if (value.includes("bus")) return "bus";
    if (value.includes("mrt") || value.includes("station")) return "mrt";
    return "wait";
}

function colorForAction(action) {
    const value = String(action || "").toLowerCase();
    if (value.includes("precision") || value.includes("direct") || value.includes("alignment")) return colors.special;
    return colors[categorizeAction(action)];
}

function calcDistKm(lat1, lon1, lat2, lon2) {
    const radiusKm = 6371;
    const dLat = (lat2 - lat1) * Math.PI / 180;
    const dLon = (lon2 - lon1) * Math.PI / 180;
    const a = Math.sin(dLat / 2) ** 2
        + Math.cos(lat1 * Math.PI / 180) * Math.cos(lat2 * Math.PI / 180) * Math.sin(dLon / 2) ** 2;
    return radiusKm * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
}

function fixLayerOrder() {
    if (map.hasLayer(plannedRouteLayer)) plannedRouteLayer.bringToFront();
    polylinesLayer.bringToFront();
    if ($("showPoints").checked) pointsLayer.bringToFront();
}

export function initMap() {
    if (typeof L === "undefined") {
        throw new Error("Leaflet failed to load from /static/vendor/. Reload the page; if it persists, reinstall the app assets.");
    }
    if (typeof L.maplibreGL !== "function") {
        throw new Error("OpenFreeMap renderer failed to load. Check the MapLibre CDN connection and reload the page.");
    }

    map = L.map("map", { zoomControl: false, preferCanvas: true }).setView([25.1673, 121.4466], 15);
    L.control.zoom({ position: "bottomright" }).addTo(map);

    const openFreeMapAttribution = [
        '<a href="https://openfreemap.org/" target="_blank" rel="noopener">OpenFreeMap</a>',
        '<a href="https://openmaptiles.org/" target="_blank" rel="noopener">OpenMapTiles</a>',
        '<a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap contributors</a>',
    ].join(" · ");
    const baseMaps = {
        osmDark: L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 19 }),
        cartoDark: L.tileLayer("https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png", { maxZoom: 20 }),
        openFreeMap: L.maplibreGL({
            style: "https://tiles.openfreemap.org/styles/liberty",
            attributionControl: { customAttribution: openFreeMapAttribution },
        }),
    };

    currentBaseLayer = baseMaps.openFreeMap;
    currentBaseLayer.addTo(map);
    polylinesLayer = L.featureGroup().addTo(map);
    pointsLayer = L.featureGroup().addTo(map);
    plannedRouteLayer = L.polyline([], { color: colors.special, weight: 5, opacity: 0.8, dashArray: "8, 8" }).addTo(map);
    twinMarker = L.marker([0, 0], { icon: icons.walk() }).addTo(map);

    $("mapLayerSelect").addEventListener("change", (event) => {
        map.removeLayer(currentBaseLayer);
        currentBaseLayer = baseMaps[event.target.value];
        currentBaseLayer.addTo(map);
        $("map").classList.toggle("dark-filter", event.target.value === "osmDark");
    });

    $("showPlannedRoute").addEventListener("change", (event) => {
        if (event.target.checked) {
            map.addLayer(plannedRouteLayer);
            if (plannedRouteDecorator) map.addLayer(plannedRouteDecorator);
            fixLayerOrder();
        } else {
            map.removeLayer(plannedRouteLayer);
            if (plannedRouteDecorator) map.removeLayer(plannedRouteDecorator);
        }
    });

    $("legend").innerHTML = [
        ["Walk", colors.walk],
        ["MRT", colors.mrt],
        ["Bus", colors.bus],
        ["Wait", colors.wait],
        ["Special", colors.special],
    ].map(([name, color]) => `<div class="legend-item"><div class="color-dot" style="background:${color}"></div>${name}</div>`).join("");

    let resizeTimer;
    const invalidateMapSize = () => {
        window.clearTimeout(resizeTimer);
        resizeTimer = window.setTimeout(() => map.invalidateSize({ pan: false }), 120);
    };
    window.addEventListener("resize", invalidateMapSize, { passive: true });
    window.addEventListener("orientationchange", invalidateMapSize, { passive: true });
    invalidateMapSize();
}

export function resetMapStats() {
    activePolyline = null;
    activePolylineColor = null;
    lastDrawnRecord = null;
    mapTotalDistance = 0;
    polylinesLayer.clearLayers();
    pointsLayer.clearLayers();
    $("totalPoints").textContent = "0";
    $("totalDistance").innerHTML = '0.00<span class="stat-unit">km</span>';
}

export function renderRecords(records) {
    resetMapStats();
    // Only a full redraw may change the sampling density; incremental appends
    // must keep the stride the rest of the track was already drawn with.
    pointStride = Math.max(1, Math.ceil(records.length / POINT_STRIDE_TARGET));
    renderRecordsIncremental(records, records.length);
}

export function renderRecordsIncremental(records, totalCount) {
    const typeFilter = $("typeFilter").value;
    const showPoints = $("showPoints").checked;

    records.forEach((record) => {
        if (typeFilter !== "ALL" && categorizeAction(record.action) !== typeFilter) return;

        const latlng = [record.lat, record.lng];
        const isContinuous = lastDrawnRecord && (record.sequence - lastDrawnRecord.sequence <= 2);
        if (isContinuous) mapTotalDistance += calcDistKm(lastDrawnRecord.lat, lastDrawnRecord.lng, record.lat, record.lng);

        const recordColor = colorForAction(record.action);
        if (!activePolyline || activePolylineColor !== recordColor || !isContinuous) {
            activePolyline = L.polyline(isContinuous ? [[lastDrawnRecord.lat, lastDrawnRecord.lng], latlng] : [latlng], {
                color: recordColor,
                weight: 5,
                opacity: 0.85,
            }).addTo(polylinesLayer);
            activePolylineColor = recordColor;
        } else {
            activePolyline.addLatLng(latlng);
        }

        if (showPoints && Number(record.sequence || 0) % pointStride === 0) {
            const tooltip = `<div><strong style="color:${recordColor}">${escapeHtml(record.action)}</strong><br><span>${escapeHtml(formatDateTime(record.time))}</span></div>`;
            L.circleMarker(latlng, { radius: 4, color: "#fff", weight: 1, fillColor: recordColor, fillOpacity: 0.9 })
                .bindTooltip(tooltip, { className: "custom-tooltip", direction: "top", opacity: 1 })
                .addTo(pointsLayer);
        }
        lastDrawnRecord = record;
    });

    $("totalPoints").textContent = Number(totalCount || 0).toLocaleString();
    $("totalDistance").innerHTML = `${mapTotalDistance.toFixed(2)}<span class="stat-unit">km</span>`;
    fixLayerOrder();
}

export function updateMarker(record, autoFollow) {
    if (!record) return;
    const latlng = [record.lat, record.lng];
    twinMarker.setLatLng(latlng).setIcon((icons[categorizeAction(record.action)] || icons.walk)());
    if (autoFollow) map.panTo(latlng, { animate: true, duration: 0.7, easeLinearity: 0.1 });
}

export function updatePlannedRoute(points) {
    if (!Array.isArray(points) || points.length === 0) {
        plannedRouteLayer.setLatLngs([]);
        if (plannedRouteDecorator) {
            map.removeLayer(plannedRouteDecorator);
            plannedRouteDecorator = null;
        }
        return;
    }

    plannedRouteLayer.setLatLngs(points.map((point) => [point.lat, point.lng]));
    if (plannedRouteDecorator) map.removeLayer(plannedRouteDecorator);
    plannedRouteDecorator = typeof L.polylineDecorator === "function" ? L.polylineDecorator(plannedRouteLayer, {
        patterns: [{ offset: 25, repeat: 100, symbol: L.Symbol.arrowHead({ pixelSize: 12, polygon: false, pathOptions: { stroke: true, weight: 2, color: colors.special } }) }],
    }) : null;
    if ($("showPlannedRoute").checked) {
        if (!map.hasLayer(plannedRouteLayer)) map.addLayer(plannedRouteLayer);
        if (plannedRouteDecorator) map.addLayer(plannedRouteDecorator);
    }
    fixLayerOrder();
}
