from __future__ import annotations

from html import escape

from .configuration import WatcherConfig


def dashboard_html(config: WatcherConfig) -> str:
    title = "KING Folder Watcher"
    watch_path = escape(str(config.watch_path))
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f7f9fb;
      --ink: #15191d;
      --muted: #5d6875;
      --line: #d8e0e6;
      --panel: #ffffff;
      --accent: #0f766e;
      --accent-2: #b45309;
      --dark: #17202a;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--ink);
    }}
    header {{
      display: flex;
      align-items: end;
      justify-content: space-between;
      gap: 24px;
      padding: 24px 28px 18px;
      border-bottom: 1px solid var(--line);
      background: var(--panel);
    }}
    h1 {{
      margin: 0;
      font-size: 26px;
      line-height: 1.1;
      letter-spacing: 0;
    }}
    .path {{
      margin-top: 8px;
      color: var(--muted);
      font-size: 13px;
      overflow-wrap: anywhere;
    }}
    .pill {{
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 7px 11px;
      font-size: 13px;
      background: #edf6f5;
      white-space: nowrap;
    }}
    main {{
      display: grid;
      grid-template-columns: minmax(280px, 360px) minmax(0, 1fr);
      min-height: calc(100vh - 92px);
    }}
    aside {{
      border-right: 1px solid var(--line);
      padding: 20px;
      background: #edf2f6;
    }}
    section {{
      padding: 20px;
    }}
    .stats {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
    }}
    .metric, .panel {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
    }}
    .metric {{
      padding: 12px;
      min-height: 78px;
    }}
    .metric b {{
      display: block;
      font-size: 24px;
      line-height: 1.2;
    }}
    .metric span {{
      color: var(--muted);
      font-size: 12px;
    }}
    .panel {{
      padding: 14px;
      margin-top: 14px;
    }}
    .panel h2 {{
      margin: 0 0 12px;
      font-size: 15px;
      letter-spacing: 0;
    }}
    .search {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto auto auto;
      gap: 8px;
      margin-bottom: 14px;
    }}
    input, button {{
      height: 38px;
      border-radius: 7px;
      border: 1px solid var(--line);
      font: inherit;
    }}
    input {{
      padding: 0 10px;
      background: #fff;
      color: var(--ink);
      min-width: 0;
    }}
    button {{
      padding: 0 13px;
      background: var(--dark);
      color: #fff;
      cursor: pointer;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      table-layout: fixed;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      overflow: hidden;
    }}
    th, td {{
      text-align: left;
      padding: 10px;
      border-bottom: 1px solid var(--line);
      font-size: 13px;
      vertical-align: top;
      overflow-wrap: anywhere;
    }}
    th {{
      color: var(--muted);
      font-weight: 600;
      background: #f1f5f8;
    }}
    tr:last-child td {{ border-bottom: 0; }}
    .tag {{
      display: inline-block;
      padding: 2px 7px;
      border-radius: 999px;
      background: #d8f1ed;
      color: #115e59;
      font-size: 12px;
      margin: 0 4px 4px 0;
    }}
    .event {{
      padding: 10px 0;
      border-bottom: 1px solid var(--line);
      font-size: 13px;
    }}
    .event b {{ color: var(--accent-2); }}
    .muted {{ color: var(--muted); }}
    #llmResult {{ white-space: pre-wrap; overflow-wrap: anywhere; }}
    .split {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(260px, 360px);
      gap: 16px;
      align-items: start;
    }}
    .status-list {{
      max-height: 320px;
      overflow: auto;
      padding-right: 4px;
    }}
    .status-list div {{
      padding: 7px 0;
      border-bottom: 1px solid var(--line);
      font-size: 13px;
    }}
    @media (max-width: 900px) {{
      header {{ align-items: start; flex-direction: column; }}
      main, .split, .search {{ grid-template-columns: 1fr; }}
      aside {{ border-right: 0; border-bottom: 1px solid var(--line); }}
    }}
  </style>
</head>
<body>
  <header>
    <div>
      <h1>{title}</h1>
      <div class="path">{watch_path}</div>
    </div>
    <div class="pill" id="socketState">connecting</div>
  </header>
  <main>
    <aside>
      <div class="stats">
        <div class="metric"><b id="activeFiles">0</b><span>active files</span></div>
        <div class="metric"><b id="events">0</b><span>events</span></div>
        <div class="metric"><b id="sizeBytes">0</b><span>bytes indexed</span></div>
        <div class="metric"><b id="summaryCoverage">0%</b><span>summary coverage</span></div>
      </div>
      <div class="panel">
        <h2>Capability Status</h2>
        <div class="stats">
          <div class="metric"><b id="implementedCount">0</b><span>implemented</span></div>
          <div class="metric"><b id="plannedCount">0</b><span>planned</span></div>
          <div class="metric"><b id="llmState">...</b><span>LLM mode</span></div>
          <div class="metric"><b id="llmModel">...</b><span>model</span></div>
        </div>
        <div class="status-list" id="partialList"></div>
      </div>
    </aside>
    <section>
      <div class="split">
        <div>
          <div class="search">
            <input id="searchInput" value="folder watcher" aria-label="Search indexed content">
            <button id="searchButton">Search</button>
            <button id="askButton">Ask LLM</button>
            <button id="summarizeButton">Summaries</button>
          </div>
          <div class="panel" id="llmPanel" style="display: none; margin-bottom: 14px">
            <h2>LLM Result</h2>
            <div id="llmResult" class="muted"></div>
          </div>
          <table>
            <thead>
              <tr><th style="width: 28%">File</th><th>Path</th><th style="width: 22%">Tags</th></tr>
            </thead>
            <tbody id="filesBody"></tbody>
          </table>
        </div>
        <div class="panel" style="margin-top: 0">
          <h2>Live Events</h2>
          <div id="eventsList"></div>
        </div>
      </div>
    </section>
  </main>
  <script>
    const fmt = new Intl.NumberFormat();
    const filesBody = document.getElementById('filesBody');
    const eventsList = document.getElementById('eventsList');
    const searchInput = document.getElementById('searchInput');
    const searchButton = document.getElementById('searchButton');
    const askButton = document.getElementById('askButton');
    const summarizeButton = document.getElementById('summarizeButton');
    const socketState = document.getElementById('socketState');
    const llmPanel = document.getElementById('llmPanel');
    const llmResult = document.getElementById('llmResult');

    function text(value) {{
      return value === undefined || value === null ? '' : String(value);
    }}

    function tags(values) {{
      if (!Array.isArray(values) || !values.length) return '<span class="muted">none</span>';
      return values.map((item) => `<span class="tag">${{text(item)}}</span>`).join('');
    }}

    async function loadStats() {{
      const stats = await fetch('/files/stats').then((r) => r.json());
      document.getElementById('activeFiles').textContent = fmt.format(stats.active_files || 0);
      document.getElementById('events').textContent = fmt.format(stats.events || 0);
      document.getElementById('sizeBytes').textContent = fmt.format(stats.total_size_bytes || 0);
      document.getElementById('summaryCoverage').textContent = Math.round((stats.summary_coverage || 0) * 100) + '%';
    }}

    async function loadStatus() {{
      const status = await fetch('/status').then((r) => r.json());
      const summary = status.summary || {{}};
      const llm = ((status.runtime || {{}}).llm || {{}});
      document.getElementById('implementedCount').textContent = fmt.format(summary.implemented || 0);
      document.getElementById('plannedCount').textContent = fmt.format(summary.planned || 0);
      document.getElementById('llmState').textContent = llm.provider_ready ? 'ready' : 'offline';
      document.getElementById('llmModel').textContent = text(llm.model || 'none').split('/').pop();
      const partial = Array.isArray(status.partial) ? status.partial : [];
      document.getElementById('partialList').innerHTML = partial.map((item) => `<div>${{text(item)}}</div>`).join('');
    }}

    function renderFiles(files) {{
      filesBody.innerHTML = files.map((file) => `
        <tr>
          <td><strong>${{text(file.filename)}}</strong><br><span class="muted">${{text(file.mime_type)}}</span></td>
          <td>${{text(file.path)}}${{file.snippet ? `<br><span class="muted">${{text(file.snippet)}}</span>` : ''}}</td>
          <td>${{tags(file.tags)}}</td>
        </tr>
      `).join('');
    }}

    async function loadLatest() {{
      const latest = await fetch('/files/latest?n=20').then((r) => r.json());
      renderFiles(latest.files || []);
    }}

    async function runSearch() {{
      const query = searchInput.value.trim();
      if (!query) {{
        await loadLatest();
        return;
      }}
      const result = await fetch('/files/search?q=' + encodeURIComponent(query) + '&limit=20').then((r) => r.json());
      renderFiles(result.files || []);
    }}

    async function askLlm() {{
      const query = searchInput.value.trim();
      if (!query) return;
      llmPanel.style.display = 'block';
      llmResult.textContent = 'asking...';
      const response = await fetch('/files/query', {{
        method: 'POST',
        headers: {{'content-type': 'application/json'}},
        body: JSON.stringify({{query, limit: 20}})
      }});
      const result = await response.json();
      if (!response.ok) {{
        llmResult.textContent = JSON.stringify(result.detail || result, null, 2);
        return;
      }}
      if (Array.isArray(result.files) && result.files.length) renderFiles(result.files);
      llmResult.textContent = JSON.stringify({{
        mode: result.mode,
        sql: result.sql,
        explanation: result.explanation,
        rows: result.rows || result.files || []
      }}, null, 2);
    }}

    async function summarizePending() {{
      llmPanel.style.display = 'block';
      llmResult.textContent = 'summarizing...';
      const response = await fetch('/files/summarize-pending', {{
        method: 'POST',
        headers: {{'content-type': 'application/json'}},
        body: JSON.stringify({{limit: 10}})
      }});
      const result = await response.json();
      llmResult.textContent = JSON.stringify(result.detail || result, null, 2);
      await loadStats();
      await loadLatest();
    }}

    function addEvent(event) {{
      const payload = event.payload || {{}};
      const file = payload.file || {{}};
      const row = document.createElement('div');
      row.className = 'event';
      row.innerHTML = `<b>${{text(event.event_type)}}</b><br>${{text(file.filename || event.new_path || event.old_path)}}<br><span class="muted">${{new Date((event.timestamp || Date.now() / 1000) * 1000).toLocaleTimeString()}}</span>`;
      eventsList.prepend(row);
      while (eventsList.children.length > 20) eventsList.removeChild(eventsList.lastChild);
    }}

    function connectSocket() {{
      const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
      const socket = new WebSocket(protocol + '//' + location.host + '/watch');
      socket.onopen = () => socketState.textContent = 'live';
      socket.onclose = () => {{
        socketState.textContent = 'reconnecting';
        setTimeout(connectSocket, 1500);
      }};
      socket.onerror = () => socket.close();
      socket.onmessage = async (message) => {{
        addEvent(JSON.parse(message.data));
        await loadStats();
        await loadLatest();
      }};
    }}

    searchButton.addEventListener('click', runSearch);
    askButton.addEventListener('click', askLlm);
    summarizeButton.addEventListener('click', summarizePending);
    searchInput.addEventListener('keydown', (event) => {{
      if (event.key === 'Enter') runSearch();
    }});

    Promise.all([loadStats(), loadStatus(), loadLatest()]).then(connectSocket);
  </script>
</body>
</html>"""
