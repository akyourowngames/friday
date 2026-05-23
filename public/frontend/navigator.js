const NAV_API = (() => {
    if (typeof window === 'undefined') return 'http://127.0.0.1:8000';
    if (window.KING_API_BASE) return window.KING_API_BASE;
    try {
        const current = new URL(window.location.href);
        if (current.protocol === 'file:') return 'http://127.0.0.1:8000';
        if ((current.hostname === 'localhost' || current.hostname === '127.0.0.1') && current.port !== '8000') {
            return `${current.protocol}//${current.hostname}:8000`;
        }
        return window.location.origin || 'http://127.0.0.1:8000';
    } catch (_) {
        return 'http://127.0.0.1:8000';
    }
})();

const canvas = document.getElementById('route-canvas');
const ctx = canvas.getContext('2d');
const form = document.getElementById('route-form');
const originInput = document.getElementById('origin-input');
const destinationInput = document.getElementById('destination-input');
const statusEl = document.getElementById('provider-status');
const routeDistanceEl = document.getElementById('route-distance');
const routeDistanceSubEl = document.getElementById('route-distance-sub');
const routeTimeEl = document.getElementById('route-time');
const routeModeEl = document.getElementById('route-mode');
const directDistanceEl = document.getElementById('direct-distance');
const originNameEl = document.getElementById('origin-name');
const destinationNameEl = document.getElementById('destination-name');
const providerLineEl = document.getElementById('provider-line');
const submitBtn = form.querySelector('.submit-btn');
const modeButtons = Array.from(document.querySelectorAll('.mode-pill'));

let currentMode = 'driving';
let currentPayload = null;
let animationStart = performance.now();

function setStatus(text, mode) {
    const label = statusEl.querySelector('strong');
    if (label) label.textContent = text;
    statusEl.classList.toggle('error', mode === 'error');
}

function fitCanvas() {
    const rect = canvas.getBoundingClientRect();
    const scale = window.devicePixelRatio || 1;
    canvas.width = Math.max(1, Math.floor(rect.width * scale));
    canvas.height = Math.max(1, Math.floor(rect.height * scale));
    ctx.setTransform(scale, 0, 0, scale, 0, 0);
}

function shortPlace(place) {
    if (!place || typeof place !== 'object') return '';
    return String(place.name || place.display_name || place.query || '').trim();
}

function longPlace(place) {
    if (!place || typeof place !== 'object') return '';
    return String(place.display_name || place.name || place.query || '').trim();
}

function metricText(value, suffix) {
    if (value === null || value === undefined || value === '') return '--';
    return `${value} ${suffix}`;
}

function renderPayload(payload) {
    currentPayload = payload;
    if (!payload || typeof payload !== 'object') return;
    const route = payload.route || {};
    const straight = payload.straight_line || {};
    const origin = payload.origin || {};
    const destination = payload.destination || {};
    originInput.value = payload.origin_query || shortPlace(origin) || originInput.value;
    destinationInput.value = payload.destination_query || shortPlace(destination) || destinationInput.value;
    routeDistanceEl.textContent = metricText(route.distance_km, 'km');
    routeDistanceSubEl.textContent = metricText(route.distance_miles, 'mi');
    routeTimeEl.textContent = route.duration_text || '--';
    routeModeEl.textContent = payload.mode || currentMode;
    directDistanceEl.textContent = metricText(straight.distance_km, 'km');
    originNameEl.textContent = longPlace(origin) || 'Origin resolved';
    destinationNameEl.textContent = longPlace(destination) || 'Destination resolved';
    const providers = Array.isArray(payload.provider_sequence) ? payload.provider_sequence.join(' + ') : '';
    const degraded = payload.degraded ? `Fallback: ${payload.degraded_reason || 'route unavailable'}` : 'Route provider returned road distance.';
    providerLineEl.textContent = `${providers || 'open providers'} · ${degraded}`;
    setStatus(payload.degraded ? 'Fallback' : 'Routed', payload.degraded ? '' : 'ok');
}

function loadStoredPayload() {
    try {
        const raw = sessionStorage.getItem('kingNavigatorPayload');
        if (!raw) return;
        const payload = JSON.parse(raw);
        renderPayload(payload);
    } catch (_) {}
}

async function requestRoute() {
    const origin = originInput.value.trim();
    const destination = destinationInput.value.trim();
    if (!origin || !destination) return;
    submitBtn.disabled = true;
    setStatus('Routing', '');
    providerLineEl.textContent = 'Calling navigator tool...';
    try {
        const response = await fetch(`${NAV_API}/navigator/route`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                origin,
                destination,
                mode: currentMode,
                alternatives: false,
                timeout_ms: 0,
            }),
        });
        const data = await response.json();
        if (!response.ok) {
            const detail = data && data.detail ? data.detail : {};
            throw new Error(detail.message || detail.provider_status || `HTTP ${response.status}`);
        }
        try {
            sessionStorage.setItem('kingNavigatorPayload', JSON.stringify(data));
        } catch (_) {}
        renderPayload(data);
    } catch (err) {
        setStatus('Error', 'error');
        providerLineEl.textContent = err && err.message ? err.message : 'Navigator route failed.';
    } finally {
        submitBtn.disabled = false;
    }
}

function setMode(mode) {
    currentMode = mode || 'driving';
    modeButtons.forEach(button => {
        button.classList.toggle('active', button.dataset.mode === currentMode);
    });
    routeModeEl.textContent = currentMode;
}

function drawGrid(width, height, t) {
    ctx.save();
    ctx.globalAlpha = 0.34;
    ctx.strokeStyle = 'rgba(210, 226, 214, 0.08)';
    ctx.lineWidth = 1;
    const spacing = 42;
    const offset = (t * 0.018) % spacing;
    for (let x = -spacing + offset; x < width + spacing; x += spacing) {
        ctx.beginPath();
        ctx.moveTo(x, 0);
        ctx.lineTo(x + height * 0.34, height);
        ctx.stroke();
    }
    for (let y = -spacing + offset; y < height + spacing; y += spacing) {
        ctx.beginPath();
        ctx.moveTo(0, y);
        ctx.lineTo(width, y - width * 0.18);
        ctx.stroke();
    }
    ctx.restore();
}

function drawRoute(width, height, t) {
    const origin = { x: width * 0.18, y: height * 0.66 };
    const destination = { x: width * 0.82, y: height * 0.34 };
    const control = { x: width * 0.54, y: height * 0.13 };
    const progress = ((t * 0.00018) % 1);

    ctx.save();
    ctx.lineCap = 'round';
    ctx.lineJoin = 'round';
    ctx.strokeStyle = 'rgba(67, 232, 198, 0.2)';
    ctx.lineWidth = 18;
    ctx.beginPath();
    ctx.moveTo(origin.x, origin.y);
    ctx.quadraticCurveTo(control.x, control.y, destination.x, destination.y);
    ctx.stroke();

    ctx.strokeStyle = 'rgba(240, 182, 75, 0.78)';
    ctx.lineWidth = 4;
    ctx.setLineDash([10, 18]);
    ctx.lineDashOffset = -t * 0.045;
    ctx.beginPath();
    ctx.moveTo(origin.x, origin.y);
    ctx.quadraticCurveTo(control.x, control.y, destination.x, destination.y);
    ctx.stroke();
    ctx.setLineDash([]);

    const px = (1 - progress) * (1 - progress) * origin.x + 2 * (1 - progress) * progress * control.x + progress * progress * destination.x;
    const py = (1 - progress) * (1 - progress) * origin.y + 2 * (1 - progress) * progress * control.y + progress * progress * destination.y;
    drawNode(origin.x, origin.y, '#43e8c6', t);
    drawNode(destination.x, destination.y, '#f0b64b', t + 900);
    ctx.fillStyle = '#eef6ef';
    ctx.beginPath();
    ctx.arc(px, py, 5, 0, Math.PI * 2);
    ctx.fill();
    ctx.restore();
}

function drawNode(x, y, color, t) {
    const pulse = 14 + Math.sin(t * 0.004) * 4;
    ctx.save();
    ctx.strokeStyle = color;
    ctx.globalAlpha = 0.24;
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.arc(x, y, pulse, 0, Math.PI * 2);
    ctx.stroke();
    ctx.globalAlpha = 1;
    ctx.fillStyle = color;
    ctx.beginPath();
    ctx.arc(x, y, 6, 0, Math.PI * 2);
    ctx.fill();
    ctx.restore();
}

function draw() {
    const rect = canvas.getBoundingClientRect();
    const width = rect.width;
    const height = rect.height;
    const t = performance.now() - animationStart;
    ctx.clearRect(0, 0, width, height);
    drawGrid(width, height, t);
    drawRoute(width, height, t);
    requestAnimationFrame(draw);
}

form.addEventListener('submit', event => {
    event.preventDefault();
    requestRoute();
});

modeButtons.forEach(button => {
    button.addEventListener('click', () => setMode(button.dataset.mode));
});

window.addEventListener('resize', fitCanvas);
fitCanvas();
loadStoredPayload();
draw();
