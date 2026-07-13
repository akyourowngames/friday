const state = { overview:{}, monitors:[], events:[], checks:[], notifications:[], settings:{}, filter:"all", severity:"all", query:"" };
const $ = (selector, root=document) => root.querySelector(selector);
const $$ = (selector, root=document) => [...root.querySelectorAll(selector)];
const esc = value => String(value ?? "").replace(/[&<>'"]/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[char]));
const apiToken = localStorage.getItem("aresWatcherToken") || "";
const monitorTemplates = {
  website:{url:"",required:true,placeholder:"https://example.com/status",help:"Extract page text, CSS fields, prices, or raw HTML.",config:{change_detection:"diff",ignore_patterns:[]}},
  custom:{url:"",required:true,placeholder:"https://api.example.com/status",help:"Call a JSON API and extract fields with JSONPath.",config:{method:"GET",extractors:[{field:"status",json_path:"$.status"}],change_detection:"diff"}},
  instagram:{url:"",required:false,placeholder:"Meta Graph API URL (usually configured in JSON)",help:"Use the permitted Meta Graph API with credentials stored in config.",config:{api_url:"",access_token:"",fields:"id,caption,timestamp",change_detection:"diff"}},
  browser:{url:"https://www.instagram.com/direct/inbox/",required:false,placeholder:"Authenticated page URL or blank for current tab",help:"Uses the connected Playwright session. The default recipe monitors Instagram DMs without scraping credentials.",config:{preset:"instagram_dm",navigate:true,change_detection:"diff",ignore_patterns:["\\b\\d+\\s*(?:s|m|h|d|w)\\b","\\bactive\\s+(?:now|\\d+\\s*(?:m|h)\\s*ago)\\b"]}},
  tool:{url:"",required:false,placeholder:"Not required for Ares tool workflows",help:"Run bounded read-only Ares/MCP tool steps and diff the final result.",config:{steps:[{tool_name:"phone_get_notifications",arguments:{limit:20}}],change_detection:"diff",ignore_patterns:[]}}
};

function applyMonitorType(type, replaceConfig=false) {
  const form=$("#monitor-form"), template=monitorTemplates[type]||monitorTemplates.website, url=form.elements.url;
  url.required=template.required; url.placeholder=template.placeholder;
  if(replaceConfig){url.value=template.url;form.elements.config.value=JSON.stringify(template.config,null,2)}
  $("#monitor-config-help").textContent=template.help;
}

async function api(path, options={}) {
  const headers = {"Content-Type":"application/json", ...(apiToken ? {"X-Ares-Token":apiToken} : {}), ...(options.headers||{})};
  const response = await fetch(path, {...options, headers});
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try { detail = (await response.json()).detail || detail; } catch {}
    if(response.status===401) $("#auth-modal").hidden=false;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return response.status === 204 ? null : response.json();
}

const pad = n => String(n || 0).padStart(2,"0");
function ago(value) {
  if (!value) return "NEVER";
  const seconds = Math.max(0, (Date.now() - new Date(value).getTime()) / 1000);
  if (seconds < 60) return `${Math.floor(seconds)}S AGO`;
  if (seconds < 3600) return `${Math.floor(seconds/60)}M AGO`;
  if (seconds < 86400) return `${Math.floor(seconds/3600)}H AGO`;
  return `${Math.floor(seconds/86400)}D AGO`;
}
function interval(seconds) {
  if (seconds < 60) return `${seconds} SEC`;
  if (seconds < 3600) return `${seconds/60} MIN`;
  if (seconds < 86400) return `${seconds/3600} HR`;
  return `${seconds/86400} DAY`;
}
function statusOf(monitor) {
  if (!monitor.enabled) return ["paused","PAUSED"];
  if (["error","timeout"].includes(monitor.last_status)) return ["error",monitor.last_status.toUpperCase()];
  return ["ok", monitor.last_status ? "HEALTHY" : "ARMED"];
}
function monitorName(id) { return state.monitors.find(m=>m.id===id)?.name || "Unknown watcher"; }
function toast(title, message) {
  const node = document.createElement("div"); node.className="toast";
  node.innerHTML=`<b>${esc(title)}</b>${esc(message)}`; $("#toasts").append(node);
  setTimeout(()=>node.remove(), 5000);
}

async function loadAll(silent=false) {
  try {
    const [overview,monitors,events,checks,notifications,settings] = await Promise.all([
      api("/api/overview"),api("/api/monitors"),api("/api/events?limit=200"),api("/api/checks?limit=200"),api("/api/notifications?limit=200"),api("/api/settings")
    ]);
    Object.assign(state,{overview,monitors,events,checks,notifications,settings}); render();
    $("#rail-latency").textContent=`${overview.average_latency_ms || 0}MS AVG`;
  } catch (error) { if (!silent) toast("CONTROL PLANE ERROR", error.message); }
}

function render() { renderOverview(); renderFleet(); renderEvents(); renderTelemetry(); renderSettings(); }
function renderOverview() {
  const o=state.overview;
  $("#stat-success").textContent=`${Number(o.success_rate_24h||100).toFixed(1)}%`;
  $("#stat-checks").textContent=o.checks_24h||0; $("#stat-latency").textContent=`${o.average_latency_ms||0}ms`;
  $("#metric-active").textContent=pad(o.active); $("#metric-total").textContent=`OF ${o.monitors||0} CONFIGURED`;
  $("#metric-alerts").textContent=pad(o.unacknowledged_alerts); $("#metric-failing").textContent=pad(o.failing);
  $("#metric-changes").textContent=pad(o.total_changes); $("#nav-monitor-count").textContent=o.monitors||0;
  $("#nav-alert-count").textContent=o.unacknowledged_alerts||0;
  $("#alert-bars").innerHTML=Array.from({length:12},(_,i)=>`<i class="${i < Math.min(12,o.unacknowledged_alerts||0)?'hot':''}" style="height:${10+((i*17)%30)}px"></i>`).join("");
}
function renderFleet() {
  const compact=$("#fleet-compact"), table=$("#monitor-table");
  if (!state.monitors.length) compact.innerHTML="No monitors configured.";
  else compact.innerHTML=state.monitors.slice(0,5).map(m=>{
    const [klass,label]=statusOf(m); return `<div class="fleet-row" data-detail="${esc(m.id)}"><div class="fleet-name"><b>${esc(m.name)}</b><small>${esc(m.url||m.type)}</small></div><span class="status ${klass}"><i></i>${label}</span><span class="mono">${interval(m.interval_seconds)}</span><span class="mono">${ago(m.last_checked_at)}</span></div>`;
  }).join("");
  const query=state.query.toLowerCase();
  const filtered=state.monitors.filter(m=>{
    const match=!query || `${m.name} ${m.url} ${m.type}`.toLowerCase().includes(query);
    const [klass]=statusOf(m); return match && (state.filter==="all" || state.filter===klass || (state.filter==="active"&&m.enabled&&klass!=="error") || (state.filter==="failing"&&klass==="error"));
  });
  if (!filtered.length) { table.innerHTML="No monitors match this view."; table.classList.add("empty-state"); }
  else { table.classList.remove("empty-state"); table.innerHTML=filtered.map(m=>{
    const [klass,label]=statusOf(m); return `<div class="monitor-row"><div class="watcher-cell" data-detail="${esc(m.id)}"><b>${esc(m.name)}</b><small>${esc(m.type.toUpperCase())} · ${esc(m.url||"NO TARGET")}</small></div><span class="status ${klass}"><i></i>${label}</span><span class="mono">${interval(m.interval_seconds)}</span><span class="mono">${ago(m.last_checked_at)}</span><span class="mono">${m.last_duration_ms==null?'—':m.last_duration_ms+'MS'}</span><div class="row-actions"><button class="icon-action" title="Run now" data-action="check" data-id="${esc(m.id)}">↻</button><button class="icon-action" title="${m.enabled?'Pause':'Resume'}" data-action="${m.enabled?'pause':'resume'}" data-id="${esc(m.id)}">${m.enabled?'Ⅱ':'▶'}</button><button class="icon-action" title="Delete" data-action="delete" data-id="${esc(m.id)}">×</button></div></div>`;
  }).join(""); }
  bindFleetActions();
}
function renderEvents() {
  const compact=$("#events-compact");
  if (!state.events.length) compact.innerHTML="Awaiting the first watcher signal.";
  else compact.innerHTML=state.events.slice(0,5).map(e=>`<div class="event-row"><i class="severity-line ${esc(e.severity)}"></i><div><b>${esc(e.change_summary||e.event_type)}</b><small>${esc(monitorName(e.monitor_id))} · ${esc(e.event_type.toUpperCase())}</small></div><time>${ago(e.created_at)}</time></div>`).join("");
  const visible=state.events.filter(e=>state.severity==="all"||e.severity===state.severity), list=$("#incident-list");
  if (!visible.length) { list.innerHTML="No incidents recorded."; list.classList.add("empty-state"); return; }
  list.classList.remove("empty-state"); list.innerHTML=visible.map(e=>`<article class="incident ${esc(e.severity)} ${e.acknowledged?'acknowledged':''}" data-event="${esc(e.id)}"><i class="incident-marker"></i><div class="incident-body"><div class="incident-top"><span>${esc(e.severity)} · ${esc(e.event_type.replaceAll('_',' '))}</span><time>${new Date(e.created_at).toLocaleString()}</time></div><h3>${esc(monitorName(e.monitor_id))}</h3><p>${esc(e.change_summary||"A monitored signal changed.")}</p>${e.old_value!=null||e.new_value!=null?`<div class="incident-values"><div><span>PREVIOUS</span><code>${esc(e.old_value||'—')}</code></div><div><span>CURRENT</span><code>${esc(e.new_value||'—')}</code></div></div>`:''}${e.ai_summary?`<div class="ai-note"><b>ARES ANALYSIS</b><br>${esc(e.ai_summary)}</div>`:''}</div><div class="incident-actions">${e.acknowledged?'<button disabled>ACKNOWLEDGED</button>':`<button data-ack="${esc(e.id)}">ACKNOWLEDGE</button>`}<button data-monitor-detail="${esc(e.monitor_id)}">OPEN WATCHER</button></div></article>`).join("");
  $$('[data-ack]').forEach(btn=>btn.onclick=()=>acknowledge(btn.dataset.ack));
  $$('[data-monitor-detail]').forEach(btn=>btn.onclick=()=>openDetail(btn.dataset.monitorDetail));
}
function renderTelemetry() {
  const runs=[...state.checks].reverse().slice(-100), values=runs.map(run=>run.duration_ms||0), max=Math.max(...values,1);
  const points=values.map((value,i)=>`${values.length===1?450:i*900/(values.length-1)},${235-value/max*190}`).join(" ");
  $("#latency-line").setAttribute("d",points?`M${points.replaceAll(' ',' L')}`:"");
  $("#latency-area").setAttribute("d",points?`M0,235 L${points.replaceAll(' ',' L')} L900,235 Z`:"");
  const avg=values.length?Math.round(values.reduce((a,b)=>a+b,0)/values.length):0; $("#chart-average").textContent=`Ø ${avg}ms`;
  const channels={}; state.notifications.forEach(n=>{ const item=channels[n.channel]||={total:0,sent:0,failed:0}; item.total++; item[n.status]=(item[n.status]||0)+1; });
  const channel=$("#channel-health");
  if (!Object.keys(channels).length) channel.innerHTML="No notifications attempted.";
  else channel.innerHTML=Object.entries(channels).map(([name,item])=>{const rate=Math.round(item.sent*100/item.total);return `<div class="channel-row"><div><b>${esc(name)}</b><small>${item.sent} SENT · ${item.failed||0} FAILED</small></div><span class="channel-score">${rate}%</span></div>`}).join("");
  const log=$("#run-log");
  if (!state.checks.length) log.innerHTML="No check history yet.";
  else log.innerHTML=state.checks.slice(0,30).map(run=>`<div class="run-row"><span>${esc(monitorName(run.monitor_id))}</span><span>${new Date(run.started_at).toLocaleTimeString()}</span><span class="${esc(run.status)}">${esc(run.status.toUpperCase())}</span><span>${run.http_status||'—'} HTTP</span><span>${run.duration_ms}MS</span></div>`).join("");
}

function renderSettings() {
  const form=$("#settings-form"); if(!form || !state.settings.notifications) return;
  const set=(name,value)=>{const field=form.elements[name];if(!field)return;if(field.type==='checkbox')field.checked=Boolean(value);else if(value!==undefined&&value!==null&&value!=="***REDACTED***")field.value=value;};
  for(const [channel,config] of Object.entries(state.settings.notifications)) for(const [key,value] of Object.entries(config||{})) set(`${channel}.${key}`,value);
  for(const name of ['telegram.bot_token','email.password']) { const field=form.elements[name], value=name.split('.').reduce((current,key)=>current?.[key],state.settings.notifications); if(field&&value==='***REDACTED***'){field.value='';field.dataset.secretConfigured='true';field.placeholder='Configured · leave blank to keep';} }
  $("#setting-poll").textContent=`${state.settings.service?.poll_seconds||'—'} SEC`;
  $("#setting-concurrency").textContent=state.settings.service?.max_concurrency||'—';
  $("#setting-token").textContent=state.settings.security?.api_token_enabled?'ENABLED':'LOCAL-ONLY';
  $("#setting-sources").textContent=(state.settings.service?.monitor_types||[]).map(value=>value.toUpperCase()).join(' · ')||'—';
}

function bindFleetActions() {
  $$('[data-detail]').forEach(node=>node.onclick=()=>openDetail(node.dataset.detail));
  $$('[data-action]').forEach(button=>button.onclick=async event=>{event.stopPropagation(); const {action,id}=button.dataset;
    try {
      if (action==="delete") { if (!confirm("Delete this watcher and its full history?")) return; await api(`/api/monitors/${id}`,{method:"DELETE"}); toast("WATCHER REMOVED","Configuration and history deleted."); }
      else { await api(`/api/monitors/${id}/${action}`,{method:"POST"}); toast(action==="check"?"CHECK DISPATCHED":"WATCHER UPDATED",action==="check"?"Manual check accepted.":`Watcher ${action}d.`); }
      await loadAll(true);
    } catch(error) { toast("ACTION FAILED",error.message); }
  });
}
async function acknowledge(id) { try { await api(`/api/events/${id}/acknowledge`,{method:"POST"}); await loadAll(true); } catch(error){toast("ACK FAILED",error.message)} }
async function openDetail(id) {
  try {
    const data=await api(`/api/monitors/${id}`), m=data.monitor, [klass,label]=statusOf(m), success=data.checks.length?Math.round(data.checks.filter(c=>c.status==='ok').length*100/data.checks.length):100;
    $("#drawer-content").innerHTML=`<span class="drawer-kicker">${esc(m.type.toUpperCase())} WATCHER / ${esc(m.id.slice(0,8))}</span><h2>${esc(m.name)}</h2><div class="drawer-url">${esc(m.url||'NO TARGET')}</div><div class="detail-stats"><div><span>STATUS</span><b class="${klass}">${label}</b></div><div><span>SUCCESS</span><b>${success}%</b></div><div><span>CHANGES</span><b>${m.total_changes}</b></div></div><div class="drawer-section"><h3>OPERATIONAL STATE</h3><div class="fleet-row"><div class="fleet-name"><b>Last check</b><small>${m.last_checked_at?new Date(m.last_checked_at).toLocaleString():'Never checked'}</small></div><span class="mono">${m.last_duration_ms??'—'}MS</span></div><div class="fleet-row"><div class="fleet-name"><b>Next check</b><small>${m.next_check_at?new Date(m.next_check_at).toLocaleString():'On scheduler tick'}</small></div><span class="mono">${interval(m.interval_seconds)}</span></div>${m.last_error?`<div class="ai-note">${esc(m.last_error)}</div>`:''}</div><div class="drawer-section"><h3>LATEST SNAPSHOT</h3><pre class="config-code">${esc(data.latest_snapshot?JSON.stringify(data.latest_snapshot.metadata,null,2):'No snapshot captured yet.')}</pre></div><div class="drawer-section"><h3>DETECTION CONFIG</h3><pre class="config-code">${esc(JSON.stringify(m.config,null,2))}</pre></div>`;
    $("#drawer-content").insertAdjacentHTML('beforeend',`<button class="primary drawer-edit" data-edit-monitor="${esc(m.id)}">EDIT WATCHER CONFIG →</button>`);
    $('[data-edit-monitor]').onclick=()=>openEditor(m.id);
    $("#detail-drawer").classList.add("open"); $("#detail-drawer").setAttribute("aria-hidden","false");
  } catch(error) { toast("DETAIL FAILED",error.message); }
}

function openCreate() {
  const form=$("#monitor-form"); form.reset(); form.elements.monitor_id.value=''; form.elements.type.disabled=false;
  $("#monitor-form-kicker").textContent='DEPLOY WATCHER'; $("#monitor-form-title").textContent='NEW MONITOR';
  $("#monitor-submit").textContent='DEPLOY MONITOR →';
  applyMonitorType('website',true); $("#monitor-modal").hidden=false; form.elements.name.focus();
}
function openEditor(id) {
  const monitor=state.monitors.find(item=>item.id===id); if(!monitor)return;
  const form=$("#monitor-form"); form.elements.monitor_id.value=monitor.id; form.elements.name.value=monitor.name;
  form.elements.type.value=monitor.type; form.elements.type.disabled=true; form.elements.url.value=monitor.url||'';
  applyMonitorType(monitor.type,false);
  form.elements.interval_seconds.value=String(monitor.interval_seconds); form.elements.ai_action.value=monitor.ai_action;
  form.elements.config.value=JSON.stringify(monitor.config,null,2); $("#monitor-form-kicker").textContent='RECONFIGURE WATCHER';
  $("#monitor-form-title").textContent='EDIT MONITOR'; $("#monitor-submit").textContent='SAVE CHANGES →';
  $("#detail-drawer").classList.remove('open'); $("#detail-drawer").setAttribute('aria-hidden','true'); $("#monitor-modal").hidden=false;
}

function navigate(view) {
  $$('.nav-item').forEach(item=>item.classList.toggle('active',item.dataset.view===view));
  $$('.view').forEach(node=>node.classList.toggle('active',node.id===`view-${view}`));
  $("#current-section").textContent=view.toUpperCase();
  $("#page-title").innerHTML={overview:'WATCHER <em>COMMAND.</em>',monitors:'FLEET <em>CONTROL.</em>',incidents:'SIGNAL <em>REVIEW.</em>',telemetry:'SYSTEM <em>TELEMETRY.</em>',settings:'DELIVERY <em>CONFIG.</em>'}[view];
  $('.rail').classList.remove('open'); history.replaceState(null,'',`#${view}`);
}
function connectRealtime() {
  const scheme=location.protocol==='https:'?'wss':'ws', query=apiToken?`?token=${encodeURIComponent(apiToken)}`:'';
  const socket=new WebSocket(`${scheme}://${location.host}/ws${query}`);
  socket.onopen=()=>$(".live-pill span").textContent="LIVE FEED";
  socket.onmessage=event=>{ const message=JSON.parse(event.data); if(message.type==='alert.created') toast("NEW WATCHER SIGNAL",message.payload.event.change_summary||'A change was detected.'); if(!['connected','heartbeat'].includes(message.type)) loadAll(true); };
  socket.onclose=()=>{ $(".live-pill span").textContent="RECONNECTING"; setTimeout(connectRealtime,3000); };
}

document.addEventListener("DOMContentLoaded",()=>{
  $$('.nav-item').forEach(item=>item.onclick=()=>navigate(item.dataset.view)); $$('[data-jump]').forEach(item=>item.onclick=()=>navigate(item.dataset.jump));
  $('.mobile-menu').onclick=()=>$('.rail').classList.toggle('open');
  $('#add-monitor').onclick=openCreate;
  $('#monitor-form').elements.type.onchange=event=>applyMonitorType(event.target.value,true);
  $$('[data-close]').forEach(node=>node.onclick=()=>$('#monitor-modal').hidden=true);
  $('#monitor-modal').onclick=event=>{if(event.target===event.currentTarget)event.currentTarget.hidden=true};
  $('[data-drawer-close]').onclick=()=>{$('#detail-drawer').classList.remove('open');$('#detail-drawer').setAttribute('aria-hidden','true')};
  $('#monitor-search').oninput=event=>{state.query=event.target.value;renderFleet()};
  $$('[data-filter]').forEach(btn=>btn.onclick=()=>{$$('[data-filter]').forEach(b=>b.classList.remove('active'));btn.classList.add('active');state.filter=btn.dataset.filter;renderFleet()});
  $$('[data-severity]').forEach(btn=>btn.onclick=()=>{$$('[data-severity]').forEach(b=>b.classList.remove('active'));btn.classList.add('active');state.severity=btn.dataset.severity;renderEvents()});
  $('#ack-all').onclick=async()=>{for(const event of state.events.filter(e=>!e.acknowledged&&(state.severity==='all'||e.severity===state.severity)))await api(`/api/events/${event.id}/acknowledge`,{method:'POST'});await loadAll(true)};
  $('#monitor-form').onsubmit=async event=>{event.preventDefault();const form=event.currentTarget,data=new FormData(form),id=data.get('monitor_id');try{const payload={name:data.get('name'),url:data.get('url'),interval_seconds:Number(data.get('interval_seconds')),ai_action:data.get('ai_action'),config:JSON.parse(data.get('config')||'{}')};if(!id)payload.type=data.get('type');await api(id?`/api/monitors/${id}`:'/api/monitors',{method:id?'PATCH':'POST',body:JSON.stringify(payload)});form.reset();form.elements.type.disabled=false;$('#monitor-modal').hidden=true;toast(id?'WATCHER UPDATED':'WATCHER DEPLOYED',`${payload.name} is ${id?'reconfigured':'armed and scheduled'}.`);await loadAll(true)}catch(error){toast('DEPLOYMENT FAILED',error.message)}};
  $('#settings-form').onsubmit=async event=>{event.preventDefault();const form=event.currentTarget,read=name=>form.elements[name]?.value||'',checked=name=>Boolean(form.elements[name]?.checked),secret=name=>read(name)||(form.elements[name]?.dataset.secretConfigured?'***REDACTED***':'');const notifications={desktop:{enabled:checked('desktop.enabled'),timeout:Number(read('desktop.timeout')||8)},telegram:{enabled:checked('telegram.enabled'),chat_id:read('telegram.chat_id'),bot_token:secret('telegram.bot_token')},email:{enabled:checked('email.enabled'),smtp_host:read('email.smtp_host'),smtp_port:Number(read('email.smtp_port')||587),username:read('email.username'),password:secret('email.password'),to_address:read('email.to_address'),start_tls:true},webhook:{enabled:checked('webhook.enabled'),url:read('webhook.url'),allow_private_network:checked('webhook.allow_private_network')}};try{await api('/api/settings',{method:'PATCH',body:JSON.stringify({notifications})});toast('DELIVERY CONFIG SAVED','Notification channels are active with the new policy.');await loadAll(true)}catch(error){toast('SETTINGS FAILED',error.message)}};
  $('#auth-form').onsubmit=event=>{event.preventDefault();localStorage.setItem('aresWatcherToken',event.currentTarget.elements.token.value);location.reload()};
  setInterval(()=>$('#clock').textContent=new Date().toLocaleTimeString([], {hour12:false}),1000);
  const initial=location.hash.slice(1); if(['overview','monitors','incidents','telemetry','settings'].includes(initial))navigate(initial);
  loadAll(); connectRealtime(); setInterval(()=>loadAll(true),30000);
});
