import { pageContext } from "./context.js";

let toastTimer = null;

export const displayTimeZone = pageContext.timezone || "Asia/Taipei";

export function parseTimestamp(timestamp) {
    if (!timestamp) return null;
    const normalized = String(timestamp).replace(
        /^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})/,
        "$1T$2",
    );
    const parsed = new Date(normalized);
    return Number.isNaN(parsed.getTime()) ? null : parsed;
}

const timeFormatter = new Intl.DateTimeFormat(undefined, {
    timeZone: displayTimeZone,
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hourCycle: "h23",
});

const dateTimeFormatter = new Intl.DateTimeFormat(undefined, {
    timeZone: displayTimeZone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hourCycle: "h23",
});

export function $(id) {
    return document.getElementById(id);
}

export function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, (char) => ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;",
    }[char]));
}

export function formatDuration(seconds) {
    if (seconds === null || seconds === undefined) return "--:--";
    const total = Math.max(0, Number(seconds) || 0);
    const h = Math.floor(total / 3600);
    const m = Math.floor((total % 3600) / 60);
    const s = Math.floor(total % 60);
    return h > 0
        ? `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`
        : `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}

export function extractTime(timestamp) {
    if (!timestamp) return "--:--:--";
    const parsed = parseTimestamp(timestamp);
    if (parsed) return timeFormatter.format(parsed);
    return timestamp.includes(" ") ? timestamp.split(" ")[1] : timestamp;
}

export function formatDateTime(timestamp, fallback = "Unknown") {
    const parsed = parseTimestamp(timestamp);
    return parsed ? dateTimeFormatter.format(parsed) : (timestamp || fallback);
}

export function showToast(message, type = "info") {
    const toast = $("toast");
    toast.textContent = message;
    toast.className = `toast show ${type === "error" ? "error" : ""}`;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => {
        toast.className = "toast";
    }, 4200);
}

const modalOpeners = new Map();
const focusable = 'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), [tabindex="0"]';

export function openModal(id) {
    const modal = $(id);
    if (!modal.classList.contains("show")) modalOpeners.set(id, document.activeElement);
    document.querySelectorAll(".modal.show").forEach((other) => {
        if (other.id !== id) closeModal(other.id);
    });
    modal.classList.add("show");
    modal.setAttribute("role", "dialog");
    modal.setAttribute("aria-modal", "true");
    const heading = modal.querySelector("h2");
    if (heading) {
        heading.id ||= `${id}-heading`;
        modal.setAttribute("aria-labelledby", heading.id);
    }
    modal.tabIndex = -1;
    (modal.querySelector(focusable) || modal).focus();
}

export function closeModal(id) {
    $(id).classList.remove("show");
    modalOpeners.get(id)?.focus();
    modalOpeners.delete(id);
    document.dispatchEvent(new CustomEvent("modalclosed", { detail: id }));
}

document.addEventListener("keydown", (event) => {
    const modal = document.querySelector(".modal.show");
    if (!modal) return;
    if (event.key === "Escape") {
        event.preventDefault();
        closeModal(modal.id);
    } else if (event.key === "Tab") {
        const nodes = [...modal.querySelectorAll(focusable)].filter((node) => node.getClientRects().length);
        const first = nodes[0] || modal;
        const last = nodes.at(-1) || modal;
        if (event.shiftKey && (document.activeElement === first || !modal.contains(document.activeElement))) {
            event.preventDefault(); last.focus();
        } else if (!event.shiftKey && (document.activeElement === last || !modal.contains(document.activeElement))) {
            event.preventDefault(); first.focus();
        }
    }
});

export function renderError(container, message) {
    container.innerHTML = `<div class="error-state">${escapeHtml(message)}</div>`;
}

export function renderEmpty(container, message) {
    container.innerHTML = `<div class="empty-state">${escapeHtml(message)}</div>`;
}
