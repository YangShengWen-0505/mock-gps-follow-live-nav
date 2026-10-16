import { $, closeModal, showToast } from "./ui.js";
import { isHistoryPage } from "./context.js";
import { openCsvViewer, openLogViewer } from "./dataViewer.js";
import { initMap, resetMapStats } from "./mapView.js";
import { bindPlaybackControls, clearPlaybackData, loadInitialMovements } from "./playback.js";
import { openHistoryModal, openMissionModal, openNavigationHistoryModal, refreshPlannedRoute, refreshSystemStatus } from "./panels.js";

function bindShellControls() {
    const sidebar = $("right-sidebar");
    const compactSidebarQuery = window.matchMedia("(max-width: 1180px)");
    const commandMenuBtn = $("commandMenuBtn");
    // The command menu and the status panel share one breakpoint.
    const compactQuery = window.matchMedia("(max-width: 900px)");
    const statusPanelBtn = $("statusPanelBtn");
    const closeCommandMenu = () => document.body.classList.remove("commands-open");
    const syncCommandMode = () => {
        if (!compactQuery.matches) closeCommandMenu();
    };
    // Crossing the breakpoint drops both status modifiers either way: the
    // compact layout uses "status-open" and the wide one "status-collapsed",
    // so whichever was set belongs to the layout being left behind.
    const syncStatusMode = () => {
        document.body.classList.remove("status-open", "status-collapsed");
    };
    const syncSidebarMode = () => {
        if (compactSidebarQuery.matches) {
            const open = document.body.classList.contains("tools-open");
            sidebar.classList.toggle("collapsed", !open);
            document.body.classList.remove("tools-collapsed");
        } else {
            document.body.classList.remove("tools-open");
            sidebar.classList.remove("collapsed");
            document.body.classList.remove("tools-collapsed");
        }
    };

    commandMenuBtn.addEventListener("click", () => {
        document.body.classList.toggle("commands-open");
    });
    compactQuery.addEventListener("change", syncCommandMode);
    syncCommandMode();

    statusPanelBtn.addEventListener("click", () => {
        closeCommandMenu();
        if (compactQuery.matches) {
            document.body.classList.toggle("status-open");
            return;
        }
        document.body.classList.toggle("status-collapsed");
    });
    compactQuery.addEventListener("change", syncStatusMode);
    syncStatusMode();

    $("sidebarBtn").addEventListener("click", () => {
        if (compactSidebarQuery.matches) {
            const open = document.body.classList.toggle("tools-open");
            sidebar.classList.toggle("collapsed", !open);
            document.body.classList.remove("tools-collapsed");
            return;
        }
        const collapsed = sidebar.classList.toggle("collapsed");
        document.body.classList.toggle("tools-collapsed", collapsed);
    });
    compactSidebarQuery.addEventListener("change", syncSidebarMode);
    syncSidebarMode();

    $("scheduleBtn").addEventListener("click", () => {
        closeCommandMenu();
        openMissionModal();
    });
    $("historyBtn").addEventListener("click", () => {
        closeCommandMenu();
        openHistoryModal();
    });
    $("navigationHistoryBtn").addEventListener("click", () => {
        closeCommandMenu();
        openNavigationHistoryModal();
    });
    $("clearTracks").addEventListener("click", () => {
        clearPlaybackData();
        resetMapStats();
        showToast("Visible traces were cleared.");
    });

    document.querySelectorAll("[data-close-modal]").forEach((button) => {
        button.addEventListener("click", () => closeModal(button.dataset.closeModal));
    });

    document.querySelectorAll("[data-open-log]").forEach((button) => {
        button.addEventListener("click", () => {
            closeCommandMenu();
            openLogViewer(button.dataset.openLog);
        });
    });

    document.querySelector("[data-open-csv]").addEventListener("click", () => {
        closeCommandMenu();
        openCsvViewer();
    });

    document.querySelector(".logout-form").addEventListener("submit", closeCommandMenu);

    document.querySelectorAll(".modal").forEach((modal) => {
        modal.addEventListener("click", (event) => {
            if (event.target === modal) closeModal(modal.id);
        });
    });
}

async function boot() {
    try {
        try { initMap(); }
        catch (error) { showToast(error.message, "error"); }
        bindShellControls();
        bindPlaybackControls();
        await refreshSystemStatus();
        await refreshPlannedRoute();
        await loadInitialMovements();
        const schedulePoll = (operation, baseDelay, maxDelay) => {
            let delay = baseDelay;
            const run = async () => {
                if (document.hidden) {
                    window.setTimeout(run, 10000);
                    return;
                }
                const ok = await operation();
                delay = ok === false ? Math.min(delay * 2, maxDelay) : baseDelay;
                window.setTimeout(run, delay);
            };
            window.setTimeout(run, baseDelay);
        };
        if (!isHistoryPage) {
            schedulePoll(refreshSystemStatus, 1000, 30000);
            schedulePoll(refreshPlannedRoute, 1500, 30000);
        }
    } catch (error) {
        showToast(error.message || "Dashboard initialization failed.", "error");
        console.error(error);
    }
}

boot();
