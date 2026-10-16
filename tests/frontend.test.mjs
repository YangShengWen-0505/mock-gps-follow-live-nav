import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import vm from 'node:vm';

async function load(name, mocks = {}, globals = {}) {
    const context = vm.createContext({ console, URL, AbortController, Response, setTimeout, clearTimeout, ...globals });
    const cache = new Map();
    async function moduleFor(file) {
        if (cache.has(file)) return cache.get(file);
        let module;
        if (mocks[file]) {
            module = new vm.SyntheticModule(Object.keys(mocks[file]), function () {
                for (const [key, value] of Object.entries(mocks[file])) this.setExport(key, value);
            }, { context });
        } else {
            module = new vm.SourceTextModule(await readFile(new URL(`../mock_gps/static/js/${file}`, import.meta.url), 'utf8'),
                { context, identifier: file });
        }
        cache.set(file, module);
        await module.link((specifier) => moduleFor(specifier.replace('./', '')));
        return module;
    }
    const module = await moduleFor(name);
    await module.evaluate();
    return module.namespace;
}

function dom() {
    const nodes = new Map();
    const $ = (id) => {
        if (!nodes.has(id)) nodes.set(id, {
            value: id === 'typeFilter' ? 'ALL' : '', checked: false, disabled: false,
            textContent: '', innerHTML: '', handlers: {},
            classList: { toggle() {}, add() {}, remove() {} },
            addEventListener(event, fn) { this.handlers[event] = fn; },
            querySelector() { return { disabled: false }; },
        });
        return nodes.get(id);
    };
    return { $, nodes };
}

async function playback(fetchJson, history = false) {
    const { $, nodes } = dom();
    const renders = [];
    const module = await load('playback.js', {
        'api.js': { fetchJson },
        'context.js': { contextLabel: () => 'test', dataUrls: { movements: '/movement' }, isHistoryPage: history },
        'ui.js': { $, extractTime: (s) => s, parseTimestamp: (s) => new Date(s), showToast() {} },
        'mapView.js': { renderRecords: (records) => renders.push(records.length),
            renderRecordsIncremental: (_records, count) => renders.push(count), updateMarker() {} },
    }, {
        document: { hidden: false, addEventListener() {} },
        window: { setTimeout: (fn) => fn() },
        setTimeout: () => 1, clearTimeout() {}, performance: { now: () => 0 },
    });
    return { module, renders, $, nodes };
}

const record = (sequence) => ({ sequence, lat: 25, lng: 121, time: '2026-01-01T00:00:00Z', action: 'Walking' });

test('clearing live traces preserves offset and does not reload old records', async () => {
    const urls = [];
    const { module, renders } = await playback(async (url) => {
        urls.push(url);
        return { stream_id: 'one', next_offset: 99, records: urls.length === 1 ? [record(1)] : [], has_more: false };
    });
    await module.fetchLiveMovements();
    module.clearPlaybackData();
    await module.fetchLiveMovements();
    assert.match(urls[1], /offset=99/);
    assert.equal(renders.at(-1), 0);
});

test('live polls do not overlap', async () => {
    let resolve;
    let count = 0;
    const { module } = await playback(() => { count++; return new Promise((done) => { resolve = done; }); });
    const pending = module.fetchLiveMovements();
    await module.fetchLiveMovements();
    assert.equal(count, 1);
    resolve({ stream_id: 'one', records: [], next_offset: 0, has_more: false });
    await pending;
});

test('live replay memory stays within its window', async () => {
    let offset = 0;
    const { module, renders } = await playback(async () => ({
        stream_id: 'one', next_offset: ++offset,
        records: Array.from({ length: 500 }, (_, i) => record(offset * 500 + i)), has_more: false,
    }));
    for (let i = 0; i < 50; i++) await module.fetchLiveMovements();
    assert.ok(Math.max(...renders) <= 20000);
});

test('history pages stop at 20000 records and the next window starts at the saved cursor', async () => {
    const urls = [];
    const { module, $, renders } = await playback(async (url) => {
        urls.push(url);
        const offset = Number(new URL(url, 'http://local').searchParams.get('offset'));
        return { stream_id: 'one', next_offset: offset + 500,
            records: Array.from({ length: 500 }, (_, i) => record(offset + i)), has_more: offset < 20500 };
    }, true);
    module.bindPlaybackControls();
    await module.loadInitialMovements();
    assert.equal(urls.length, 40);
    assert.equal($('historyLater').disabled, false);
    await $('historyLater').handlers.click();
    assert.match(urls[40], /offset=20000/);
    assert.ok(Math.max(...renders) <= 20000);
});

test('an old mission cursor is discarded when the stream changes', async () => {
    const urls = [];
    const { module } = await playback(async (url) => {
        urls.push(url);
        return { stream_id: urls.length === 1 ? 'one' : 'two', next_offset: 50,
            records: [record(1)], has_more: false };
    });
    await module.fetchLiveMovements();
    await module.fetchLiveMovements();
    assert.match(urls[2], /offset=0/);
});

test('API timeout aborts a stalled request', async () => {
    let aborted = false;
    const api = await load('api.js', {}, {
        window: { location: {} },
        fetch: (_url, { signal }) => new Promise((_resolve, reject) => {
            signal.addEventListener('abort', () => { aborted = true; reject(new Error('aborted')); });
        }),
    });
    await assert.rejects(api.apiFetch('/slow', { timeoutMs: 5 }), /timed out/);
    assert.equal(aborted, true);
});

test('API handles a bodyless 304 and redirects on 401', async () => {
    const window = { location: {} };
    let status = 304;
    const api = await load('api.js', {}, { window, fetch: async () => new Response(null, { status }) });
    assert.equal((await api.apiFetch('/route')).status, 304);
    status = 401;
    await assert.rejects(api.apiFetch('/route'), /expired/);
    assert.equal(window.location.href, '/login');
});

test('CSV viewer drops a previous stream offset after a mission switch', async () => {
    const { $ } = dom();
    const urls = [];
    const ui = { $, displayTimeZone: 'UTC', escapeHtml: String, formatDateTime: String,
        openModal() {}, renderError(_node, error) { throw new Error(error); } };
    const viewer = await load('dataViewer.js', {
        'ui.js': ui,
        'context.js': { contextLabel: () => 'test', dataUrls: { movements: '/movement' } },
        'api.js': { apiFetch() {}, fetchJson: async (url) => {
            urls.push(url);
            return { stream_id: urls.length === 1 ? 'one' : 'two', next_offset: 99, records: [], has_more: true };
        } },
    }, { document: { addEventListener() {} } });
    viewer.openCsvViewer();
    await new Promise(setImmediate);
    $('csvNext').handlers.click();
    await new Promise(setImmediate);
    assert.match(urls[1], /offset=99/);
    assert.match(urls[2], /offset=0/);
});

test('map falls back without MapLibre and bounds point markers', async () => {
    const { $ } = dom();
    $('showPoints').checked = true;
    let points = 0;
    let maximum = 0;
    let groupIndex = 0;
    const map = { setView() { return this; }, hasLayer() { return true; }, removeLayer() {},
        addLayer() {}, invalidateSize() {} };
    const layer = () => ({ addTo() { return this; }, clearLayers() {}, bringToFront() {},
        addLatLng() {}, setLatLngs() {}, setLatLng() { return this; }, setIcon() { return this; } });
    const L = { map: () => map, control: { zoom: layer }, tileLayer: layer, polyline: layer, marker: layer,
        divIcon: () => ({}), featureGroup: () => {
            const group = layer(); group.isPoints = ++groupIndex === 2;
            group.removeLayer = () => { points--; };
            return group;
        }, circleMarker: () => ({ bindTooltip() { return this; }, addTo(group) {
            if (group.isPoints) { points++; maximum = Math.max(maximum, points); } return this;
        } }) };
    const module = await load('mapView.js', {
        'ui.js': { $, escapeHtml: String, formatDateTime: String },
        'context.js': { pageContext: {} },
    }, { L, window: { addEventListener() {}, clearTimeout() {}, setTimeout() {} } });
    module.initMap();
    assert.equal($('mapLayerSelect').value, 'osmDark');
    module.renderRecordsIncremental(Array.from({ length: 6000 }, (_, i) => record(i)), 6000);
    assert.equal(points, 5000);
    assert.ok(maximum <= 5001);
});

test('history list requests the next page instead of hiding entries beyond 100', async () => {
    const { $ } = dom();
    const urls = [];
    const panels = await load('panels.js', {
        'api.js': { apiFetch() {}, fetchJson: async (url) => {
            urls.push(url);
            return Array.from({ length: 100 }, (_, i) => ({ date: '2026-10-16', session: `${i}`, id: `${i}` }));
        } },
        'context.js': { contextLabel: () => 'test', dataUrls: {}, isHistoryPage: false },
        'mapView.js': { updatePlannedRoute() {} },
        'ui.js': { $, escapeHtml: String, extractTime: String, formatDateTime: String,
            formatDuration: String, openModal() {}, renderEmpty() {}, renderError() {}, showToast() {} },
    });
    await panels.openHistoryModal();
    await $('historyNext').handlers.click();
    assert.match(urls[1], /offset=100/);
});

test('closing the data viewer prevents a stale response from rendering', async () => {
    const { $ } = dom();
    const events = {};
    let resolve;
    const viewer = await load('dataViewer.js', {
        'api.js': { apiFetch() {}, fetchJson: () => new Promise((done) => { resolve = done; }) },
        'context.js': { contextLabel: () => 'test', dataUrls: { movements: '/movement' } },
        'ui.js': { $, displayTimeZone: 'UTC', escapeHtml: String, formatDateTime: String,
            openModal() {}, renderError() {} },
    }, { document: { addEventListener: (event, fn) => { events[event] = fn; } } });
    viewer.openCsvViewer();
    events.modalclosed({ detail: 'data-modal' });
    $('data-body').innerHTML = 'new content';
    resolve({ stream_id: 'one', records: [], next_offset: 0 });
    await new Promise(setImmediate);
    assert.equal($('data-body').innerHTML, 'new content');
});
