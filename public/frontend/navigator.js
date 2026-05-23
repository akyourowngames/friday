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
let routeModel = null;

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
    providerLineEl.textContent = `${providers || 'open providers'} - ${degraded}`;
    setStatus(payload.degraded ? 'Fallback' : 'Routed', payload.degraded ? '' : 'ok');
    routeModel = buildRouteModel(payload);
}

function loadStoredPayload() {
    try {
        const raw = sessionStorage.getItem('kingNavigatorPayload');
        if (!raw) return;
        const payload = JSON.parse(raw);
        renderPayload(payload);
    } catch (_) {}
}

function loadRouteFromQuery() {
    try {
        const params = new URLSearchParams(window.location.search || '');
        const origin = params.get('origin') || '';
        const destination = params.get('destination') || '';
        const mode = params.get('mode') || '';
        const autorun = params.get('autorun') === '1' || params.get('autorun') === 'true';
        if (origin) originInput.value = origin;
        if (destination) destinationInput.value = destination;
        if (mode) setMode(mode);
        if (autorun && origin && destination) {
            requestRoute();
            return true;
        }
    } catch (_) {}
    return false;
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

function decodePolyline(encoded) {
    const points = [];
    if (!encoded || typeof encoded !== 'string') return points;
    let index = 0;
    let lat = 0;
    let lon = 0;

    while (index < encoded.length) {
        let result = 0;
        let shift = 0;
        let byte = 0;
        do {
            byte = encoded.charCodeAt(index++) - 63;
            result |= (byte & 0x1f) << shift;
            shift += 5;
        } while (byte >= 0x20 && index < encoded.length);
        const dlat = (result & 1) ? ~(result >> 1) : (result >> 1);
        lat += dlat;

        result = 0;
        shift = 0;
        do {
            byte = encoded.charCodeAt(index++) - 63;
            result |= (byte & 0x1f) << shift;
            shift += 5;
        } while (byte >= 0x20 && index < encoded.length);
        const dlon = (result & 1) ? ~(result >> 1) : (result >> 1);
        lon += dlon;

        points.push({ lat: lat / 100000, lon: lon / 100000 });
    }
    return points;
}

function mercatorPoint(point) {
    const lat = Math.max(-85, Math.min(85, Number(point.lat) || 0));
    const lon = Number(point.lon) || 0;
    const sin = Math.sin((lat * Math.PI) / 180);
    return {
        x: (lon + 180) / 360,
        y: 0.5 - Math.log((1 + sin) / (1 - sin)) / (4 * Math.PI),
        lat,
        lon,
    };
}

function routeFallbackPoints(payload) {
    const origin = payload && payload.origin ? payload.origin : {};
    const destination = payload && payload.destination ? payload.destination : {};
    const start = { lat: Number(origin.lat), lon: Number(origin.lon) };
    const end = { lat: Number(destination.lat), lon: Number(destination.lon) };
    if (!Number.isFinite(start.lat) || !Number.isFinite(start.lon) || !Number.isFinite(end.lat) || !Number.isFinite(end.lon)) {
        return [];
    }
    const points = [];
    for (let i = 0; i <= 24; i++) {
        const k = i / 24;
        const bow = Math.sin(k * Math.PI) * 0.08;
        points.push({
            lat: start.lat + (end.lat - start.lat) * k + bow,
            lon: start.lon + (end.lon - start.lon) * k - bow,
        });
    }
    return points;
}

function simplifyPoints(points, maxPoints) {
    if (!Array.isArray(points) || points.length <= maxPoints) return points || [];
    const result = [];
    const step = (points.length - 1) / (maxPoints - 1);
    for (let i = 0; i < maxPoints; i++) {
        result.push(points[Math.round(i * step)]);
    }
    return result;
}

function buildRouteModel(payload) {
    const route = payload && payload.route ? payload.route : {};
    const decoded = decodePolyline(route.geometry || '');
    const geoPoints = decoded.length >= 2 ? decoded : routeFallbackPoints(payload);
    const points = simplifyPoints(geoPoints, 420).map(mercatorPoint);
    if (points.length < 2) return null;

    const start = points[0];
    const end = points[points.length - 1];
    const angle = Math.atan2(end.y - start.y, end.x - start.x);
    return {
        points,
        routeDistance: route.distance_km || null,
        angle,
        angleDegrees: Math.round((angle * 180) / Math.PI),
        seed: Math.abs(Math.round((start.lon + end.lon + start.lat + end.lat) * 1000)),
    };
}

function mapBounds(points) {
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
    const padX = Math.max((maxX - minX) * 0.18, 0.0008);
    const padY = Math.max((maxY - minY) * 0.18, 0.0008);
    return {
        minX: minX - padX,
        maxX: maxX + padX,
        minY: minY - padY,
        maxY: maxY + padY,
    };
}

function visualRect(width, height) {
    const compact = width < 780;
    const top = compact ? height * 0.37 : height * 0.23;
    const left = compact ? width * 0.08 : width * 0.10;
    const rectWidth = compact ? width * 0.84 : width * 0.80;
    const rectHeight = compact ? height * 0.25 : height * 0.36;
    return { x: left, y: top, w: rectWidth, h: rectHeight };
}

function projectedRoute(width, height) {
    const fallback = [
        { x: width * 0.18, y: height * 0.44 },
        { x: width * 0.32, y: height * 0.35 },
        { x: width * 0.52, y: height * 0.30 },
        { x: width * 0.70, y: height * 0.38 },
        { x: width * 0.84, y: height * 0.32 },
    ];
    if (!routeModel || !routeModel.points || routeModel.points.length < 2) {
        return { points: fallback, rect: visualRect(width, height), bounds: null };
    }
    const rect = visualRect(width, height);
    const bounds = mapBounds(routeModel.points);
    const spanX = Math.max(bounds.maxX - bounds.minX, 0.0001);
    const spanY = Math.max(bounds.maxY - bounds.minY, 0.0001);
    const projected = routeModel.points.map(point => ({
        x: rect.x + ((point.x - bounds.minX) / spanX) * rect.w,
        y: rect.y + ((point.y - bounds.minY) / spanY) * rect.h,
        lat: point.lat,
        lon: point.lon,
    }));
    return { points: projected, rect, bounds };
}

function routeLengths(points) {
    const lengths = [0];
    let total = 0;
    for (let i = 1; i < points.length; i++) {
        const dx = points[i].x - points[i - 1].x;
        const dy = points[i].y - points[i - 1].y;
        total += Math.hypot(dx, dy);
        lengths.push(total);
    }
    return { lengths, total };
}

function pointAtProgress(points, progress) {
    const data = routeLengths(points);
    if (!data.total) return points[0];
    const target = data.total * progress;
    for (let i = 1; i < data.lengths.length; i++) {
        if (data.lengths[i] >= target) {
            const prev = points[i - 1];
            const next = points[i];
            const segment = data.lengths[i] - data.lengths[i - 1] || 1;
            const k = (target - data.lengths[i - 1]) / segment;
            return {
                x: prev.x + (next.x - prev.x) * k,
                y: prev.y + (next.y - prev.y) * k,
            };
        }
    }
    return points[points.length - 1];
}

function drawGrid(width, height, t) {
    ctx.save();
    ctx.globalAlpha = 0.26;
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

function drawHologramField(width, height, t, rect) {
    ctx.save();
    ctx.strokeStyle = 'rgba(67, 232, 198, 0.16)';
    ctx.lineWidth = 1;
    ctx.globalAlpha = 0.72;
    for (let i = 0; i < 9; i++) {
        const y = rect.y + (rect.h / 8) * i + Math.sin(t * 0.0014 + i) * 5;
        ctx.beginPath();
        ctx.moveTo(rect.x - 34, y);
        ctx.lineTo(rect.x + rect.w + 34, y - rect.h * 0.18);
        ctx.stroke();
    }
    for (let i = 0; i < 12; i++) {
        const x = rect.x + (rect.w / 11) * i + Math.cos(t * 0.001 + i) * 4;
        ctx.beginPath();
        ctx.moveTo(x, rect.y - 30);
        ctx.lineTo(x + rect.h * 0.22, rect.y + rect.h + 36);
        ctx.stroke();
    }
    const sweep = rect.x + ((t * 0.045) % (rect.w + 180)) - 90;
    const gradient = ctx.createLinearGradient(sweep - 80, 0, sweep + 80, 0);
    gradient.addColorStop(0, 'rgba(67, 232, 198, 0)');
    gradient.addColorStop(0.5, 'rgba(67, 232, 198, 0.22)');
    gradient.addColorStop(1, 'rgba(67, 232, 198, 0)');
    ctx.fillStyle = gradient;
    ctx.fillRect(sweep - 80, rect.y - 80, 160, rect.h + 160);
    ctx.restore();
}

function drawPolyline(points) {
    if (!points || points.length < 2) return;
    ctx.beginPath();
    ctx.moveTo(points[0].x, points[0].y);
    for (let i = 1; i < points.length; i++) {
        ctx.lineTo(points[i].x, points[i].y);
    }
}

function drawRoute(width, height, t) {
    const projection = projectedRoute(width, height);
    const points = projection.points;
    const origin = points[0];
    const destination = points[points.length - 1];
    const progress = (t * 0.00016) % 1;
    drawHologramField(width, height, t, projection.rect);

    ctx.save();
    ctx.lineCap = 'round';
    ctx.lineJoin = 'round';
    ctx.shadowBlur = 28;
    ctx.shadowColor = 'rgba(67, 232, 198, 0.78)';
    ctx.strokeStyle = 'rgba(67, 232, 198, 0.18)';
    ctx.lineWidth = 22;
    drawPolyline(points);
    ctx.stroke();

    ctx.shadowBlur = 16;
    ctx.strokeStyle = 'rgba(67, 232, 198, 0.68)';
    ctx.lineWidth = 8;
    drawPolyline(points);
    ctx.stroke();

    ctx.shadowBlur = 6;
    ctx.strokeStyle = 'rgba(240, 182, 75, 0.9)';
    ctx.lineWidth = 3;
    ctx.setLineDash([9, 14]);
    ctx.lineDashOffset = -t * 0.06;
    drawPolyline(points);
    ctx.stroke();
    ctx.setLineDash([]);

    const traveler = pointAtProgress(points, progress);
    drawNode(origin.x, origin.y, '#43e8c6', t);
    drawNode(destination.x, destination.y, '#f0b64b', t + 900);
    drawRouteTelemetry(points, projection.rect, t);
    ctx.fillStyle = '#eef6ef';
    ctx.shadowBlur = 18;
    ctx.shadowColor = '#eef6ef';
    ctx.beginPath();
    ctx.arc(traveler.x, traveler.y, 5, 0, Math.PI * 2);
    ctx.fill();
    ctx.restore();
}

function drawRouteTelemetry(points, rect, t) {
    ctx.save();
    ctx.shadowBlur = 0;
    ctx.fillStyle = 'rgba(67, 232, 198, 0.72)';
    const count = Math.min(14, Math.max(4, Math.floor(points.length / 20)));
    for (let i = 1; i < count; i++) {
        const k = i / count;
        const pulse = pointAtProgress(points, (k + t * 0.000035) % 1);
        const radius = 1.5 + Math.sin(t * 0.006 + i) * 0.8;
        ctx.beginPath();
        ctx.arc(pulse.x, pulse.y, Math.max(0.8, radius), 0, Math.PI * 2);
        ctx.fill();
    }
    ctx.strokeStyle = 'rgba(240, 182, 75, 0.42)';
    ctx.lineWidth = 1;
    for (let i = 0; i < 5; i++) {
        const x = rect.x + rect.w * ((i + 1) / 6);
        const y = rect.y + rect.h + 18 + Math.sin(t * 0.002 + i) * 4;
        ctx.beginPath();
        ctx.moveTo(x - 24, y);
        ctx.lineTo(x + 24, y);
        ctx.stroke();
    }
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
loadRouteFromQuery();
draw();
