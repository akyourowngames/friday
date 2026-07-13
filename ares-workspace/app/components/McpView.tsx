"use client";

import { Activity, Cable, Edit3, Network, Plus, RefreshCw, Shield, Trash2, Wrench } from "lucide-react";
import type { McpServer, McpState } from "@/lib/types";

interface Props {
  state: McpState;
  probe: () => void;
  add: () => void;
  edit: (server: McpServer) => void;
  reconnect: (server: McpServer) => void;
  remove: (server: McpServer) => void;
}

export function McpView({ state, probe, add, edit, reconnect, remove }: Props) {
  const summary = state.summary;
  const metrics = [
    { label: "Configured", value: summary.configured || 0, copy: "shared MCP server definitions", icon: Network },
    { label: "Connected", value: summary.connected || 0, copy: "ready in the current runtime", icon: Cable, className: summary.connected ? "is-good" : "is-warn" },
    { label: "Available tools", value: summary.tools || 0, copy: "capabilities exposed to Ares", icon: Wrench },
    { label: "Fabric state", value: summary.ready ? "READY" : "DEGRADED", copy: state.refreshed_at ? `probed ${new Date(state.refreshed_at).toLocaleTimeString()}` : "awaiting first probe", icon: Activity, className: summary.ready ? "is-good" : "is-warn" },
  ];
  return <section className="view is-active"><div className="page-scroll">
    <div className="page-head"><div><p className="eyebrow">MODEL CONTEXT PROTOCOL</p><h1>Integration fabric</h1><p>Connect local or remote tool servers, inspect readiness, and expose their capabilities to Ares and watchers.</p></div><div className="head-actions"><button className="secondary-btn" onClick={probe}><Activity />Probe all</button><button className="primary-btn" onClick={add}><Plus />Add server</button></div></div>
    <div className="metric-grid mcp-metrics">{metrics.map(metric => <article className={`metric-card ${metric.className || ""}`} key={metric.label}><small>{metric.label}</small><strong>{metric.value}</strong><p>{metric.copy}</p><span className="metric-accent"><metric.icon /></span></article>)}</div>
    <div className="source-note"><Shield /><p><strong>Secret-safe management.</strong> Existing environment values and OAuth secrets are never returned to this UI. Leave secret fields blank to preserve them.</p></div>
    <div className="mcp-grid">{state.servers.map(server => <article className="mcp-card" key={server.name}><div className="mcp-card-head"><span className="server-mark">{server.name.slice(0, 2).toUpperCase()}</span><div><strong>{server.name}</strong><small>{server.endpoint || server.server_url || server.command || "No endpoint"}</small></div><span className={`state-badge ${server.status || "disconnected"}`}>{server.status || "disconnected"}</span></div><div className="mcp-card-body"><div className="server-facts"><div><small>TRANSPORT</small><strong>{server.transport || "—"}</strong></div><div><small>TOOLS</small><strong>{server.tools || 0}</strong></div><div><small>TIMEOUT</small><strong>{server.timeout_seconds || 60}s</strong></div></div>{server.error && <div className="server-error">{server.error}</div>}<div className="tool-list">{server.tools_detail?.slice(0, 12).map(tool => <span title={tool.description} key={tool.full_name}>{tool.name}</span>)}{!server.tools_detail?.length && <span>No schemas discovered</span>}</div><div className="mcp-card-actions"><button onClick={() => reconnect(server)}><RefreshCw size={12} /> Reconnect</button><button onClick={() => edit(server)}><Edit3 size={12} /> Edit</button><button onClick={() => remove(server)}><Trash2 size={12} /> Remove</button></div></div></article>)}{!state.servers.length && <div className="empty-state"><div><Network size={28} /><p>No MCP servers configured. Add a stdio, Streamable HTTP, or SSE server.</p></div></div>}</div>
  </div></section>;
}
