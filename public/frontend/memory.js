const API = (() => {
    if (typeof window === "undefined") return "http://127.0.0.1:8000";
    if (window.KING_API_BASE) return window.KING_API_BASE;
    const params = new URLSearchParams(window.location.search);
    const apiParam = params.get("api");
    if (apiParam) {
        window.localStorage.setItem("KING_API_BASE", apiParam);
        return apiParam;
    }
    const stored = window.localStorage.getItem("KING_API_BASE");
    if (stored) return stored;
    const origin = new URL(window.location.origin);
    const localHost = origin.hostname === "localhost" || origin.hostname === "127.0.0.1";
    if (localHost && origin.port !== "8000") {
        return `${origin.protocol}//${origin.hostname}:8000`;
    }
    return window.location.origin || "http://127.0.0.1:8000";
})();

const state = {
    payload: null,
    page: "memory",
    filter: "active",
    canvasFilter: "active",
    view: "graph",
    memoryFilter: "all",
    search: "",
    canvasSearch: "",
    selected: null,
    canvas: {
        scale: 1,
        panX: 0,
        panY: 0,
        dragging: false,
        lastX: 0,
        lastY: 0,
        fitted: false,
    },
};

const $ = id => document.getElementById(id);
const els = {
    connection: $("connection-state"),
    connectionLabel: $("connection-label"),
    modeSlider: $("mode-slider"),
    memoryPage: $("memory-page"),
    canvasPage: $("canvas-page"),
    refreshBtn: $("refresh-btn"),
    exportBtn: $("export-btn"),
    metricGrid: $("metric-grid"),
    memoryInput: $("memory-input"),
    importanceInput: $("importance-input"),
    importanceValue: $("importance-value"),
    rememberBtn: $("remember-btn"),
    forgetInput: $("forget-input"),
    forgetBtn: $("forget-btn"),
    reflectLabel: $("reflect-label"),
    reflectBtn: $("reflect-btn"),
    writeStatus: $("write-status"),
    forgetStatus: $("forget-status"),
    reflectStatus: $("reflect-status"),
    graphSearch: $("graph-search"),
    canvasSearch: $("canvas-search"),
    graphSummary: $("graph-summary"),
    graphView: $("graph-view"),
    tableView: $("table-view"),
    graphSvg: $("graph-svg"),
    graphEmpty: $("graph-empty"),
    edgeTable: $("edge-table"),
    memoryList: $("memory-list"),
    inspectorBody: $("inspector-body"),
    selectionKind: $("selection-kind"),
    relationList: $("relation-list"),
    relationCount: $("relation-count"),
    timeline: $("timeline"),
    activityCount: $("activity-count"),
    toastStack: $("toast-stack"),
    canvasStage: $("canvas-stage"),
    canvasSvg: $("canvas-svg"),
    canvasEmpty: $("canvas-empty"),
    canvasFitBtn: $("canvas-fit-btn"),
    canvasZoomInBtn: $("canvas-zoom-in-btn"),
    canvasZoomOutBtn: $("canvas-zoom-out-btn"),
    canvasNodeCount: $("canvas-node-count"),
    canvasEdgeCount: $("canvas-edge-count"),
    canvasZoomLabel: $("canvas-zoom-label"),
};

function escapeHtml(value) {
    return String(value ?? "")
        .split("&").join("&amp;")
        .split("<").join("&lt;")
        .split(">").join("&gt;")
        .split('"').join("&quot;")
        .split("'").join("&#39;");
}

function clean(value) {
    return String(value ?? "").trim();
}

function lower(value) {
    return clean(value).toLowerCase();
}

function formatNumber(value, fallback = "0") {
    if (value === null || value === undefined || value === "") return fallback;
    const parsed = Number(value);
    if (!Number.isFinite(parsed)) return fallback;
    if (Math.abs(parsed) >= 100) return String(Math.round(parsed));
    return parsed.toFixed(2).replace(".00", "");
}

function formatDate(value) {
    const text = clean(value);
    if (!text) return "unknown";
    const date = new Date(text);
    if (Number.isNaN(date.getTime())) return text;
    return date.toLocaleString([], { month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}

function relationLabel(value) {
    return clean(value).split("_").join(" ");
}

function nodeMap() {
    const nodes = state.payload && Array.isArray(state.payload.nodes) ? state.payload.nodes : [];
    const map = new Map();
    for (const node of nodes) {
        map.set(clean(node.id), node);
    }
    return map;
}

function edgeMemoryIds() {
    const ids = new Set();
    for (const edge of allEdges()) {
        const id = clean(edge.memory_id);
        if (id) ids.add(id);
    }
    return ids;
}

function allNodes() {
    return state.payload && Array.isArray(state.payload.nodes) ? state.payload.nodes : [];
}

function allEdges() {
    return state.payload && Array.isArray(state.payload.edges) ? state.payload.edges : [];
}

function allMemories() {
    return state.payload && Array.isArray(state.payload.memories) ? state.payload.memories : [];
}

function textMatchesSearch(text) {
    const query = lower(state.search);
    if (!query) return true;
    return lower(text).includes(query);
}

function edgeText(edge) {
    return [
        edge.source_name,
        relationLabel(edge.relation),
        edge.target_name,
        edge.evidence,
        edge.tier,
        edge.mode,
    ].map(clean).join(" ");
}

function filteredEdges() {
    return allEdges().filter(edge => {
        const isActive = edge.active !== false;
        if (state.filter === "active" && !isActive) return false;
        if (state.filter === "inactive" && isActive) return false;
        return textMatchesSearch(edgeText(edge));
    });
}

function canvasFilteredEdges() {
    return allEdges().filter(edge => {
        const isActive = edge.active !== false;
        if (state.canvasFilter === "active" && !isActive) return false;
        if (state.canvasFilter === "inactive" && isActive) return false;
        const query = lower(state.canvasSearch);
        if (!query) return true;
        return lower(edgeText(edge)).includes(query);
    });
}

function canvasFilteredNodes(edges) {
    const ids = new Set();
    for (const edge of edges) {
        ids.add(clean(edge.source));
        ids.add(clean(edge.target));
    }
    const query = lower(state.canvasSearch);
    const nodes = allNodes().filter(node => {
        const text = lower([node.name, node.type, (node.aliases || []).join(" ")].join(" "));
        return ids.has(clean(node.id)) || (query && text.includes(query));
    });
    if (edges.length > 0) return nodes;
    if (!query) return allNodes();
    return nodes;
}

function filteredNodes(edges) {
    const ids = new Set();
    for (const edge of edges) {
        ids.add(clean(edge.source));
        ids.add(clean(edge.target));
    }
    const nodes = allNodes().filter(node => ids.has(clean(node.id)) || textMatchesSearch([node.name, node.type, (node.aliases || []).join(" ")].join(" ")));
    if (edges.length > 0) return nodes;
    return nodes.filter(node => textMatchesSearch([node.name, node.type, (node.aliases || []).join(" ")].join(" ")));
}

function setConnection(kind, label) {
    els.connection.classList.remove("online", "offline");
    if (kind) els.connection.classList.add(kind);
    els.connectionLabel.textContent = label;
}

function setStatus(el, text) {
    if (el) el.textContent = text;
}

function toast(message, kind = "") {
    const item = document.createElement("div");
    item.className = `toast ${kind}`.trim();
    item.textContent = message;
    els.toastStack.appendChild(item);
    window.setTimeout(() => item.remove(), 4200);
}

async function api(path, options = {}) {
    const response = await fetch(`${API}${path}`, {
        headers: { "Content-Type": "application/json" },
        ...options,
    });
    if (!response.ok) {
        const text = await response.text().catch(() => "");
        throw new Error(text || `Request failed with ${response.status}`);
    }
    return response.json();
}

async function loadGraph() {
    setConnection("", "Loading");
    try {
        state.payload = await api("/memory/graph");
        state.canvas.fitted = false;
        setConnection("online", "Live");
        render();
    } catch (error) {
        setConnection("offline", "Offline");
        toast(`Memory API unavailable: ${error.message}`, "error");
    }
}

async function rememberMemory() {
    const text = clean(els.memoryInput.value);
    if (!text) {
        toast("Memory text is empty.", "error");
        return;
    }
    setStatus(els.writeStatus, "Writing");
    try {
        const result = await api("/memory/remember", {
            method: "POST",
            body: JSON.stringify({ text, importance: Number(els.importanceInput.value) }),
        });
        state.payload = result.memory;
        state.canvas.fitted = false;
        setStatus(els.writeStatus, result.result && result.result.status ? result.result.status : "Done");
        els.memoryInput.value = "";
        toast("Memory store updated.");
        render();
    } catch (error) {
        setStatus(els.writeStatus, "Error");
        toast(`Remember failed: ${error.message}`, "error");
    }
}

async function forgetMemory(queryOverride) {
    const query = clean(queryOverride || els.forgetInput.value);
    if (!query) {
        toast("Forget query is empty.", "error");
        return;
    }
    setStatus(els.forgetStatus, "Removing");
    try {
        const result = await api("/memory/forget", {
            method: "POST",
            body: JSON.stringify({ query }),
        });
        state.payload = result.memory;
        state.canvas.fitted = false;
        const status = result.result && result.result.status ? result.result.status : "Done";
        setStatus(els.forgetStatus, status);
        els.forgetInput.value = "";
        state.selected = null;
        toast(`Forget result: ${status}`);
        render();
    } catch (error) {
        setStatus(els.forgetStatus, "Error");
        toast(`Forget failed: ${error.message}`, "error");
    }
}

async function reflectMemory() {
    const label = clean(els.reflectLabel.value) || "frontend";
    setStatus(els.reflectStatus, "Writing");
    try {
        const result = await api("/memory/reflect", {
            method: "POST",
            body: JSON.stringify({ label }),
        });
        state.payload = result.memory;
        state.canvas.fitted = false;
        setStatus(els.reflectStatus, "Done");
        toast("Reflection recorded.");
        render();
    } catch (error) {
        setStatus(els.reflectStatus, "Error");
        toast(`Reflect failed: ${error.message}`, "error");
    }
}

function render() {
    renderMetrics();
    renderSummary();
    renderGraph();
    renderCanvas();
    renderTable();
    renderMemories();
    renderRelations();
    renderTimeline();
    renderInspector();
}

function setPage(page) {
    state.page = page;
    els.memoryPage.classList.toggle("active", page === "memory");
    els.canvasPage.classList.toggle("active", page === "canvas");
    els.modeSlider.classList.toggle("right", page === "canvas");
    document.querySelectorAll("[data-page]").forEach(button => {
        button.classList.toggle("active", button.getAttribute("data-page") === page);
    });
    if (page === "canvas") {
        window.setTimeout(() => {
            if (!state.canvas.fitted) fitCanvas();
            renderCanvas();
        }, 40);
    }
}

function renderMetrics() {
    const assessment = state.payload && state.payload.assessment ? state.payload.assessment : {};
    const graph = assessment.graph || {};
    const metrics = [
        ["Nodes", graph.node_count, "entities"],
        ["Edges", graph.active_edge_count, `${graph.edge_count || 0} total`],
        ["Memories", assessment.entry_count, `${assessment.daily_file_count || 0} daily files`],
        ["Rules", graph.relation_rule_count, "markdown-owned"],
        ["Index", formatNumber((assessment.index_coverage_ratio || 0) * 100), "coverage percent"],
        ["Capacity", assessment.capacity_remaining, `${assessment.capacity_limit || 0} max`],
    ];
    els.metricGrid.innerHTML = metrics.map(([label, value, sub]) => `
        <div class="metric">
            <div class="metric-label">${escapeHtml(label)}</div>
            <div class="metric-value">${escapeHtml(formatNumber(value))}</div>
            <div class="metric-sub">${escapeHtml(sub)}</div>
        </div>
    `).join("");
}

function renderSummary() {
    const edges = filteredEdges();
    const active = edges.filter(edge => edge.active !== false).length;
    const inactive = edges.length - active;
    const nodes = filteredNodes(edges).length;
    const summary = state.payload && state.payload.summary ? state.payload.summary : "";
    const pills = [
        `${nodes} visible nodes`,
        `${edges.length} visible edges`,
        `${active} active`,
        `${inactive} inactive`,
    ];
    if (summary) pills.push(summary);
    els.graphSummary.innerHTML = pills.map(pill => `<span class="summary-pill">${escapeHtml(pill)}</span>`).join("");
}

function renderGraph() {
    els.graphView.classList.toggle("active", state.view === "graph");
    els.tableView.classList.toggle("active", state.view === "table");
    if (state.view !== "graph") return;

    const edges = filteredEdges();
    const nodes = filteredNodes(edges);
    els.graphEmpty.classList.toggle("visible", edges.length === 0 && nodes.length === 0);
    const width = Math.max(720, els.graphView.clientWidth || 720);
    const height = Math.max(360, els.graphView.clientHeight || 420);
    els.graphSvg.setAttribute("viewBox", `0 0 ${width} ${height}`);
    els.graphSvg.replaceChildren();
    if (nodes.length === 0) return;

    const positions = layoutNodes(nodes, edges, width, height);
    const edgeLayer = document.createElementNS("http://www.w3.org/2000/svg", "g");
    const nodeLayer = document.createElementNS("http://www.w3.org/2000/svg", "g");
    els.graphSvg.appendChild(edgeLayer);
    els.graphSvg.appendChild(nodeLayer);

    for (const edge of edges) {
        const from = positions.get(clean(edge.source));
        const to = positions.get(clean(edge.target));
        if (!from || !to) continue;
        const isSelf = clean(edge.source) === clean(edge.target);
        let hit;
        if (isSelf) {
            const loop = svgEl("path", {
                d: `M ${from.x - 28} ${from.y - 30} C ${from.x - 70} ${from.y - 78}, ${from.x + 70} ${from.y - 78}, ${from.x + 28} ${from.y - 30}`,
                class: `edge-line ${edge.active === false ? "inactive" : ""}`.trim(),
            });
            edgeLayer.appendChild(loop);
            hit = svgEl("path", {
                d: loop.getAttribute("d"),
                stroke: "transparent",
                "stroke-width": 18,
                class: "graph-hit",
            });
        } else {
            const line = svgEl("line", {
                x1: from.x,
                y1: from.y,
                x2: to.x,
                y2: to.y,
                class: `edge-line ${edge.active === false ? "inactive" : ""}`.trim(),
            });
            edgeLayer.appendChild(line);
            const distance = Math.sqrt((to.x - from.x) * (to.x - from.x) + (to.y - from.y) * (to.y - from.y));
            if (distance > 190) {
                const label = svgEl("text", {
                    x: from.x + (to.x - from.x) * 0.58,
                    y: from.y + (to.y - from.y) * 0.58 - 10,
                    class: "edge-label",
                    "text-anchor": "middle",
                });
                label.textContent = relationLabel(edge.relation);
                edgeLayer.appendChild(label);
            }
            hit = svgEl("line", {
                x1: from.x,
                y1: from.y,
                x2: to.x,
                y2: to.y,
                stroke: "transparent",
                "stroke-width": 18,
                class: "graph-hit",
            });
        }
        hit.addEventListener("click", () => selectItem("edge", edge));
        edgeLayer.appendChild(hit);
    }

    for (const node of nodes) {
        const point = positions.get(clean(node.id));
        if (!point) continue;
        const group = svgEl("g", { class: "graph-hit" });
        const radius = nodeRadius(node, edges);
        const selected = state.selected && state.selected.type === "node" && clean(state.selected.item.id) === clean(node.id);
        group.appendChild(svgEl("circle", {
            cx: point.x,
            cy: point.y,
            r: radius,
            class: `node-ring ${clean(node.type)} ${selected ? "selected" : ""}`.trim(),
        }));
        const title = svgEl("text", {
            x: point.x,
            y: point.y + 4,
            class: "node-label",
            "text-anchor": "middle",
        });
        title.textContent = initials(clean(node.name) || clean(node.id));
        group.appendChild(title);
        const name = svgEl("text", {
            x: point.x,
            y: point.y + radius + 14,
            class: "node-name-label",
            "text-anchor": "middle",
        });
        name.textContent = trimMiddle(clean(node.name) || clean(node.id), 12);
        group.appendChild(name);
        const type = svgEl("text", {
            x: point.x,
            y: point.y + radius + 28,
            class: "node-type-label",
            "text-anchor": "middle",
        });
        type.textContent = clean(node.type) || "concept";
        group.appendChild(type);
        group.addEventListener("click", () => selectItem("node", node));
        nodeLayer.appendChild(group);
    }
}

function renderCanvas() {
    if (!els.canvasSvg || state.page !== "canvas") return;
    const edges = canvasFilteredEdges();
    const nodes = canvasFilteredNodes(edges);
    els.canvasEmpty.classList.toggle("visible", edges.length === 0 && nodes.length === 0);
    els.canvasNodeCount.textContent = `${nodes.length} nodes`;
    els.canvasEdgeCount.textContent = `${edges.length} edges`;
    els.canvasZoomLabel.textContent = `${Math.round(state.canvas.scale * 100)}%`;

    const width = Math.max(320, els.canvasStage.clientWidth || 960);
    const height = Math.max(320, els.canvasStage.clientHeight || 620);
    els.canvasSvg.setAttribute("viewBox", `0 0 ${width} ${height}`);
    els.canvasSvg.replaceChildren();
    if (nodes.length === 0) return;

    const positions = canvasLayoutNodes(nodes, edges);
    if (!state.canvas.fitted) fitCanvas(positions, width, height);

    const root = svgEl("g", {
        transform: `translate(${width / 2 + state.canvas.panX} ${height / 2 + state.canvas.panY}) scale(${state.canvas.scale})`,
    });
    const edgeLayer = svgEl("g", {});
    const nodeLayer = svgEl("g", {});
    root.appendChild(edgeLayer);
    root.appendChild(nodeLayer);
    els.canvasSvg.appendChild(root);

    for (const edge of edges) {
        const from = positions.get(clean(edge.source));
        const to = positions.get(clean(edge.target));
        if (!from || !to) continue;
        const isSelf = clean(edge.source) === clean(edge.target);
        let hit;
        if (isSelf) {
            const path = `M ${from.x - 52} ${from.y - 50} C ${from.x - 128} ${from.y - 138}, ${from.x + 128} ${from.y - 138}, ${from.x + 52} ${from.y - 50}`;
            edgeLayer.appendChild(svgEl("path", {
                d: path,
                class: `canvas-edge-line ${edge.active === false ? "inactive" : ""}`.trim(),
            }));
            hit = svgEl("path", {
                d: path,
                stroke: "transparent",
                "stroke-width": 32,
                class: "graph-hit",
            });
        } else {
            edgeLayer.appendChild(svgEl("line", {
                x1: from.x,
                y1: from.y,
                x2: to.x,
                y2: to.y,
                class: `canvas-edge-line ${edge.active === false ? "inactive" : ""}`.trim(),
            }));
            const label = svgEl("text", {
                x: from.x + (to.x - from.x) * 0.56,
                y: from.y + (to.y - from.y) * 0.56 - 14,
                class: "canvas-edge-label",
                "text-anchor": "middle",
            });
            label.textContent = relationLabel(edge.relation);
            edgeLayer.appendChild(label);
            hit = svgEl("line", {
                x1: from.x,
                y1: from.y,
                x2: to.x,
                y2: to.y,
                stroke: "transparent",
                "stroke-width": 34,
                class: "graph-hit",
            });
        }
        hit.addEventListener("click", event => {
            event.stopPropagation();
            selectItem("edge", edge);
        });
        edgeLayer.appendChild(hit);
    }

    for (const node of nodes) {
        const point = positions.get(clean(node.id));
        if (!point) continue;
        const group = svgEl("g", { class: "graph-hit" });
        const radius = canvasNodeRadius(node, edges);
        const selected = state.selected && state.selected.type === "node" && clean(state.selected.item.id) === clean(node.id);
        group.appendChild(svgEl("circle", {
            cx: point.x,
            cy: point.y,
            r: radius,
            class: `canvas-node-ring ${clean(node.type)} ${selected ? "selected" : ""}`.trim(),
        }));
        const label = svgEl("text", {
            x: point.x,
            y: point.y + 7,
            class: "canvas-node-label",
            "text-anchor": "middle",
        });
        label.textContent = initials(clean(node.name) || clean(node.id));
        group.appendChild(label);
        const labelAbove = point.y < -30;
        const nameY = labelAbove ? point.y - radius - 20 : point.y + radius + 22;
        const typeY = labelAbove ? point.y - radius - 4 : point.y + radius + 40;
        const name = svgEl("text", {
            x: point.x,
            y: nameY,
            class: "canvas-node-name",
            "text-anchor": "middle",
        });
        name.textContent = trimMiddle(clean(node.name) || clean(node.id), 18);
        group.appendChild(name);
        const type = svgEl("text", {
            x: point.x,
            y: typeY,
            class: "canvas-node-type",
            "text-anchor": "middle",
        });
        type.textContent = clean(node.type) || "concept";
        group.appendChild(type);
        group.addEventListener("click", event => {
            event.stopPropagation();
            selectItem("node", node);
        });
        nodeLayer.appendChild(group);
    }
}

function svgEl(name, attrs) {
    const el = document.createElementNS("http://www.w3.org/2000/svg", name);
    for (const [key, value] of Object.entries(attrs || {})) {
        el.setAttribute(key, value);
    }
    return el;
}

function nodeRadius(node, edges) {
    const id = clean(node.id);
    let degree = 0;
    for (const edge of edges) {
        if (clean(edge.source) === id || clean(edge.target) === id) degree += 1;
    }
    return Math.min(52, 30 + degree * 4);
}

function canvasNodeRadius(node, edges) {
    const id = clean(node.id);
    let degree = 0;
    for (const edge of edges) {
        if (clean(edge.source) === id || clean(edge.target) === id) degree += 1;
    }
    return Math.min(74, 44 + degree * 5);
}

function layoutNodes(nodes, edges, width, height) {
    const ids = nodes.map(node => clean(node.id));
    const positions = new Map();
    const centerX = width / 2;
    const centerY = height / 2;
    const radius = Math.max(90, Math.min(width, height) * 0.34);
    const userIndex = ids.indexOf("user");
    const orbitIds = ids.filter(id => id !== "user");

    ids.forEach((id) => {
        if (id === "user") {
            positions.set(id, { x: centerX, y: centerY });
            return;
        }
        const count = Math.max(1, orbitIds.length || ids.length);
        const orbitIndex = orbitIds.indexOf(id);
        let angle = (-Math.PI / 2) + (Math.PI * 2 * orbitIndex / count);
        if (count === 1) angle = 0;
        if (count === 2) angle = orbitIndex === 0 ? Math.PI : 0;
        positions.set(id, {
            x: centerX + Math.cos(angle) * radius,
            y: centerY + Math.sin(angle) * radius,
        });
    });

    const links = edges
        .map(edge => [clean(edge.source), clean(edge.target)])
        .filter(pair => pair[0] !== pair[1] && positions.has(pair[0]) && positions.has(pair[1]));
    for (let step = 0; step < 80; step += 1) {
        for (const idA of ids) {
            if (idA === "user") continue;
            const a = positions.get(idA);
            if (!a) continue;
            for (const idB of ids) {
                if (idA === idB) continue;
                const b = positions.get(idB);
                if (!b) continue;
                const dx = a.x - b.x;
                const dy = a.y - b.y;
                const distance = Math.max(20, Math.sqrt(dx * dx + dy * dy));
                const push = 18 / distance;
                a.x += (dx / distance) * push;
                a.y += (dy / distance) * push;
            }
        }
        for (const [source, target] of links) {
            const a = positions.get(source);
            const b = positions.get(target);
            if (!a || !b) continue;
            const dx = b.x - a.x;
            const dy = b.y - a.y;
                if (source !== "user") {
                    a.x += dx * 0.003;
                    a.y += dy * 0.003;
                }
                if (target !== "user") {
                    b.x -= dx * 0.003;
                    b.y -= dy * 0.003;
                }
        }
        for (const [id, point] of positions.entries()) {
            if (id === "user") continue;
            point.x = Math.max(64, Math.min(width - 64, point.x));
            point.y = Math.max(58, Math.min(height - 58, point.y));
        }
    }
    return positions;
}

function canvasLayoutNodes(nodes, edges) {
    const ids = nodes.map(node => clean(node.id));
    const positions = new Map();
    const orbitIds = ids.filter(id => id !== "user");
    const radius = Math.max(280, Math.min(780, 210 + orbitIds.length * 28));

    ids.forEach(id => {
        if (id === "user") {
            positions.set(id, { x: 0, y: 0 });
            return;
        }
        const count = Math.max(1, orbitIds.length || ids.length);
        const orbitIndex = orbitIds.indexOf(id);
        let angle = (-Math.PI / 2) + (Math.PI * 2 * orbitIndex / count);
        if (count === 1) angle = 0;
        if (count === 2) angle = orbitIndex === 0 ? Math.PI : 0;
        positions.set(id, {
            x: Math.cos(angle) * radius,
            y: Math.sin(angle) * radius,
        });
    });

    const links = edges
        .map(edge => [clean(edge.source), clean(edge.target)])
        .filter(pair => pair[0] !== pair[1] && positions.has(pair[0]) && positions.has(pair[1]));

    for (let step = 0; step < 90; step += 1) {
        for (const idA of ids) {
            if (idA === "user") continue;
            const a = positions.get(idA);
            if (!a) continue;
            for (const idB of ids) {
                if (idA === idB) continue;
                const b = positions.get(idB);
                if (!b) continue;
                const dx = a.x - b.x;
                const dy = a.y - b.y;
                const distance = Math.max(40, Math.sqrt(dx * dx + dy * dy));
                const push = 42 / distance;
                a.x += (dx / distance) * push;
                a.y += (dy / distance) * push;
            }
        }
        for (const [source, target] of links) {
            const a = positions.get(source);
            const b = positions.get(target);
            if (!a || !b) continue;
            const dx = b.x - a.x;
            const dy = b.y - a.y;
            if (source !== "user") {
                a.x += dx * 0.002;
                a.y += dy * 0.002;
            }
            if (target !== "user") {
                b.x -= dx * 0.002;
                b.y -= dy * 0.002;
            }
        }
    }
    return positions;
}

function canvasBounds(positions) {
    const points = Array.from(positions.values());
    if (points.length === 0) return { minX: -100, maxX: 100, minY: -100, maxY: 100 };
    let minX = points[0].x;
    let maxX = points[0].x;
    let minY = points[0].y;
    let maxY = points[0].y;
    for (const point of points) {
        minX = Math.min(minX, point.x);
        maxX = Math.max(maxX, point.x);
        minY = Math.min(minY, point.y);
        maxY = Math.max(maxY, point.y);
    }
    return { minX, maxX, minY, maxY };
}

function fitCanvas(providedPositions, providedWidth, providedHeight) {
    if (!els.canvasStage) return;
    const edges = canvasFilteredEdges();
    const nodes = canvasFilteredNodes(edges);
    const positions = providedPositions || canvasLayoutNodes(nodes, edges);
    const width = providedWidth || Math.max(320, els.canvasStage.clientWidth || 960);
    const height = providedHeight || Math.max(320, els.canvasStage.clientHeight || 620);
    const bounds = canvasBounds(positions);
    const graphWidth = Math.max(220, bounds.maxX - bounds.minX + 260);
    const graphHeight = Math.max(220, bounds.maxY - bounds.minY + 260);
    const scale = Math.max(0.28, Math.min(1.35, Math.min(width / graphWidth, height / graphHeight)));
    state.canvas.scale = scale;
    state.canvas.panX = -((bounds.minX + bounds.maxX) / 2) * scale;
    state.canvas.panY = -((bounds.minY + bounds.maxY) / 2) * scale;
    state.canvas.fitted = true;
    renderCanvas();
}

function zoomCanvas(multiplier, originX, originY) {
    if (!els.canvasStage) return;
    const rect = els.canvasStage.getBoundingClientRect();
    const stageX = originX === undefined ? rect.width / 2 : originX - rect.left;
    const stageY = originY === undefined ? rect.height / 2 : originY - rect.top;
    const centerX = rect.width / 2;
    const centerY = rect.height / 2;
    const beforeScale = state.canvas.scale;
    const afterScale = Math.max(0.22, Math.min(2.8, beforeScale * multiplier));
    const worldX = (stageX - centerX - state.canvas.panX) / beforeScale;
    const worldY = (stageY - centerY - state.canvas.panY) / beforeScale;
    state.canvas.scale = afterScale;
    state.canvas.panX = stageX - centerX - worldX * afterScale;
    state.canvas.panY = stageY - centerY - worldY * afterScale;
    state.canvas.fitted = true;
    renderCanvas();
}

function renderTable() {
    const edges = filteredEdges();
    const rows = edges.map(edge => `
        <div class="edge-row" data-edge-id="${escapeHtml(edge.id)}">
            <span>${escapeHtml(edge.source_name)}</span>
            <span>${escapeHtml(relationLabel(edge.relation))}</span>
            <span>${escapeHtml(edge.target_name)}</span>
            <span>${edge.active === false ? "inactive" : "active"}</span>
            <span>${escapeHtml(formatNumber(edge.strength))}</span>
        </div>
    `).join("");
    els.edgeTable.innerHTML = `
        <div class="edge-row header">
            <span>Source</span>
            <span>Relation</span>
            <span>Target</span>
            <span>Status</span>
            <span>Strength</span>
        </div>
        ${rows || '<div class="edge-row"><span>No matching edges</span><span></span><span></span><span></span><span></span></div>'}
    `;
    for (const row of Array.from(els.edgeTable.querySelectorAll("[data-edge-id]"))) {
        const edge = allEdges().find(item => clean(item.id) === row.getAttribute("data-edge-id"));
        row.addEventListener("click", () => selectItem("edge", edge));
    }
}

function renderMemories() {
    const graphIds = edgeMemoryIds();
    const edgeEvidence = new Set(allEdges().map(edge => lower(edge.evidence)).filter(Boolean));
    const memories = allMemories().filter(memory => {
        const id = clean(memory.id);
        const text = lower(memory.text);
        const graphBacked = graphIds.has(id) || edgeEvidence.has(text);
        if (state.memoryFilter === "graph" && !graphBacked) return false;
        if (state.memoryFilter === "plain" && graphBacked) return false;
        return textMatchesSearch([memory.text, memory.date, memory.time, memory.tier].join(" "));
    });
    els.memoryList.innerHTML = memories.map(memory => {
        const graphBacked = graphIds.has(clean(memory.id)) || edgeEvidence.has(lower(memory.text));
        return `
            <article class="memory-item" data-memory-id="${escapeHtml(memory.id || memory.index)}">
                <div class="memory-text">${escapeHtml(memory.text)}</div>
                <div class="memory-meta">
                    <span>${escapeHtml(memory.date || "undated")}</span>
                    <span>${escapeHtml(memory.time || "")}</span>
                    <span>${escapeHtml(formatNumber(memory.importance))}</span>
                    <span>${graphBacked ? "graph" : "text"}</span>
                </div>
            </article>
        `;
    }).join("") || '<div class="placeholder">No memories match.</div>';
    for (const item of Array.from(els.memoryList.querySelectorAll("[data-memory-id]"))) {
        const id = item.getAttribute("data-memory-id");
        const memory = allMemories().find(entry => clean(entry.id || entry.index) === id);
        item.addEventListener("click", () => selectItem("memory", memory));
    }
}

function renderRelations() {
    const counts = new Map();
    for (const edge of filteredEdges()) {
        const name = relationLabel(edge.relation) || "related";
        counts.set(name, (counts.get(name) || 0) + 1);
    }
    const entries = Array.from(counts.entries()).sort((a, b) => b[1] - a[1]);
    const max = Math.max(1, ...entries.map(entry => entry[1]));
    els.relationCount.textContent = String(entries.length);
    els.relationList.innerHTML = entries.map(([name, count]) => `
        <div class="relation-item">
            <div class="relation-row">
                <span class="relation-name">${escapeHtml(name)}</span>
                <span class="relation-meta">${count}</span>
            </div>
            <div class="relation-bar"><span style="width: ${Math.max(8, (count / max) * 100)}%"></span></div>
        </div>
    `).join("") || '<div class="placeholder">No relations.</div>';
}

function renderTimeline() {
    const items = [
        ...allEdges().map(edge => ({
            kind: "edge",
            title: `${edge.source_name} ${relationLabel(edge.relation)} ${edge.target_name}`,
            time: edge.updated_at || edge.created_at,
            meta: edge.active === false ? "inactive edge" : "active edge",
        })),
        ...((state.payload && state.payload.reflections) || []).map(reflection => ({
            kind: "reflection",
            title: reflection.summary || reflection.label || "Reflection",
            time: reflection.created_at,
            meta: reflection.label || "reflection",
        })),
    ].sort((a, b) => clean(b.time).localeCompare(clean(a.time))).slice(0, 12);
    els.activityCount.textContent = String(items.length);
    els.timeline.innerHTML = items.map(item => `
        <div class="timeline-item">
            <div class="timeline-title">${escapeHtml(item.title)}</div>
            <div class="timeline-meta">
                <span>${escapeHtml(formatDate(item.time))}</span>
                <span>${escapeHtml(item.meta)}</span>
            </div>
        </div>
    `).join("") || '<div class="placeholder">No graph activity.</div>';
}

function renderInspector() {
    if (!state.selected || !state.selected.item) {
        els.selectionKind.textContent = "None";
        els.inspectorBody.innerHTML = '<div class="placeholder">Select a node, edge, or memory.</div>';
        return;
    }
    if (state.selected.type === "node") return renderNodeInspector(state.selected.item);
    if (state.selected.type === "edge") return renderEdgeInspector(state.selected.item);
    if (state.selected.type === "memory") return renderMemoryInspector(state.selected.item);
}

function renderNodeInspector(node) {
    els.selectionKind.textContent = "Node";
    const id = clean(node.id);
    const edges = allEdges().filter(edge => clean(edge.source) === id || clean(edge.target) === id);
    els.inspectorBody.innerHTML = `
        <h3 class="detail-title">${escapeHtml(node.name || node.id)}</h3>
        <div class="detail-subtitle">${escapeHtml((node.aliases || []).join(", ") || node.type || "concept")}</div>
        <div class="detail-grid">
            ${detailCell("ID", node.id)}
            ${detailCell("Type", node.type)}
            ${detailCell("Importance", formatNumber(node.importance))}
            ${detailCell("Degree", edges.length)}
            ${detailCell("Created", formatDate(node.created_at))}
            ${detailCell("Updated", formatDate(node.updated_at))}
        </div>
        <div class="evidence-box">${escapeHtml(edges.map(edge => `${edge.source_name} ${relationLabel(edge.relation)} ${edge.target_name}`).join(" | ") || "No connected edges.")}</div>
    `;
}

function renderEdgeInspector(edge) {
    els.selectionKind.textContent = "Edge";
    els.inspectorBody.innerHTML = `
        <h3 class="detail-title">${escapeHtml(edge.source_name)} ${escapeHtml(relationLabel(edge.relation))} ${escapeHtml(edge.target_name)}</h3>
        <div class="detail-subtitle">${edge.active === false ? "Inactive" : "Active"} ${escapeHtml(edge.tier || "semantic")} edge</div>
        <div class="detail-grid">
            ${detailCell("Relation", relationLabel(edge.relation))}
            ${detailCell("Mode", edge.mode)}
            ${detailCell("Strength", formatNumber(edge.strength))}
            ${detailCell("Confidence", formatNumber(edge.confidence))}
            ${detailCell("Created", formatDate(edge.created_at))}
            ${detailCell("Updated", formatDate(edge.updated_at))}
            ${detailCell("Valid From", formatDate(edge.valid_from))}
            ${detailCell("Valid To", formatDate(edge.valid_to))}
            ${detailCell("Memory ID", edge.memory_id || "unknown")}
            ${detailCell("Inactive Reason", edge.inactive_reason || "none")}
        </div>
        <div class="evidence-box">${escapeHtml(edge.evidence || "No evidence text recorded.")}</div>
    `;
}

function renderMemoryInspector(memory) {
    els.selectionKind.textContent = "Memory";
    const connected = allEdges().filter(edge => clean(edge.memory_id) === clean(memory.id) || lower(edge.evidence) === lower(memory.text));
    els.inspectorBody.innerHTML = `
        <h3 class="detail-title">Memory ${escapeHtml(memory.index || "")}</h3>
        <div class="detail-subtitle">${escapeHtml(memory.text)}</div>
        <div class="detail-grid">
            ${detailCell("ID", memory.id || "unknown")}
            ${detailCell("Tier", memory.tier || "semantic")}
            ${detailCell("Importance", formatNumber(memory.importance))}
            ${detailCell("Graph Edges", connected.length)}
            ${detailCell("Date", memory.date || "unknown")}
            ${detailCell("Time", memory.time || "unknown")}
        </div>
        <div class="evidence-box">${escapeHtml(connected.map(edge => `${edge.source_name} ${relationLabel(edge.relation)} ${edge.target_name}`).join(" | ") || "Text-only memory.")}</div>
        <button class="danger-btn" id="forget-selected-btn">
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 6h18"/><path d="M8 6V4h8v2"/><path d="M19 6l-1 14H6L5 6"/></svg>
            Remove This Memory
        </button>
    `;
    const button = $("forget-selected-btn");
    if (button) button.addEventListener("click", () => forgetMemory(memory.text));
}

function detailCell(label, value) {
    return `<div class="detail-cell"><strong>${escapeHtml(label)}</strong><span>${escapeHtml(clean(value) || "none")}</span></div>`;
}

function selectItem(type, item) {
    state.selected = { type, item };
    renderGraph();
    renderCanvas();
    renderInspector();
}

function trimMiddle(text, max) {
    const value = clean(text);
    if (value.length <= max) return value;
    const keep = Math.max(4, Math.floor((max - 3) / 2));
    return `${value.slice(0, keep)}...${value.slice(value.length - keep)}`;
}

function initials(text) {
    const words = clean(text).split(" ").filter(Boolean);
    if (words.length === 0) return "?";
    if (words.length === 1) return words[0].slice(0, 2).toUpperCase();
    return `${words[0].slice(0, 1)}${words[words.length - 1].slice(0, 1)}`.toUpperCase();
}

function exportGraph() {
    if (!state.payload) {
        toast("No graph loaded.", "error");
        return;
    }
    const blob = new Blob([JSON.stringify(state.payload, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    const stamp = new Date().toISOString().slice(0, 19).split(":").join("-");
    link.href = url;
    link.download = `king-memory-graph-${stamp}.json`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
}

function bindEvents() {
    els.refreshBtn.addEventListener("click", loadGraph);
    els.exportBtn.addEventListener("click", exportGraph);
    for (const button of Array.from(document.querySelectorAll("[data-page]"))) {
        button.addEventListener("click", () => setPage(button.getAttribute("data-page") || "memory"));
    }
    els.rememberBtn.addEventListener("click", rememberMemory);
    els.forgetBtn.addEventListener("click", () => forgetMemory());
    els.reflectBtn.addEventListener("click", reflectMemory);
    els.importanceInput.addEventListener("input", () => {
        els.importanceValue.textContent = Number(els.importanceInput.value).toFixed(2);
    });
    els.graphSearch.addEventListener("input", () => {
        state.search = els.graphSearch.value;
        render();
    });
    els.canvasSearch.addEventListener("input", () => {
        state.canvasSearch = els.canvasSearch.value;
        state.canvas.fitted = false;
        renderCanvas();
    });
    for (const button of Array.from(document.querySelectorAll("[data-filter]"))) {
        button.addEventListener("click", () => {
            state.filter = button.getAttribute("data-filter") || "active";
            document.querySelectorAll("[data-filter]").forEach(item => item.classList.toggle("active", item === button));
            render();
        });
    }
    for (const button of Array.from(document.querySelectorAll("[data-canvas-filter]"))) {
        button.addEventListener("click", () => {
            state.canvasFilter = button.getAttribute("data-canvas-filter") || "active";
            state.canvas.fitted = false;
            document.querySelectorAll("[data-canvas-filter]").forEach(item => item.classList.toggle("active", item === button));
            renderCanvas();
        });
    }
    for (const button of Array.from(document.querySelectorAll("[data-view]"))) {
        button.addEventListener("click", () => {
            state.view = button.getAttribute("data-view") || "graph";
            document.querySelectorAll("[data-view]").forEach(item => item.classList.toggle("active", item === button));
            render();
        });
    }
    for (const button of Array.from(document.querySelectorAll("[data-memory-filter]"))) {
        button.addEventListener("click", () => {
            state.memoryFilter = button.getAttribute("data-memory-filter") || "all";
            document.querySelectorAll("[data-memory-filter]").forEach(item => item.classList.toggle("active", item === button));
            renderMemories();
        });
    }
    els.canvasFitBtn.addEventListener("click", () => {
        state.canvas.fitted = false;
        fitCanvas();
    });
    els.canvasZoomInBtn.addEventListener("click", () => zoomCanvas(1.18));
    els.canvasZoomOutBtn.addEventListener("click", () => zoomCanvas(0.84));
    els.canvasStage.addEventListener("wheel", event => {
        event.preventDefault();
        zoomCanvas(event.deltaY < 0 ? 1.08 : 0.92, event.clientX, event.clientY);
    }, { passive: false });
    els.canvasStage.addEventListener("pointerdown", event => {
        state.canvas.dragging = true;
        state.canvas.lastX = event.clientX;
        state.canvas.lastY = event.clientY;
        els.canvasStage.classList.add("dragging");
        els.canvasStage.setPointerCapture(event.pointerId);
    });
    els.canvasStage.addEventListener("pointermove", event => {
        if (!state.canvas.dragging) return;
        state.canvas.panX += event.clientX - state.canvas.lastX;
        state.canvas.panY += event.clientY - state.canvas.lastY;
        state.canvas.lastX = event.clientX;
        state.canvas.lastY = event.clientY;
        state.canvas.fitted = true;
        renderCanvas();
    });
    const stopCanvasDrag = event => {
        state.canvas.dragging = false;
        els.canvasStage.classList.remove("dragging");
        if (event && els.canvasStage.hasPointerCapture(event.pointerId)) {
            els.canvasStage.releasePointerCapture(event.pointerId);
        }
    };
    els.canvasStage.addEventListener("pointerup", stopCanvasDrag);
    els.canvasStage.addEventListener("pointercancel", stopCanvasDrag);
    els.canvasStage.addEventListener("click", () => {
        state.selected = null;
        renderCanvas();
        renderInspector();
    });
    window.addEventListener("resize", () => {
        renderGraph();
        if (state.page === "canvas") {
            state.canvas.fitted = false;
            renderCanvas();
        }
    });
}

bindEvents();
const initialView = new URLSearchParams(window.location.search).get("view") || window.location.hash.slice(1);
if (initialView === "graph" || initialView === "canvas") {
    setPage("canvas");
}
loadGraph();
