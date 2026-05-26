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
    schemas: {},
    activeSchema: null,
    fields: [],
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
    feedbackBox: $('feedback-box'),
    argumentFields: $('argument-fields'),
    argumentHint: $('argument-hint'),
    syncJsonBtn: $('sync-json-btn'),
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
            loadSchemaForSelected(true);
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
            }),
        });
        if (result.session_id) state.lastSessionId = result.session_id;
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
    els.syncJsonBtn.addEventListener('click', syncJsonFromFields);
    els.addToolForm.addEventListener('submit', updateTool);
    els.disableToolBtn.addEventListener('click', disableSelectedTool);
    els.catalogBtn.addEventListener('click', searchCatalog);
    els.toolkitSelect.addEventListener('change', () => {
        els.toolkitInput.value = els.toolkitSelect.value;
    });
}

bind();
refresh();
