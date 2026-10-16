import { fetchJson } from "./api.js";
import { contextLabel, dataUrls, isHistoryPage } from "./context.js";
import { $, extractTime, parseTimestamp, showToast } from "./ui.js";
import { renderRecords, renderRecordsIncremental, updateMarker } from "./mapView.js";

let allRecords = [];
let cursor = 0;
let streamId = null;
let followingLive = !isHistoryPage;
let isPlaying = false;
let playbackIndex = 0;
let playTimer = null;
let nextFrameAt = 0;
let movementTimer = null;
let fetchInterval = 1000;
let lastErrorMessage = "";
let movementInFlight = false;
let generation = 0;
const MAX_RECORDS = 20000;
let historyOffsets = [0];
let historyPage = 0;
let historyHasMore = false;
let historyLoading = false;

function syncPlaybackUi() {
    const slider = $("timeSlider");
    slider.max = Math.max(0, allRecords.length - 1);
    slider.value = Math.min(playbackIndex, Math.max(0, allRecords.length - 1));
    $("playback-end-time").textContent = allRecords.length ? extractTime(allRecords.at(-1).time) : "--:--:--";
}

export function updatePlaybackState() {
    if (!allRecords.length) return;
    const record = allRecords[playbackIndex];
    $("timeSlider").value = playbackIndex;
    $("playback-time").textContent = extractTime(record.time);
    $("lat").textContent = record.lat.toFixed(6);
    $("lng").textContent = record.lng.toFixed(6);
    $("action").textContent = record.action;
    $("lastFix").textContent = extractTime(record.time);
    $("liveMode").textContent = isHistoryPage ? "HISTORY" : (followingLive ? "LIVE" : "PLAYBACK");
    $("liveBtn").classList.toggle("active", followingLive && !isHistoryPage);
    $("status-title-text").textContent = isHistoryPage ? contextLabel() : "Current Status (LIVE)";
    updateMarker(record, $("autoFollow").checked);
}

function resetRecords() {
    // Playback must stop first: its next frame would otherwise run against an
    // emptied record list.
    stopPlayback();
    allRecords = [];
    cursor = 0;
    streamId = null;
    playbackIndex = 0;
    syncPlaybackUi();
    $("playback-time").textContent = "--:--:--";
    renderRecords([]);
}

function recordTimeMs(index) {
    const record = allRecords[index];
    const parsed = record ? parseTimestamp(record.time) : null;
    return parsed ? parsed.getTime() : null;
}

function recordGapMs(fromIndex, toIndex) {
    const from = recordTimeMs(fromIndex);
    const to = recordTimeMs(toIndex);
    // An unreadable timestamp falls back to the nominal one-second fix
    // interval so playback keeps advancing instead of stalling.
    if (from === null || to === null) return 1000;
    const gap = to - from;
    return Number.isFinite(gap) && gap > 0 ? gap : 0;
}

function syncPlayButton() {
    $("playPauseBtn").innerHTML = isPlaying
        ? '<i class="fa-solid fa-pause"></i>'
        : '<i class="fa-solid fa-play"></i>';
}

function stopPlayback() {
    clearTimeout(playTimer);
    playTimer = null;
    isPlaying = false;
    syncPlayButton();
}

function scheduleNextFrame() {
    const nextIndex = playbackIndex + 1;
    if (nextIndex > allRecords.length - 1) {
        stopPlayback();
        return;
    }
    // Replay at 1x: wait exactly the wall-clock time that separated the two
    // fixes. nextFrameAt carries the intended schedule forward, so each timer
    // corrects the previous one's jitter instead of letting it accumulate
    // across the tens of thousands of records a long mission produces.
    nextFrameAt += recordGapMs(playbackIndex, nextIndex);
    playTimer = setTimeout(playbackStep, Math.max(0, nextFrameAt - performance.now()));
}

function playbackStep() {
    playbackIndex += 1;
    updatePlaybackState();
    scheduleNextFrame();
}

export function togglePlay() {
    if (!allRecords.length) {
        showToast("No movement data is available for playback yet.", "error");
        return;
    }
    if (isPlaying) {
        stopPlayback();
        return;
    }
    isPlaying = true;
    syncPlayButton();
    followingLive = false;
    if (playbackIndex >= allRecords.length - 1) playbackIndex = 0;
    nextFrameAt = performance.now();
    scheduleNextFrame();
}

async function loadMovementPage({ append, live }) {
    const version = generation;
    const page = await fetchJson(
        `${dataUrls.movements}?offset=${append ? cursor : 0}&limit=500`,
        "Unable to load movement data.",
    );
    if (version !== generation) return { records: [], has_more: false };
    if (streamId && page.stream_id !== streamId) {
        // A new mission started; this page was fetched with the previous
        // stream's offset, so discard it and restart the cursor from zero.
        resetRecords();
        streamId = page.stream_id;
        return loadMovementPage({ append: false, live });
    }
    streamId = page.stream_id;
    cursor = page.next_offset;
    const records = Array.isArray(page.records) ? page.records : [];
    if (!append) {
        allRecords = records;
        playbackIndex = Math.max(0, records.length - 1);
        renderRecords(allRecords);
    } else if (records.length) {
        allRecords.push(...records);
        if (allRecords.length > MAX_RECORDS) {
            const removed = Math.min(5000, allRecords.length);
            allRecords.splice(0, removed);
            playbackIndex = Math.max(0, playbackIndex - removed);
            stopPlayback();
            renderRecords(allRecords);
        } else {
            renderRecordsIncremental(records, allRecords.length);
        }
        // Only jump to the newest fix while the user is actually following
        // live; otherwise every poll would yank them out of playback.
        if (live && followingLive) playbackIndex = allRecords.length - 1;
    }
    syncPlaybackUi();
    if (allRecords.length) updatePlaybackState();
    return page;
}

function scheduleMovementFetch(delay = fetchInterval) {
    clearTimeout(movementTimer);
    if (!isHistoryPage) movementTimer = setTimeout(fetchLiveMovements, delay);
}

export async function fetchLiveMovements() {
    if (isHistoryPage || movementInFlight) return;
    if (document.hidden) {
        scheduleMovementFetch(10000);
        return;
    }
    movementInFlight = true;
    try {
        const page = await loadMovementPage({ append: true, live: true });
        fetchInterval = page.records?.length ? 1000 : Math.min(fetchInterval * 1.3, 5000);
        if (lastErrorMessage) showToast("Live movement connection restored.");
        lastErrorMessage = "";
        scheduleMovementFetch(page.has_more ? 0 : fetchInterval);
    } catch (error) {
        const message = error.message || "Unable to load live movement data.";
        if (message !== lastErrorMessage) showToast(message, "error");
        lastErrorMessage = message;
        fetchInterval = Math.min(fetchInterval * 2, 30000);
        scheduleMovementFetch(fetchInterval);
    } finally {
        movementInFlight = false;
    }
}

export async function loadInitialMovements() {
    if (!isHistoryPage) {
        await fetchLiveMovements();
        return;
    }
    if (historyLoading) return;
    historyLoading = true;
    $("historyEarlier").disabled = true;
    $("historyLater").disabled = true;
    resetRecords();
    cursor = historyOffsets[historyPage];
    try {
        let page = await loadMovementPage({ append: true, live: false });
        while (page.has_more && allRecords.length < MAX_RECORDS) {
            page = await loadMovementPage({ append: true, live: false });
            await new Promise((resolve) => window.setTimeout(resolve, 0));
        }
        historyHasMore = page.has_more;
        historyOffsets[historyPage + 1] = cursor;
        playbackIndex = 0;
        updatePlaybackState();
    } finally {
        historyLoading = false;
        $("historyEarlier").disabled = historyPage === 0;
        $("historyLater").disabled = !historyHasMore;
    }
}

export function clearPlaybackData() {
    // Keep the stream cursor: clearing the view must not refetch old traces.
    const savedCursor = cursor;
    const savedStream = streamId;
    generation += 1;
    resetRecords();
    cursor = savedCursor;
    streamId = savedStream;
}

export function bindPlaybackControls() {
    $("playPauseBtn").addEventListener("click", togglePlay);
    if (isHistoryPage) {
        for (const [id, delta] of [["historyEarlier", -1], ["historyLater", 1]]) {
            $(id).addEventListener("click", async () => {
                if (historyLoading) return;
                historyPage = Math.max(0, historyPage + delta);
                try { await loadInitialMovements(); }
                catch (error) { showToast(error.message, "error"); }
            });
        }
        $("liveBtn").textContent = "HISTORY";
        $("liveBtn").disabled = true;
        $("liveBtn").classList.remove("active");
    } else {
        $("liveBtn").addEventListener("click", () => {
            if (isPlaying) togglePlay();
            followingLive = true;
            playbackIndex = Math.max(0, allRecords.length - 1);
            updatePlaybackState();
            showToast("Returned to the latest live position.");
        });
    }
    $("timeSlider").addEventListener("input", (event) => {
        if (isPlaying) togglePlay();
        followingLive = false;
        playbackIndex = Number(event.target.value);
        updatePlaybackState();
    });
    $("showPoints").addEventListener("change", () => renderRecords(allRecords));
    $("typeFilter").addEventListener("change", () => renderRecords(allRecords));
    document.addEventListener("visibilitychange", () => {
        if (!document.hidden && !isHistoryPage) scheduleMovementFetch(0);
    });
}
