/**
 * Permission / approval system for friday-ng.
 *
 * Modeled after Pi Coding Agent's `permissions.ts`. Decides whether a tool
 * call should be allowed, denied, or asked of the user.
 *
 * Three modes:
 *   - `allow` — tool runs unconditionally
 *   - `deny`  — tool is rejected
 *   - `ask`   — host is given the chance to prompt the user
 *
 * The decisions are made by:
 *   1. Mode-by-tool (`permissions.<toolName>`)
 *   2. Mode-by-pattern (`permissions.rules[].tool + pattern match on input`)
 *   3. Default mode (`permissions.default`)
 *
 * Patterns support:
 *   - a literal `*`           matches any non-empty value
 *   - a `prefix*` glob        prefix match
 *   - a path glob with `*`/`**`  segments can use `*`, `**` matches across `/`
 *   - `re:/regex/flags`       real regex
 *   - exact string            matches when value === pattern
 */
import type { AgentTool } from "./types.ts";

export type PermissionMode = "allow" | "deny" | "ask";

export interface PermissionRule {
	/** Tool this rule applies to. `*` means every tool. */
	tool: string;
	/** Argument key to match against. `*` means the rule only depends on `tool`. */
	key?: string;
	/** Pattern to match the value against. */
	pattern: string;
	/** What to do when this rule matches. */
	mode: PermissionMode;
	/** Free-form human reason, surfaced to the user when asking. */
	reason?: string;
}

export interface PermissionPolicy {
	default: PermissionMode;
	tools: Record<string, PermissionMode>;
	rules: PermissionRule[];
}

export const DEFAULT_POLICY: PermissionPolicy = {
	default: "ask",
	tools: {
		// Read-only tools are auto-approved by default; the host can override.
		read: "allow",
		glob: "allow",
		grep: "allow",
		websearch: "allow",
	},
	rules: [],
};

export interface PermissionRequest {
	tool: AgentTool<any, any>;
	args: unknown;
	reason?: string;
}

/** Match a single string against a pattern. See file header for syntax. */
export function matchPattern(value: string, pattern: string): boolean {
	if (pattern === "*") return value.length > 0;
	if (pattern.startsWith("re:")) {
		const rest = pattern.slice(3);
		const firstSlash = rest.indexOf("/");
		if (firstSlash < 0) return false;
		const lastSlash = rest.lastIndexOf("/");
		if (lastSlash <= firstSlash) return false;
		const body = rest.slice(firstSlash + 1, lastSlash);
		const flags = rest.slice(lastSlash + 1);
		try {
			return new RegExp(body, flags).test(value);
		} catch {
			return false;
		}
	}
	if (pattern.includes("*")) {
		// Translate a tiny glob subset into a regex.
		//   `*`  -> any run of non-separator chars
		//   `**` -> any run of chars (including separators)
		// Escape regex metacharacters EXCEPT `*`, then convert `*`/`**`.
		const escaped = pattern.replace(/[.+^${}()|[\]\\]/g, "\\$&");
		let body =
			escaped
				.replace(/\*\*/g, "§§")
				.replace(/\*/g, "[^/]*")
				.replace(/§§/g, ".*");
		// A leading `**/` is treated as optional so e.g. `**/*.ts` matches
		// both `c.ts` and `a/b/c.ts`.
		if (body.startsWith(".*/")) body = "(?:.*/)?" + body.slice(3);
		const re = new RegExp("^" + body + "$");
		return re.test(value);
	}
	return value === pattern;
}

/** Pull a candidate value out of args by key (dot-path or top-level). */
function readArgKey(args: unknown, key: string | undefined): string | undefined {
	if (!key || key === "*") return undefined;
	if (args === null || typeof args !== "object") return undefined;
	const parts = key.split(".");
	let cur: unknown = args;
	for (const p of parts) {
		if (cur === null || typeof cur !== "object") return undefined;
		cur = (cur as Record<string, unknown>)[p];
	}
	if (typeof cur === "string") return cur;
	if (typeof cur === "number" || typeof cur === "boolean") return String(cur);
	return undefined;
}

/** Decide the permission for a tool call. */
export function decide(
	policy: PermissionPolicy,
	req: PermissionRequest,
): { mode: PermissionMode; matchedRule?: PermissionRule; reason?: string } {
	// 1. Tool-level override.
	const toolMode = policy.tools[req.tool.name];
	if (toolMode) {
		return { mode: toolMode, reason: `tool-level policy: ${req.tool.name} -> ${toolMode}` };
	}

	// 2. Per-rule match.
	for (const rule of policy.rules) {
		if (rule.tool !== "*" && rule.tool !== req.tool.name) continue;
		if (!rule.key) {
			return { mode: rule.mode, matchedRule: rule, reason: rule.reason };
		}
		const value = readArgKey(req.args, rule.key);
		if (value !== undefined && matchPattern(value, rule.pattern)) {
			return { mode: rule.mode, matchedRule: rule, reason: rule.reason };
		}
	}

	// 3. Default.
	return { mode: policy.default, reason: "policy default" };
}

/** A single decision in time, optionally with a TTL. */
export interface PermissionDecision {
	mode: PermissionMode;
	/** Optional auto-deny reason. */
	reason?: string;
	/** When the decision was made. */
	at: number;
}

/**
 * A short-lived per-tool cache. Used so a host that re-prompts the user for
 * every call doesn't have to re-ask inside a single turn.
 */
export class PermissionCache {
	private map = new Map<string, PermissionDecision>();

	/** Build a stable cache key for a tool call. */
	static key(toolName: string, args: unknown): string {
		return `${toolName}::${stableStringify(args)}`;
	}

	get(toolName: string, args: unknown): PermissionDecision | undefined {
		return this.map.get(PermissionCache.key(toolName, args));
	}

	put(toolName: string, args: unknown, mode: PermissionMode, reason?: string): void {
		this.map.set(PermissionCache.key(toolName, args), { mode, reason, at: Date.now() });
	}

	clear(): void {
		this.map.clear();
	}
}

/** Tiny stable JSON stringify, used to derive a cache key. */
function stableStringify(value: unknown): string {
	if (value === null || typeof value !== "object") return JSON.stringify(value);
	if (Array.isArray(value)) return `[${value.map(stableStringify).join(",")}]`;
	const obj = value as Record<string, unknown>;
	const keys = Object.keys(obj).sort();
	return `{${keys.map((k) => `${JSON.stringify(k)}:${stableStringify(obj[k])}`).join(",")}}`;
}
