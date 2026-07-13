"use client";

import { Activity, AlertTriangle, ExternalLink, Globe2, Link2, MoreHorizontal, Pause, Play, Plus, RefreshCw, Search, ShieldCheck, Target, Trash2, Zap } from "lucide-react";
import { useMemo, useState } from "react";
import type { WatcherCheck, WatcherGoal, WatcherMonitor, WatcherState } from "@/lib/types";

interface Props {
  state: WatcherState;
  refresh: () => void;
  onCreate: () => void;
  onEdit: (monitor: WatcherMonitor) => void;
  onAction: (action: string, arguments_: Record<string, unknown>) => void;
}

function ago(value?: string) {
  if (!value) return "Never";
  const seconds = Math.max(0, (Date.now() - new Date(value).getTime()) / 1000);
  if (seconds < 60) return `${Math.floor(seconds)}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

function cadence(seconds: number) {
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h`;
  return `${Math.round(seconds / 86400)}d`;
}

function timeline(checks: WatcherCheck[]) {
  const now = new Date();
  now.setMinutes(0, 0, 0);
  return Array.from({ length: 12 }, (_, index) => {
    const start = new Date(now.getTime() - (11 - index) * 3600000);
    const end = new Date(start.getTime() + 3600000);
    const values = checks.filter(check => { const time = new Date(check.started_at).getTime(); return time >= start.getTime() && time < end.getTime(); });
    return { label: start.toLocaleTimeString([], { hour: "numeric" }), success: values.filter(item => item.status === "ok").length, failed: values.filter(item => item.status !== "ok").length };
  });
}

function TimelineChart({ checks }: { checks: WatcherCheck[] }) {
  const points = timeline(checks);
  const max = Math.max(1, ...points.map(item => item.success + item.failed));
  const coords = points.map((point, index) => `${25 + index * (550 / 11)},${130 - point.success / max * 96}`).join(" ");
  const area = `25,130 ${coords} 575,130`;
  return <svg className="timeline-chart" viewBox="0 0 600 155" preserveAspectRatio="none" role="img" aria-label="Successful and failed watcher checks over the last twelve hours">
    <defs><linearGradient id="successGradient" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stopColor="#f1f1ee" stopOpacity=".16" /><stop offset="1" stopColor="#f1f1ee" stopOpacity="0" /></linearGradient></defs>
    {[34, 66, 98, 130].map(y => <line key={y} className="grid" x1="25" y1={y} x2="575" y2={y} />)}
    <polygon className="success-area" points={area} /><polyline className="success-line" points={coords} />
    {points.map((point, index) => <g key={index}><rect className="fail-bar" x={22 + index * (550 / 11)} y={130 - point.failed / max * 96} width="6" height={point.failed / max * 96} rx="2" /><text x={25 + index * (550 / 11)} y="149" textAnchor="middle">{index % 2 === 0 ? point.label : ""}</text></g>)}
  </svg>;
}

function GoalRoute({ goals = [], open = 0 }: { goals?: WatcherGoal[]; open?: number }) {
  if (!goals.length) return <span className="goal-route-empty">Unlinked</span>;
  return <div className="workspace-goal-route">
    {goals.slice(0, 2).map(goal => <span className={`workspace-goal-chip ${goal.is_overdue ? "is-overdue" : ""}`} key={goal.goal_id} title={`#${goal.goal_id} · ${goal.status} · ${goal.progress_percent}%`}><i />#{goal.goal_id} {goal.title}</span>)}
    {goals.length > 2 && <span className="workspace-goal-chip is-more">+{goals.length - 2}</span>}
    {open > 0 && <span className="goal-open-count">{open} open</span>}
  </div>;
}

export function WatchersView({ state, refresh, onCreate, onEdit, onAction }: Props) {
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState("all");
  const overview = state.overview;
  const monitorNames = useMemo(() => Object.fromEntries(state.monitors.map(item => [item.id, item.name])), [state.monitors]);
  const monitors = state.monitors.filter(item => {
    const matches = `${item.name} ${item.type} ${item.url || ""} ${(item.linked_goals || []).map(goal => goal.title).join(" ")}`.toLowerCase().includes(query.toLowerCase());
    if (!matches) return false;
    if (filter === "active") return item.enabled;
    if (filter === "paused") return !item.enabled;
    if (filter === "failing") return ["error", "timeout"].includes(item.last_status || "");
    return true;
  });
  const incidents = state.events.filter(item => !item.acknowledged);
  const uptime = Number(overview.success_rate_24h || 100);
  const metrics = [
    { label: "Active watchers", value: overview.active, copy: `${overview.paused} paused`, icon: Activity, className: "" },
    { label: "Checks / 24h", value: overview.checks_24h, copy: `${overview.total_checks} lifetime`, icon: Zap, className: "" },
    { label: "Success rate", value: `${uptime.toFixed(1)}%`, copy: "completed checks / all checks", icon: ShieldCheck, className: uptime >= 98 ? "is-good" : uptime >= 90 ? "is-warn" : "is-bad" },
    { label: "Open incidents", value: overview.unacknowledged_alerts, copy: `${overview.total_changes} changes detected`, icon: AlertTriangle, className: overview.unacknowledged_alerts ? "is-warn" : "is-good" },
    { label: "Goal-linked", value: overview.goal_linked_watchers || 0, copy: `${overview.linked_goals || 0} outcomes · ${overview.open_goal_signals || 0} open signals`, icon: Target, className: overview.open_goal_signals ? "is-warn" : "is-good" },
  ];

  return <section className="view is-active"><div className="page-scroll">
    <div className="page-head"><div><p className="eyebrow">AUTONOMOUS OBSERVABILITY</p><h1>Watcher control plane</h1><p>Monitor public sites, authenticated browser state, APIs, and any read-only Ares or MCP workflow.</p></div><div className="head-actions"><button className="secondary-btn" onClick={() => state.dashboard_url && window.open(state.dashboard_url, "_blank", "noopener,noreferrer")}><ExternalLink />Advanced console</button><button className="primary-btn" onClick={onCreate}><Plus />Deploy watcher</button></div></div>
    <div className="freshness-row"><span><i className={`connection-dot ${state.running ? "is-online" : "is-offline"}`} /><strong>{state.running ? "Scheduler online" : "Scheduler stopped"}</strong></span><span><Link2 size={11} />{overview.goal_linked_watchers || 0} watchers route into {overview.linked_goals || 0} goals</span><span>{state.refreshed_at ? `Refreshed ${ago(state.refreshed_at)}` : "Waiting for refresh"}</span><button className="tiny-text-btn" onClick={refresh}>Refresh now</button></div>
    <div className="metric-grid">{metrics.map(metric => <article className={`metric-card ${metric.className}`} key={metric.label} title={metric.copy}><small>{metric.label}</small><strong>{metric.value}</strong><p>{metric.copy}</p><span className="metric-accent"><metric.icon /></span></article>)}</div>
    <div className="watcher-analysis-grid">
      <article className="panel chart-panel"><div className="panel-head"><div><p className="panel-kicker">CHECK VOLUME · LAST 12 HOURS</p><h2>Execution pulse</h2></div><div className="legend"><span><i className="ok" />Success</span><span><i className="bad" />Failure</span></div></div><div className="chart-wrap">{state.checks.length ? <TimelineChart checks={state.checks} /> : <div className="empty-state compact">Run checks to populate the execution timeline.</div>}</div></article>
      <article className="panel health-panel"><div className="panel-head"><div><p className="panel-kicker">FLEET RELIABILITY · 24H</p><h2>Health posture</h2></div></div><div id="healthPosture"><div className="health-gauge"><div className="gauge-ring" style={{ "--value": uptime } as React.CSSProperties}><div><strong>{uptime.toFixed(1)}%</strong><small>SUCCESS</small></div></div><div className="health-breakdown"><div className="health-row"><span>Healthy</span><strong>{Math.max(0, overview.active - overview.failing)}</strong><div className="bar"><i style={{ width: `${overview.active ? Math.max(0, (overview.active - overview.failing) / overview.active * 100) : 0}%` }} /></div></div><div className="health-row"><span>Failing</span><strong>{overview.failing}</strong><div className="bar"><i style={{ width: `${overview.active ? overview.failing / overview.active * 100 : 0}%`, background: "var(--danger)" }} /></div></div><div className="health-row"><span>Goal-routed</span><strong>{overview.goal_linked_watchers || 0}</strong><div className="bar goal"><i style={{ width: `${overview.monitors ? (overview.goal_linked_watchers || 0) / overview.monitors * 100 : 0}%` }} /></div></div></div></div><div className="goal-safety-note"><Target /><span><strong>Evidence, not mutation</strong><small>Signals enter goal context for review. Progress changes only after an explicit action.</small></span></div></div></article>
    </div>
    <div className="operator-grid">
      <article className="panel fleet-panel"><div className="panel-head table-head"><div><p className="panel-kicker">DEPLOYED AUTOMATIONS</p><h2>Watcher fleet</h2></div><div className="table-tools"><div className="search-field small"><Search /><input value={query} onChange={event => setQuery(event.target.value)} placeholder="Filter fleet" /></div><select value={filter} onChange={event => setFilter(event.target.value)}><option value="all">All states</option><option value="active">Active</option><option value="paused">Paused</option><option value="failing">Failing</option></select></div></div>
        <div className="data-table-wrap"><table className="data-table"><thead><tr><th>Watcher</th><th>Goal route</th><th>State</th><th>Last check</th><th>Cadence</th><th>Reliability</th><th /></tr></thead><tbody>{monitors.map(monitor => { const failing = ["error", "timeout"].includes(monitor.last_status || ""); const reliability = monitor.total_checks ? Math.max(0, 100 - monitor.error_count / monitor.total_checks * 100) : 100; return <tr key={monitor.id}><td><button className="watcher-name" onClick={() => onEdit(monitor)}><span className="watcher-type-icon"><Globe2 /></span><span><strong>{monitor.name}</strong><small>{monitor.url || monitor.type}</small></span></button></td><td><GoalRoute goals={monitor.linked_goals} open={monitor.open_goal_signals?.length || 0} /></td><td><span className={`state-badge ${failing ? "failing" : monitor.enabled ? "active" : "paused"}`}>{failing ? "Failing" : monitor.enabled ? "Active" : "Paused"}</span></td><td>{ago(monitor.last_checked_at)}</td><td>{cadence(monitor.interval_seconds)}</td><td>{reliability.toFixed(1)}%</td><td><div className="row-actions"><button title="Run now" onClick={() => onAction("run", { watcher_id: monitor.id })}><RefreshCw /></button><button title={monitor.enabled ? "Pause" : "Resume"} onClick={() => onAction(monitor.enabled ? "pause" : "resume", { watcher_id: monitor.id })}>{monitor.enabled ? <Pause /> : <Play />}</button><button title="Edit" onClick={() => onEdit(monitor)}><MoreHorizontal /></button><button title="Delete" onClick={() => { if (window.confirm(`Delete ${monitor.name} and all of its history?`)) onAction("delete", { watcher_id: monitor.id, confirm: true }); }}><Trash2 /></button></div></td></tr>; })}{!monitors.length && <tr><td colSpan={7}><div className="empty-state">No watchers match this view. Deploy a browser, site, API, or tool watcher.</div></td></tr>}</tbody></table></div>
      </article>
      <article className="panel incident-panel"><div className="panel-head"><div><p className="panel-kicker">ACTION QUEUE</p><h2>Open incidents</h2></div><span className="count-badge">{incidents.length}</span></div><div className="incident-list">{incidents.map(event => <div className="incident" key={event.id}><div className="incident-top"><strong>{monitorNames[event.monitor_id] || "Unknown watcher"}</strong><span className={`severity ${event.severity}`}>{event.severity}</span></div><p>{event.ai_summary || event.change_summary || `${event.event_type} detected`}</p>{event.goal_signals?.length ? <GoalRoute goals={event.goal_signals.map(signal => ({ goal_id: signal.goal_id, title: signal.goal_title || `Goal #${signal.goal_id}`, status: signal.goal_status || "active", priority: "normal", progress_percent: 0 }))} open={event.goal_signals.filter(signal => !signal.acknowledged).length} /> : null}<div className="incident-foot"><span>{ago(event.created_at)}</span><button onClick={() => onAction("acknowledge", { event_id: event.id })}>Acknowledge</button></div></div>)}{!incidents.length && <div className="empty-state"><div><ShieldCheck size={25} /><p>No open incidents. Your action queue is clear.</p></div></div>}</div></article>
    </div>
  </div></section>;
}
