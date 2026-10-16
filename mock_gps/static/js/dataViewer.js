import { apiFetch, fetchJson } from "./api.js";
import { contextLabel, dataUrls } from "./context.js";
import { $, displayTimeZone, escapeHtml, formatDateTime, openModal, renderError } from "./ui.js";

let csvOffsets = [0];
let csvPageIndex = 0;
let csvStream = null;
let activeRequest = null;
let requestVersion = 0;

function beginRequest() {
    activeRequest?.abort();
    activeRequest = new AbortController();
    return { version: ++requestVersion, signal: activeRequest.signal };
}

document.addEventListener("modalclosed", (event) => {
    if (event.detail === "data-modal") {
        activeRequest?.abort();
        requestVersion += 1;
    }
});

function setTitle(value) {
    $("data-modal-title").textContent = `${value} · ${contextLabel()}`;
}

function renderCsvPage(page) {
    const body = $("data-body");
    const records = Array.isArray(page.records) ? page.records : [];
    const rows = records.map((record) => `
        <tr>
            <td>${Number(record.sequence)}</td>
            <td>${escapeHtml(formatDateTime(record.time))}</td>
            <td>${Number(record.lat).toFixed(7)}</td>
            <td>${Number(record.lng).toFixed(7)}</td>
            <td>${escapeHtml(record.action)}</td>
            <td>${escapeHtml(record.note || "")}</td>
        </tr>
    `).join("");
    body.innerHTML = `
        <div class="csv-table-wrap">
            <table class="csv-table">
                <thead><tr><th>Sequence</th><th>Timestamp (${escapeHtml(displayTimeZone)})</th><th>Latitude</th><th>Longitude</th><th>Action</th><th>Note</th></tr></thead>
                <tbody>${rows || '<tr><td colspan="6">This session has no movement records.</td></tr>'}</tbody>
            </table>
        </div>
        <div class="csv-pager">
            <button class="action-btn" id="csvPrev" ${csvPageIndex === 0 ? "disabled" : ""}>Previous</button>
            <span>Page ${csvPageIndex + 1}</span>
            <button class="action-btn" id="csvNext" ${page.has_more ? "" : "disabled"}>Next</button>
        </div>
    `;
    $("csvPrev").addEventListener("click", () => loadCsvPage(csvPageIndex - 1));
    $("csvNext").addEventListener("click", () => {
        if (!csvOffsets[csvPageIndex + 1]) csvOffsets[csvPageIndex + 1] = page.next_offset;
        loadCsvPage(csvPageIndex + 1);
    });
}

async function loadCsvPage(index) {
    if (index < 0) return;
    const request = beginRequest();
    csvPageIndex = index;
    $("data-body").innerHTML = '<div class="empty-state">Loading movement records...</div>';
    try {
        const page = await fetchJson(
            `${dataUrls.movements}?offset=${csvOffsets[index] || 0}&limit=250`,
            `Unable to load movement records for ${contextLabel()}.`,
            { signal: request.signal },
        );
        if (request.version !== requestVersion) return;
        if (csvStream && page.stream_id !== csvStream) {
            csvOffsets = [0];
            csvStream = page.stream_id;
            return loadCsvPage(0);
        }
        csvStream = page.stream_id;
        renderCsvPage(page);
    } catch (error) {
        if (request.version !== requestVersion || request.signal.aborted) return;
        renderError($("data-body"), error.message || "Unable to load movement records.");
    }
}

export function openCsvViewer() {
    csvOffsets = [0];
    csvStream = null;
    csvPageIndex = 0;
    setTitle("CSV contents");
    openModal("data-modal");
    loadCsvPage(0);
}

export async function openLogViewer(logName) {
    const request = beginRequest();
    setTitle(`${logName.charAt(0).toUpperCase()}${logName.slice(1)} log`);
    openModal("data-modal");
    $("data-body").innerHTML = '<div class="empty-state">Loading log...</div>';
    try {
        const response = await apiFetch(dataUrls.log(logName), { signal: request.signal });
        const text = await response.text();
        if (request.version !== requestVersion) return;
        if (!response.ok) {
            $("data-body").innerHTML = `<div class="empty-state">${escapeHtml(text || "This session has no data for this log.")}</div>`;
            return;
        }
        $("data-body").innerHTML = `<pre class="log-viewer">${escapeHtml(text || "This session has no data for this log.")}</pre>`;
    } catch (error) {
        if (request.version !== requestVersion || request.signal.aborted) return;
        renderError($("data-body"), error.message || "Unable to load log data.");
    }
}
