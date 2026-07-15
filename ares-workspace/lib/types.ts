export type ViewId = "chat" | "watchers" | "files" | "skills" | "mcp" | "settings";
export type ConnectionState = "connecting" | "online" | "offline";
export type JsonRecord = Record<string, unknown>;

export interface AresMessage extends JsonRecord { type: string }
export interface Session { id: number; title: string; summary?: string; started_at?: string; message_count: number }
export interface Artifact { id: string; name: string; path: string; mime?: string; kind?: "image" | "markdown" | "pdf" | "file" }
export interface ChatMessage { id?: number | string; role: "user" | "assistant"; content: string; created_at?: string; tool_calls?: ToolCall[]; artifacts?: Artifact[] }
export interface ArtifactPreview extends Artifact { content?: string; data_url?: string; preview_url?: string }
export interface ToolCall { name: string; content?: unknown; args?: JsonRecord }
export interface PendingFile { id: string; name: string; type: string; size: number; data?: string; path?: string; libraryId?: string }
export interface WorkspaceFile { id: string; name: string; type: string; size: number; modified_at: string; path: string }
export interface Skill {
  name: string; description: string; category: string; version: string; path: string;
  editable: boolean; model_invocable: boolean; files: string[]; examples?: unknown[];
  test_commands?: string[]; lint_messages?: string[]; source?: string;
}
export interface McpTool { name: string; full_name: string; description: string }
export interface McpServer {
  name: string; ready?: boolean; status?: string; transport?: string; endpoint?: string;
  server_url?: string; command?: string; args?: string[]; env?: Record<string, string>;
  oauth_client_id?: string; oauth_client_secret_configured?: boolean; oauth_scopes?: string[];
  timeout_seconds?: number; tools?: number; tools_detail?: McpTool[]; error?: string;
}
export interface McpState { summary: { ready: boolean; configured: number; connected: number; tools: number }; servers: McpServer[]; refreshed_at?: string }
export interface WatcherMonitor {
  id: string; name: string; type: string; url?: string; config?: JsonRecord; interval_seconds: number;
  ai_action: string; ai_prompt?: string; enabled: boolean; last_checked_at?: string;
  next_check_at?: string; last_status?: string; error_count: number; total_checks: number;
  total_changes: number; last_duration_ms?: number; last_error?: string;
  linked_goals?: WatcherGoal[]; goal_signal_count?: number; open_goal_signals?: WatcherGoalSignal[];
}
export interface WatcherGoal {
  goal_id: number; title: string; status: string; priority: string; progress_percent: number;
  target_date?: string; is_overdue?: boolean; days_remaining?: number; watcher_ids?: string[];
}
export interface WatcherGoalSignal {
  signal_id: number; goal_id: number; watcher_id: string; source_event_id: string; event_type: string;
  event_summary: string; severity: string; created_at: string; acknowledged: boolean;
  goal_title?: string; goal_status?: string; snoozed_until?: string;
}
export interface WatcherEvent {
  id: string; monitor_id: string; event_type: string; change_summary?: string; severity: string;
  notified: boolean; acknowledged: boolean; ai_summary?: string; created_at: string;
  goal_signals?: WatcherGoalSignal[];
}
export interface WatcherCheck {
  id: string; monitor_id: string; status: string; started_at: string; finished_at: string;
  duration_ms: number; changed: boolean; error?: string;
}
export interface WatcherOverview {
  monitors: number; active: number; paused: number; failing: number; unacknowledged_alerts: number;
  delivery_failures: number; total_checks: number; total_changes: number; average_latency_ms: number;
  checks_24h: number; success_rate_24h: number;
  goal_linked_watchers?: number; linked_goals?: number; open_goal_signals?: number;
}
export interface WatcherState {
  running: boolean; overview: WatcherOverview; monitors: WatcherMonitor[]; events: WatcherEvent[];
  checks: WatcherCheck[]; goals?: WatcherGoal[]; capabilities?: JsonRecord; dashboard_url?: string; refreshed_at?: string;
}
export interface RuntimeStatus {
  model?: string; memory_count?: number; session_id?: number | null;
  context_usage?: { used: number; total: number; percent: number; breakdown?: JsonRecord };
  watchers?: JsonRecord;
}
export interface TraceEvent { id: string; label: string; detail?: string; state: "active" | "done" | "error"; at: Date }
export interface ActivityEvent {
  id: string; label: string; detail?: string; kind: "thinking" | "streaming" | "tool" | "system";
  state: "active" | "done" | "error"; at: Date;
}

export interface WorkspaceSettings {
  identity?: Record<string, string>;
  personalization?: Record<string, string>;
  model?: Record<string, unknown>;
  telegram?: Record<string, unknown>;
  browser?: Record<string, unknown>;
  monitoring?: Record<string, unknown>;
  workspace?: Record<string, unknown>;
  advanced?: Record<string, string>;
}
