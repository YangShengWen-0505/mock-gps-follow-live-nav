export async function apiFetch(url, options = {}) {
    const { timeoutMs = 15000, signal, ...requestOptions } = options;
    const controller = new AbortController();
    const abort = () => controller.abort();
    signal?.addEventListener("abort", abort, { once: true });
    if (signal?.aborted) abort();
    const timer = setTimeout(abort, timeoutMs);
    try {
        const response = await fetch(url, {
            credentials: "same-origin", ...requestOptions, signal: controller.signal,
        });
        if (response.status === 401) {
            window.location.href = "/login";
            throw new Error("Your session has expired. Please sign in again.");
        }
        // Keep the timeout active through body download, not only headers.
        const body = [204, 205, 304].includes(response.status) ? null : await response.arrayBuffer();
        return new Response(body, { status: response.status, headers: response.headers });
    } catch (error) {
        if (controller.signal.aborted && !signal?.aborted) throw new Error("Request timed out. Please retry.");
        throw error;
    } finally {
        clearTimeout(timer);
        signal?.removeEventListener("abort", abort);
    }
}

export async function fetchJson(url, fallbackMessage, options = {}) {
    const response = await apiFetch(url, options);
    if (!response.ok) throw new Error(fallbackMessage || `Request failed with HTTP ${response.status}.`);
    return response.json();
}
