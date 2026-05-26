const API = (() => {
    if (typeof window === 'undefined') return 'http://127.0.0.1:8000';
    if (window.KING_API_BASE) return window.KING_API_BASE;
    const current = new URL(window.location.origin);
    if ((current.hostname === 'localhost' || current.hostname === '127.0.0.1') && current.port !== '8000') {
        return `${current.protocol}//${current.hostname}:8000`;
    }
    return window.location.origin || 'http://127.0.0.1:8000';
})();

const $ = id => document.getElementById(id);
const state = {
    status: null,
    policy: null,
    selectedTool: null,
    lastSessionId: '',
};

const els = {
    statusPill: $('status-pill'),
    statusText: $('status-text'),
    metricApi: $('metric-api'),
    metricSession: $('metric-session'),
    metricToolkits: $('metric-toolkits'),
    metricTools: $('metric-tools'),
    refreshBtn: $('refresh-btn'),
    createSessionBtn: $('create-session-btn'),
    connectBtn: $('connect-btn'),
    toolkitSelect: $('toolkit-select'),
    authResult: $('auth-result'),
    toolList: $('tool-list'),
    disableToolBtn: $('disable-tool-btn'),
    addToolForm: $('add-tool-form'),
    toolSlugInput: $('tool-slug-input'),
    toolkitInput: $('toolkit-input'),
    riskSelect: $('risk-select'),
    toolNoteInput: $('tool-note-input'),
    selectedToolTitle: $('selected-tool-title'),
    schemaBtn: $('schema-btn'),
    executeBtn: $('execute-btn'),
    argumentsInput: $('arguments-input'),
    outputBox: $('output-box'),
    catalogBtn: $('catalog-btn'),
    catalogQuery: $('catalog-query'),
    catalogLimit: $('catalog-limit'),
    catalogOutput: $('catalog-output'),
    toast: $('toast'),
};

function showToast(message) {
    if (!els.toast) return;
    els.toast.textContent = message;
    els.toast.classList.add('show');
    clearTimeout(showToast.timer);
    showToast.timer = setTimeout(() => els.toast.classList.remove('show'), 2600);
}

function pretty(value) {
    try {
        return JSON.stringify(value, null, 2);
    } catch (_) {
        return String(value);
    }
}

function setStatus(kind, text) {
    els.statusPill.classList.remove('warn', 'error');
    if (kind) els.statusPill.classList.add(kind);
    els.statusText.textContent = text;
}

async function request(path, options = {}) {
    const response = await fetch(`${API}${path}`, {
        headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
        ...options,
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
        const detail = payload.detail || payload;
        const message = detail.message || detail.code || `HTTP ${response.status}`;
        const error = new Error(message);
        error.payload = detail;
        throw error;
    }
    return payload;
}

function toolkitsFromStatus(status, policy) {
    const values = new Set();
    for (const item of status?.enabled_toolkits || []) values.add(item);
    for (const item of policy?.enabled_toolkits || []) values.add(item);
    return [...values].sort();
}

function toolsFromStatus(status, policy) {
    const tools = status?.enabled_tools?.length ? status.enabled_tools : policy?.enabled_tools || [];
    return [...tools].sort((a, b) => String(a.slug).localeCompare(String(b.slug)));
}

function renderStatus() {
    const status = state.status || {};
    const policy = state.policy || {};
    const tools = toolsFromStatus(status, policy);
    const toolkits = toolkitsFromStatus(status, policy);
    els.metricApi.textContent = status.api_key_present ? 'Ready' : 'Missing';
    els.metricSession.textContent = status.session_id_present || state.lastSessionId ? 'Ready' : 'New';
    els.metricToolkits.textContent = String(toolkits.length);
    els.metricTools.textContent = String(tools.length);
    setStatus(status.api_key_present ? '' : 'warn', status.api_key_present ? 'Ready' : 'Key needed');

    els.toolkitSelect.innerHTML = '';
    for (const toolkit of toolkits) {
        const option = document.createElement('option');
        option.value = toolkit;
        option.textContent = toolkit;
        els.toolkitSelect.appendChild(option);
    }
    if (toolkits.length === 0) {
        const option = document.createElement('option');
        option.value = '';
        option.textContent = 'No toolkit';
        els.toolkitSelect.appendChild(option);
    }
    if (!els.toolkitInput.value && toolkits[0]) {
        els.toolkitInput.value = toolkits[0];
    }
    renderTools(tools);
}

function renderTools(tools) {
    els.toolList.innerHTML = '';
    if (!state.selectedTool && tools.length) {
        state.selectedTool = tools[0].slug;
    }
    if (!tools.some(tool => tool.slug === state.selectedTool)) {
        state.selectedTool = tools[0]?.slug || '';
    }
    for (const tool of tools) {
        const row = document.createElement('button');
        row.type = 'button';
        row.className = `tool-row${tool.slug === state.selectedTool ? ' active' : ''}`;
        row.innerHTML = `
            <span class="tool-main">
                <strong></strong>
                <span></span>
            </span>
            <span class="tool-risk ${tool.risk || 'read'}"></span>
        `;
        row.querySelector('strong').textContent = tool.slug;
        row.querySelector('.tool-main span').textContent = `${tool.toolkit || ''}${tool.note ? ' - ' + tool.note : ''}`;
        row.querySelector('.tool-risk').textContent = tool.risk || 'read';
        row.addEventListener('click', () => {
            state.selectedTool = tool.slug;
            renderTools(toolsFromStatus(state.status, state.policy));
        });
        els.toolList.appendChild(row);
    }
    if (!tools.length) {
        const empty = document.createElement('div');
        empty.className = 'result-strip';
        empty.textContent = 'No approved tools in policy.';
        els.toolList.appendChild(empty);
    }
    els.selectedToolTitle.textContent = state.selectedTool || 'Run';
}

async function refresh() {
    try {
        setStatus('warn', 'Syncing');
        const [status, policy] = await Promise.all([
            request('/composio/status'),
            request('/composio/policy'),
        ]);
        state.status = status;
        state.policy = policy;
        renderStatus();
    } catch (error) {
        setStatus('error', 'Offline');
        els.outputBox.textContent = pretty(error.payload || error.message);
    }
}

async function createSession() {
    try {
        setStatus('warn', 'Working');
        const result = await request('/composio/action', {
            method: 'POST',
            body: JSON.stringify({ action: 'create_session' }),
        });
        state.lastSessionId = result.session_id || '';
        els.authResult.textContent = result.mcp_url ? `Session ${result.session_id}\n${result.mcp_url}` : `Session ${result.session_id || 'created'}`;
        els.outputBox.textContent = pretty(result);
        showToast('Composio session ready');
        await refresh();
    } catch (error) {
        setStatus('error', 'Blocked');
        els.authResult.textContent = pretty(error.payload || error.message);
    }
}

async function connectToolkit() {
    const toolkit = els.toolkitSelect.value;
    if (!toolkit) return;
    try {
        setStatus('warn', 'Linking');
        const result = await request('/composio/action', {
            method: 'POST',
            body: JSON.stringify({ action: 'link', toolkit, session_id: state.lastSessionId }),
        });
        if (result.session_id) state.lastSessionId = result.session_id;
        const url = result.redirect_url || '';
        els.authResult.innerHTML = '';
        const line = document.createElement('div');
        line.textContent = url || 'No redirect URL returned.';
        els.authResult.appendChild(line);
        if (url) {
            const button = document.createElement('button');
            button.type = 'button';
            button.className = 'small-button';
            button.style.marginTop = '10px';
            button.innerHTML = '<svg viewBox="0 0 24 24"><path d="M7 17L17 7"/><path d="M7 7h10v10"/></svg><span>Open</span>';
            button.addEventListener('click', () => window.open(url, '_blank', 'noopener,noreferrer'));
            els.authResult.appendChild(button);
            window.open(url, '_blank', 'noopener,noreferrer');
        }
        els.outputBox.textContent = pretty(result);
        showToast('Auth link created');
    } catch (error) {
        setStatus('error', 'Blocked');
        els.authResult.textContent = pretty(error.payload || error.message);
    }
}

async function fetchSchema() {
    if (!state.selectedTool) return;
    try {
        setStatus('warn', 'Schema');
        const result = await request('/composio/action', {
            method: 'POST',
            body: JSON.stringify({ action: 'schema', tool_slug: state.selectedTool }),
        });
        els.outputBox.textContent = pretty(result.data || result);
        setStatus('', 'Ready');
    } catch (error) {
        setStatus('error', 'Blocked');
        els.outputBox.textContent = pretty(error.payload || error.message);
    }
}

async function executeTool() {
    if (!state.selectedTool) return;
    let args = {};
    try {
        args = JSON.parse(els.argumentsInput.value || '{}');
    } catch (_) {
        showToast('Arguments must be JSON');
        return;
    }
    try {
        setStatus('warn', 'Running');
        const result = await request('/composio/action', {
            method: 'POST',
            body: JSON.stringify({
                action: 'execute',
                tool_slug: state.selectedTool,
                arguments: args,
                session_id: state.lastSessionId,
            }),
        });
        if (result.session_id) state.lastSessionId = result.session_id;
        els.outputBox.textContent = pretty(result.data || result);
        setStatus('', 'Ready');
    } catch (error) {
        setStatus('error', 'Blocked');
        els.outputBox.textContent = pretty(error.payload || error.message);
    }
}

async function updateTool(event) {
    event.preventDefault();
    const slug = els.toolSlugInput.value.trim().toUpperCase();
    const toolkit = els.toolkitInput.value.trim().toLowerCase();
    if (!slug || !toolkit) {
        showToast('Slug and toolkit are required');
        return;
    }
    try {
        const policy = await request('/composio/policy/tool', {
            method: 'POST',
            body: JSON.stringify({
                slug,
                toolkit,
                risk: els.riskSelect.value,
                enabled: true,
                note: els.toolNoteInput.value.trim(),
            }),
        });
        state.policy = policy;
        state.selectedTool = slug;
        els.toolSlugInput.value = '';
        els.toolNoteInput.value = '';
        renderStatus();
        showToast('Policy updated');
        await refresh();
    } catch (error) {
        showToast(error.payload?.message || error.message);
    }
}

async function disableSelectedTool() {
    if (!state.selectedTool) return;
    const tool = toolsFromStatus(state.status, state.policy).find(item => item.slug === state.selectedTool);
    if (!tool) return;
    try {
        state.policy = await request('/composio/policy/tool', {
            method: 'POST',
            body: JSON.stringify({
                slug: tool.slug,
                toolkit: tool.toolkit,
                risk: tool.risk || 'read',
                enabled: false,
                note: tool.note || '',
            }),
        });
        state.selectedTool = '';
        renderStatus();
        showToast('Tool disabled');
        await refresh();
    } catch (error) {
        showToast(error.payload?.message || error.message);
    }
}

async function searchCatalog() {
    try {
        setStatus('warn', 'Searching');
        const result = await request('/composio/action', {
            method: 'POST',
            body: JSON.stringify({
                action: 'catalog',
                toolkit: els.toolkitSelect.value,
                query: els.catalogQuery.value.trim(),
                limit: Number(els.catalogLimit.value || 20),
            }),
        });
        els.catalogOutput.textContent = pretty(result.data || result);
        setStatus('', 'Ready');
    } catch (error) {
        setStatus('error', 'Blocked');
        els.catalogOutput.textContent = pretty(error.payload || error.message);
    }
}

function bind() {
    els.refreshBtn.addEventListener('click', refresh);
    els.createSessionBtn.addEventListener('click', createSession);
    els.connectBtn.addEventListener('click', connectToolkit);
    els.schemaBtn.addEventListener('click', fetchSchema);
    els.executeBtn.addEventListener('click', executeTool);
    els.addToolForm.addEventListener('submit', updateTool);
    els.disableToolBtn.addEventListener('click', disableSelectedTool);
    els.catalogBtn.addEventListener('click', searchCatalog);
    els.toolkitSelect.addEventListener('change', () => {
        els.toolkitInput.value = els.toolkitSelect.value;
    });
}

bind();
refresh();
