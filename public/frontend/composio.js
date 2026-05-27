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
    authStatus: null,
    selectedTool: null,
    selectedToolkit: '',
    lastSessionId: window.localStorage?.getItem('king_composio_session_id') || '',
    schemas: {},
    activeSchema: null,
    fields: [],
    catalogItems: [],
};

const els = {
    statusPill: $('status-pill'),
    statusText: $('status-text'),
    metricApi: $('metric-api'),
    metricSession: $('metric-session'),
    metricToolkits: $('metric-toolkits'),
    metricTools: $('metric-tools'),
    refreshBtn: $('refresh-btn'),
    authStatusBtn: $('auth-status-btn'),
    toolkitCards: $('toolkit-cards'),
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
    feedbackBox: $('feedback-box'),
    argumentFields: $('argument-fields'),
    argumentHint: $('argument-hint'),
    syncJsonBtn: $('sync-json-btn'),
    confirmRisk: $('confirm-risk-checkbox'),
    schemaBtn: $('schema-btn'),
    executeBtn: $('execute-btn'),
    argumentsInput: $('arguments-input'),
    outputBox: $('output-box'),
    catalogBtn: $('catalog-btn'),
    catalogAllowBtn: $('catalog-allow-btn'),
    catalogQuery: $('catalog-query'),
    catalogLimit: $('catalog-limit'),
    catalogResults: $('catalog-results'),
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

function setFeedback(kind, title, detail = '') {
    els.feedbackBox.classList.remove('warn', 'error');
    if (kind) els.feedbackBox.classList.add(kind);
    els.feedbackBox.innerHTML = '';
    const heading = document.createElement('span');
    heading.className = 'feedback-title';
    heading.textContent = title;
    els.feedbackBox.appendChild(heading);
    if (detail) {
        const text = document.createElement('span');
        text.textContent = detail;
        els.feedbackBox.appendChild(text);
    }
}

function setStatus(kind, text) {
    els.statusPill.classList.remove('warn', 'error');
    if (kind) els.statusPill.classList.add(kind);
    els.statusText.textContent = text;
}

function rememberSession(sessionId) {
    const clean = String(sessionId || '').trim();
    if (!clean) return;
    state.lastSessionId = clean;
    try {
        window.localStorage?.setItem('king_composio_session_id', clean);
    } catch (_) {}
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

function renderToolkitCards(toolkits, tools) {
    if (!els.toolkitCards) return;
    const connected = new Set(state.authStatus?.connected_toolkits || []);
    els.toolkitCards.innerHTML = '';
    for (const toolkit of toolkits) {
        const count = tools.filter(tool => tool.toolkit === toolkit).length;
        const isConnected = connected.has(toolkit);
        const card = document.createElement('article');
        card.className = `toolkit-card${state.selectedToolkit === toolkit ? ' active' : ''}${isConnected ? ' connected' : ''}`;
        card.innerHTML = `
            <div class="toolkit-title">
                <strong></strong>
                <span class="connection-state"></span>
            </div>
            <div class="toolkit-meta">
                <span>approved</span>
                <span class="toolkit-count"></span>
            </div>
            <div class="toolkit-actions">
                <button class="primary-button" type="button">
                    <svg viewBox="0 0 24 24"><path d="M10 13a5 5 0 0 0 7 0l3-3a5 5 0 0 0-7-7l-1.5 1.5"/><path d="M14 11a5 5 0 0 0-7 0l-3 3a5 5 0 0 0 7 7l1.5-1.5"/></svg>
                    <span>Connect</span>
                </button>
                <button class="small-button" type="button">
                    <svg viewBox="0 0 24 24"><path d="M4 7h16"/><path d="M4 12h16"/><path d="M4 17h10"/></svg>
                </button>
            </div>
        `;
        card.querySelector('.toolkit-title strong').textContent = toolkit;
        card.querySelector('.connection-state').textContent = isConnected ? 'Live' : 'Link';
        card.querySelector('.toolkit-count').textContent = String(count);
        const buttons = card.querySelectorAll('button');
        buttons[0].addEventListener('click', event => {
            event.stopPropagation();
            selectToolkit(toolkit);
            connectToolkit(toolkit);
        });
        buttons[1].addEventListener('click', event => {
            event.stopPropagation();
            selectToolkit(toolkit);
        });
        card.addEventListener('click', () => selectToolkit(toolkit));
        els.toolkitCards.appendChild(card);
    }
}

function selectToolkit(toolkit) {
    state.selectedToolkit = toolkit || state.selectedToolkit;
    if (state.selectedToolkit) {
        els.toolkitInput.value = state.selectedToolkit;
        els.toolkitSelect.value = state.selectedToolkit;
    }
    renderStatus();
}

async function loadAuthStatus(silent = false) {
    if (!state.lastSessionId && silent) return;
    try {
        if (!silent) setStatus('warn', 'Checking');
        const params = new URLSearchParams();
        if (state.lastSessionId) params.set('session_id', state.lastSessionId);
        const suffix = params.toString() ? `?${params.toString()}` : '';
        const result = await request(`/composio/auth-status${suffix}`);
        state.authStatus = result;
        rememberSession(result.session_id);
        if (result.session_created) {
            showToast('Composio session created');
        }
        renderStatus();
        if (!silent) {
            const connected = result.connected_toolkits || [];
            setStatus('', 'Ready');
            setFeedback('', 'Connection status refreshed.', connected.length ? `Live: ${connected.join(', ')}` : 'No connected toolkit reported yet.');
        }
    } catch (error) {
        if (!silent) {
            setStatus('error', 'Blocked');
            setFeedback('error', 'Could not check Composio auth.', error.payload?.message || error.message);
        }
    }
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
    if (!state.selectedToolkit && toolkits[0]) {
        state.selectedToolkit = toolkits[0];
        els.toolkitInput.value = toolkits[0];
        els.toolkitSelect.value = toolkits[0];
    }
    if (state.selectedToolkit) {
        els.toolkitInput.value = state.selectedToolkit;
        els.toolkitSelect.value = state.selectedToolkit;
    }
    renderToolkitCards(toolkits, tools);
    renderTools(tools);
}

function renderTools(tools) {
    els.toolList.innerHTML = '';
    const visibleTools = state.selectedToolkit ? tools.filter(tool => tool.toolkit === state.selectedToolkit) : tools;
    if (!state.selectedTool && visibleTools.length) {
        state.selectedTool = visibleTools[0].slug;
    }
    if (!visibleTools.some(tool => tool.slug === state.selectedTool)) {
        state.selectedTool = visibleTools[0]?.slug || '';
    }
    for (const tool of visibleTools) {
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
            loadSchemaForSelected(true);
        });
        els.toolList.appendChild(row);
    }
    if (!visibleTools.length) {
        const empty = document.createElement('div');
        empty.className = 'result-strip';
        empty.textContent = state.selectedToolkit ? 'No approved tools for this toolkit.' : 'No approved tools in policy.';
        els.toolList.appendChild(empty);
    }
    els.selectedToolTitle.textContent = state.selectedTool || 'Run';
    if (state.selectedTool) {
        els.argumentHint.textContent = `${state.selectedTool} arguments`;
    }
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
        await loadAuthStatus(true);
        if (state.selectedTool) loadSchemaForSelected(true);
    } catch (error) {
        setStatus('error', 'Offline');
        els.outputBox.textContent = pretty(error.payload || error.message);
    }
}

function unwrapSchemaPayload(result) {
    if (result?.input_schema) {
        return { input_parameters: result.input_schema };
    }
    let data = result?.data || result;
    if (data?.truncated && typeof data.preview === 'string') {
        try {
            data = JSON.parse(data.preview);
        } catch (_) {}
    }
    return data || {};
}

function schemaInputParameters(schema) {
    const candidates = [
        schema?.input_parameters,
        schema?.inputSchema,
        schema?.input_schema,
        schema?.parameters,
        schema?.schema,
    ];
    for (const candidate of candidates) {
        if (candidate && typeof candidate === 'object' && candidate.properties) return candidate;
    }
    return { properties: {}, required: [] };
}

function buildFieldsFromSchema(schema) {
    const input = schemaInputParameters(schema);
    const required = Array.isArray(input.required) ? input.required.map(String) : [];
    const properties = input.properties || {};
    const names = Object.keys(properties);
    names.sort((a, b) => {
        const ar = required.includes(a) ? 0 : 1;
        const br = required.includes(b) ? 0 : 1;
        if (ar !== br) return ar - br;
        return a.localeCompare(b);
    });
    return names.slice(0, 10).map(name => {
        const meta = properties[name] || {};
        return {
            name,
            required: required.includes(name),
            type: meta.type || 'string',
            title: meta.human_parameter_name || meta.title || name,
            description: meta.human_parameter_description || meta.description || '',
            enum: Array.isArray(meta.enum) ? meta.enum : [],
            defaultValue: meta.default,
            examples: Array.isArray(meta.examples) ? meta.examples : [],
        };
    });
}

function parseArguments() {
    try {
        const parsed = JSON.parse(els.argumentsInput.value || '{}');
        return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : {};
    } catch (_) {
        return null;
    }
}

function argumentValue(field, args) {
    const value = args[field.name];
    if (value !== undefined && value !== null) return value;
    if (field.defaultValue !== undefined) return field.defaultValue;
    return '';
}

function coerceFieldValue(field, value) {
    if (value === '') return undefined;
    if (field.type === 'integer') {
        const parsed = Number.parseInt(value, 10);
        return Number.isNaN(parsed) ? value : parsed;
    }
    if (field.type === 'number') {
        const parsed = Number(value);
        return Number.isNaN(parsed) ? value : parsed;
    }
    if (field.type === 'boolean') {
        return value === true || value === 'true';
    }
    return value;
}

function syncJsonFromFields() {
    const args = parseArguments();
    if (args === null) {
        setFeedback('error', 'JSON needs a quick fix.', 'The payload must be a valid JSON object before the form can sync.');
        return null;
    }
    for (const field of state.fields) {
        const input = document.querySelector(`[data-arg-field="${field.name}"]`);
        if (!input) continue;
        const value = input.type === 'checkbox' ? input.checked : input.value;
        const coerced = coerceFieldValue(field, value);
        if (coerced === undefined) {
            delete args[field.name];
        } else {
            args[field.name] = coerced;
        }
    }
    els.argumentsInput.value = pretty(args);
    return args;
}

function defaultsForSelected(result = null) {
    return result?.argument_defaults
        || state.status?.argument_defaults?.[state.selectedTool]
        || state.policy?.argument_defaults?.[state.selectedTool]
        || {};
}

function renderArgumentFields() {
    const args = parseArguments() || {};
    els.argumentFields.innerHTML = '';
    if (!state.fields.length) {
        setFeedback('warn', 'Schema loaded, but no input fields were exposed.', 'You can still edit the JSON payload directly.');
        return;
    }
    const requiredNames = state.fields.filter(field => field.required).map(field => field.name);
    setFeedback('', 'Inputs ready.', requiredNames.length ? `Required: ${requiredNames.join(', ')}` : 'No required fields for this tool.');
    for (const field of state.fields) {
        const wrap = document.createElement('div');
        wrap.className = 'argument-field';
        const label = document.createElement('label');
        const title = document.createElement('span');
        title.textContent = field.title || field.name;
        label.appendChild(title);
        if (field.required) {
            const required = document.createElement('span');
            required.className = 'required-mark';
            required.textContent = 'required';
            label.appendChild(required);
        }
        wrap.appendChild(label);
        let input;
        if (field.enum.length) {
            input = document.createElement('select');
            const blank = document.createElement('option');
            blank.value = '';
            blank.textContent = field.required ? 'Choose...' : 'Any';
            input.appendChild(blank);
            for (const optionValue of field.enum) {
                const option = document.createElement('option');
                option.value = optionValue;
                option.textContent = optionValue;
                input.appendChild(option);
            }
        } else {
            input = document.createElement('input');
            input.type = field.type === 'integer' || field.type === 'number' ? 'number' : 'text';
            const example = field.examples[0] || '';
            input.placeholder = example ? String(example) : field.name;
        }
        input.dataset.argField = field.name;
        const value = argumentValue(field, args);
        if (value !== undefined && value !== null && value !== '') input.value = String(value);
        input.addEventListener('input', syncJsonFromFields);
        input.addEventListener('change', syncJsonFromFields);
        wrap.appendChild(input);
        const help = document.createElement('div');
        help.className = 'field-help';
        help.textContent = field.description || field.name;
        wrap.appendChild(help);
        els.argumentFields.appendChild(wrap);
    }
}

function applySchema(result) {
    const schema = unwrapSchemaPayload(result);
    state.activeSchema = schema;
    state.schemas[state.selectedTool] = schema;
    state.fields = buildFieldsFromSchema(schema);
    const args = parseArguments() || {};
    const defaults = { ...defaultsForSelected(result) };
    for (const field of state.fields) {
        if (field.defaultValue !== undefined && defaults[field.name] === undefined) defaults[field.name] = field.defaultValue;
    }
    if (Object.keys(defaults).length) {
        const merged = { ...args };
        const applied = [];
        for (const [key, value] of Object.entries(defaults)) {
            if (merged[key] === undefined || merged[key] === null || String(merged[key]).trim() === '') {
                merged[key] = value;
                applied.push(key);
            }
        }
        if (applied.length) {
            els.argumentsInput.value = pretty(merged);
        }
    } else if (Object.keys(args).length === 0) {
        const fieldDefaults = {};
        for (const field of state.fields) {
            if (field.defaultValue !== undefined) fieldDefaults[field.name] = field.defaultValue;
        }
        els.argumentsInput.value = pretty(fieldDefaults);
    }
    renderArgumentFields();
}

async function loadSchemaForSelected(silent = false) {
    if (!state.selectedTool) return;
    if (state.schemas[state.selectedTool]) {
        state.activeSchema = state.schemas[state.selectedTool];
        state.fields = buildFieldsFromSchema(state.activeSchema);
        renderArgumentFields();
        return;
    }
    try {
        if (!silent) setStatus('warn', 'Schema');
        const result = await request('/composio/action', {
            method: 'POST',
            body: JSON.stringify({ action: 'schema', tool_slug: state.selectedTool }),
        });
        applySchema(result);
        if (!silent) setStatus('', 'Ready');
    } catch (error) {
        if (!silent) setStatus('error', 'Blocked');
        setFeedback('error', 'Could not load schema.', error.payload?.message || error.message);
    }
}

function validateRequired(args) {
    const missing = [];
    for (const field of state.fields) {
        const value = args[field.name];
        if (field.required && (value === undefined || value === null || String(value).trim() === '')) {
            missing.push(field.name);
        }
    }
    return missing;
}

function extractProviderError(data) {
    const payload = data?.data || data;
    if (!payload || typeof payload !== 'object') return null;
    if (payload.error || payload.data?.message) {
        return {
            message: payload.error || payload.data?.message || 'Provider returned an error.',
            logId: payload.log_id || '',
            status: payload.data?.status_code ?? '',
        };
    }
    return null;
}

function renderResult(result) {
    const providerError = extractProviderError(result.data || result);
    if (providerError) {
        setFeedback('error', 'Composio returned a provider error.', providerError.message + (providerError.logId ? ` Log: ${providerError.logId}` : ''));
        els.outputBox.textContent = pretty(result.data || result);
        return;
    }
    setFeedback('', 'Tool completed.', `Executed ${result.tool_slug || state.selectedTool}.`);
    els.outputBox.textContent = pretty(result.data || result);
}

function renderCatalogResults(items) {
    state.catalogItems = Array.isArray(items) ? items : [];
    els.catalogResults.innerHTML = '';
    if (!state.catalogItems.length) {
        const empty = document.createElement('div');
        empty.className = 'result-strip';
        empty.textContent = 'No catalog tools returned.';
        els.catalogResults.appendChild(empty);
        return;
    }
    for (const item of state.catalogItems) {
        const row = document.createElement('div');
        row.className = 'catalog-item';
        const main = document.createElement('div');
        main.className = 'catalog-main';
        const slug = document.createElement('strong');
        slug.textContent = item.slug || item.name || 'Unknown tool';
        const detail = document.createElement('span');
        const required = Array.isArray(item.required_arguments) && item.required_arguments.length
            ? `Required: ${item.required_arguments.join(', ')}`
            : 'No required fields exposed';
        detail.textContent = `${item.toolkit || els.toolkitInput.value || els.toolkitSelect.value || ''} - ${required}`;
        const description = document.createElement('p');
        description.textContent = item.description || item.name || '';
        main.appendChild(slug);
        main.appendChild(detail);
        if (description.textContent) main.appendChild(description);
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'small-button';
        button.innerHTML = '<svg viewBox="0 0 24 24"><path d="M12 5v14"/><path d="M5 12h14"/></svg><span>Allow</span>';
        button.addEventListener('click', () => approveCatalogItems([item]));
        row.appendChild(main);
        row.appendChild(button);
        els.catalogResults.appendChild(row);
    }
}

function policyToolFromCatalogItem(item) {
    const toolkit = item.toolkit || els.toolkitInput.value.trim() || els.toolkitSelect.value;
    return {
        slug: String(item.slug || '').toUpperCase(),
        toolkit: String(toolkit || '').toLowerCase(),
        risk: els.riskSelect.value || 'read',
        enabled: true,
        note: item.description || item.name || '',
    };
}

async function approveCatalogItems(items = state.catalogItems) {
    const tools = (items || []).map(policyToolFromCatalogItem).filter(item => item.slug && item.toolkit);
    if (!tools.length) {
        showToast('No catalog tools to allow');
        return;
    }
    try {
        setStatus('warn', 'Allowing');
        state.policy = await request('/composio/policy/tools', {
            method: 'POST',
            body: JSON.stringify({ tools }),
        });
        showToast(`${tools.length} tool${tools.length === 1 ? '' : 's'} allowed`);
        await refresh();
        setStatus('', 'Ready');
    } catch (error) {
        setStatus('error', 'Blocked');
        els.catalogOutput.textContent = pretty(error.payload || error.message);
    }
}

async function createSession() {
    try {
        setStatus('warn', 'Working');
        const result = await request('/composio/action', {
            method: 'POST',
            body: JSON.stringify({ action: 'create_session' }),
        });
        rememberSession(result.session_id);
        els.authResult.textContent = result.mcp_url ? `Session ${result.session_id}\n${result.mcp_url}` : `Session ${result.session_id || 'created'}`;
        els.outputBox.textContent = pretty(result);
        showToast('Composio session ready');
        await refresh();
    } catch (error) {
        setStatus('error', 'Blocked');
        els.authResult.textContent = pretty(error.payload || error.message);
    }
}

async function connectToolkit(toolkitOverride = '') {
    const toolkit = toolkitOverride || els.toolkitSelect.value || state.selectedToolkit;
    if (!toolkit) return;
    try {
        selectToolkit(toolkit);
        setStatus('warn', 'Linking');
        const result = await request('/composio/connect', {
            method: 'POST',
            body: JSON.stringify({
                toolkit,
                session_id: state.lastSessionId,
                alias: `${toolkit}-local`,
            }),
        });
        rememberSession(result.session_id);
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
        await loadAuthStatus(true);
    } catch (error) {
        setStatus('error', 'Blocked');
        els.authResult.textContent = pretty(error.payload || error.message);
    }
}

async function fetchSchema() {
    if (!state.selectedTool) return;
    await loadSchemaForSelected(false);
    if (state.activeSchema) els.outputBox.textContent = pretty(state.activeSchema);
}

async function executeTool() {
    if (!state.selectedTool) return;
    if (!state.activeSchema && !state.schemas[state.selectedTool]) {
        await loadSchemaForSelected(true);
    }
    let args = syncJsonFromFields();
    if (args === null) {
        showToast('Arguments must be JSON');
        return;
    }
    const missing = validateRequired(args);
    if (missing.length) {
        setStatus('warn', 'Needs input');
        setFeedback('warn', 'Missing required fields.', `Fill ${missing.join(', ')} before running ${state.selectedTool}.`);
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
                confirm: Boolean(els.confirmRisk?.checked),
            }),
        });
        rememberSession(result.session_id);
        renderResult(result);
        setStatus('', 'Ready');
    } catch (error) {
        setStatus('error', 'Blocked');
        setFeedback('error', 'Composio action was blocked.', error.payload?.message || error.message);
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
        state.selectedToolkit = toolkit;
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
        let toolkit = els.toolkitInput.value.trim() || els.toolkitSelect.value;
        if (!toolkit) {
            await refresh();
            toolkit = els.toolkitInput.value.trim() || els.toolkitSelect.value;
        }
        const params = new URLSearchParams({
            toolkit,
            query: els.catalogQuery.value.trim(),
            limit: String(Number(els.catalogLimit.value || 20)),
        });
        const result = await request(`/composio/tools?${params.toString()}`);
        renderCatalogResults(result.items || []);
        els.catalogOutput.textContent = pretty(result.data || result);
        setStatus('', 'Ready');
    } catch (error) {
        setStatus('error', 'Blocked');
        els.catalogOutput.textContent = pretty(error.payload || error.message);
    }
}

function bind() {
    els.refreshBtn.addEventListener('click', refresh);
    if (els.authStatusBtn) els.authStatusBtn.addEventListener('click', () => loadAuthStatus(false));
    els.createSessionBtn.addEventListener('click', createSession);
    els.connectBtn.addEventListener('click', connectToolkit);
    els.schemaBtn.addEventListener('click', fetchSchema);
    els.executeBtn.addEventListener('click', executeTool);
    els.syncJsonBtn.addEventListener('click', syncJsonFromFields);
    els.addToolForm.addEventListener('submit', updateTool);
    els.disableToolBtn.addEventListener('click', disableSelectedTool);
    els.catalogBtn.addEventListener('click', searchCatalog);
    els.catalogAllowBtn.addEventListener('click', () => approveCatalogItems());
    els.toolkitSelect.addEventListener('change', () => {
        selectToolkit(els.toolkitSelect.value);
    });
}

bind();
refresh();
