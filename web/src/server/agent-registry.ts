import "server-only";
import type { Agent } from "@/src/agent";

/**
 * Per-process registry of live Agent instances keyed by sessionId.
 * One entry per active session — the next /api/chat for the same session
 * reuses the live agent (so multi-turn conversations keep their message
 * history and any in-flight state).
 */
const registry = new Map<string, Agent>();

export function getAgent(sessionId: string): Agent | undefined {
	return registry.get(sessionId);
}

export function registerAgent(sessionId: string, agent: Agent): void {
	registry.set(sessionId, agent);
}

export function unregisterAgent(sessionId: string): void {
	registry.delete(sessionId);
}

export function abortSession(sessionId: string): boolean {
	const agent = registry.get(sessionId);
	if (!agent) return false;
	agent.abort();
	return true;
}
