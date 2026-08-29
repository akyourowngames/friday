/**
 * Lifecycle hooks for friday-ng.
 *
 * Modeled after Pi Coding Agent's hooks system. A hook is a callback that
 * fires at a well-known point in the agent's lifecycle, with the option to
 * short-circuit, transform input/output, or veto a call.
 *
 * Hooks are registered against named events. Each event carries a typed
 * payload. A hook can:
 *   - observe (just return)
 *   - veto (return `{ cancel: true, reason: "..." }`)
 *   - transform (return `{ ...payload, field: newValue }`)
 *
 * Multiple hooks per event run in registration order. If any hook vetoes,
 * the rest are skipped.
 */
import type { AgentMessage, AgentTool, ToolResult } from "./types.ts";

/** A tool call about to be made. */
export interface PreToolUsePayload {
	tool: AgentTool<any, any>;
	args: unknown;
	callId: string;
}

/** A tool call that just finished. */
export interface PostToolUsePayload {
	tool: AgentTool<any, any>;
	args: unknown;
	callId: string;
	result: ToolResult;
	durationMs: number;
}

/** A user message about to be sent to the model. */
export interface PreUserMessagePayload {
	text: string;
}

/** An assistant message that just finished streaming. */
export interface PostAssistantMessagePayload {
	message: AgentMessage;
}

/** A model call about to be made (after the tool loop has assembled messages). */
export interface PreModelCallPayload {
	messages: AgentMessage[];
	systemPrompt: string;
	model: string;
}

/** A model call that just returned. */
export interface PostModelCallPayload {
	messages: AgentMessage[];
	usage?: { input: number; output: number; total: number };
}

/** A turn that just ended. */
export interface TurnEndPayload {
	messages: AgentMessage[];
	stopReason: "complete" | "tool_use" | "aborted" | "error";
	error?: Error;
}

export type HookPayloads = {
	"pre_tool_use": PreToolUsePayload;
	"post_tool_use": PostToolUsePayload;
	"pre_user_message": PreUserMessagePayload;
	"post_assistant_message": PostAssistantMessagePayload;
	"pre_model_call": PreModelCallPayload;
	"post_model_call": PostModelCallPayload;
	"turn_end": TurnEndPayload;
};

export type HookEvent = keyof HookPayloads;

export type HookResult<K extends HookEvent> = HookPayloads[K] & {
	/** If true, the action that would have happened is cancelled. */
	cancel?: boolean;
	/** When cancelling, a human reason to show. */
	reason?: string;
};

export type Hook<K extends HookEvent> = (
	payload: HookPayloads[K],
) => HookResult<K> | void | Promise<HookResult<K> | void>;

/**
 * Registry of lifecycle hooks. Thread-safe in the sense that hook firing is
 * sequential; concurrent calls to `trigger` are awaited in order.
 */
export class HookRegistry {
	private hooks: Map<HookEvent, Array<(payload: any) => any>> = new Map();

	/** Register a hook for an event. Returns an unregister function. */
	on<K extends HookEvent>(event: K, hook: Hook<K>): () => void {
		let list = this.hooks.get(event);
		if (!list) {
			list = [];
			this.hooks.set(event, list);
		}
		list.push(hook as any);
		return () => {
			if (!list) return;
			const idx = list.indexOf(hook as any);
			if (idx >= 0) list.splice(idx, 1);
		};
	}

	/** Remove every hook. Useful in tests. */
	clear(): void {
		this.hooks.clear();
	}

	/** Fire all hooks for an event in order. If any hook vetoes, stop. */
	async trigger<K extends HookEvent>(
		event: K,
		payload: HookPayloads[K],
	): Promise<HookResult<K>> {
		const list = (this.hooks.get(event) ?? []) as Hook<K>[];
		let cur: HookResult<K> = { ...(payload as any) };
		for (const hook of list) {
			const result = await (hook as Hook<K>)(cur as HookPayloads[K]);
			if (result && typeof result === "object") {
				cur = { ...cur, ...(result as object) } as HookResult<K>;
			}
			if (cur.cancel) {
				return cur;
			}
		}
		return cur;
	}

	/** Count of registered hooks for an event (test helper). */
	count<K extends HookEvent>(event: K): number {
		return (this.hooks.get(event) ?? []).length;
	}
}
