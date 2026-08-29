/**
 * Session persistence for friday-ng.
 *
 * A *session* is a single conversation: the full transcript of messages plus
 * a small metadata header. We persist sessions as a directory under
 * `~/.friday-ng/sessions/<id>/`:
 *
 *   sessions/
 *     <id>/
 *       meta.json     ← { id, createdAt, updatedAt, model, provider, title, ... }
 *       messages.jsonl ← one AgentMessage per line
 *
 * JSONL (one record per line) gives us:
 *  - append-only writes (no rewrite of a huge transcript on every turn)
 *  - crash safety (an interrupted write only loses the last line)
 *  - cheap to inspect with `cat` / `jq`
 *
 * No native dependencies — this scales to millions of lines on every platform
 * with only the Node stdlib.
 */
import { promises as fs } from "node:fs";
import { randomUUID } from "node:crypto";
import os from "node:os";
import path from "node:path";
import type { AgentMessage, Model } from "./types.ts";
import type { ProviderId } from "./types.ts";

/** Environment override so tests / forks can redirect the sessions dir. */
function getSessionsDir(): string {
	return process.env.FRIDAY_NG_SESSIONS_DIR ?? path.join(os.homedir(), ".friday-ng", "sessions");
}

function sessionDir(id: string): string {
	return path.join(getSessionsDir(), id);
}

function metaPath(id: string): string {
	return path.join(sessionDir(id), "meta.json");
}

function messagesPath(id: string): string {
	return path.join(sessionDir(id), "messages.jsonl");
}

export interface SessionMeta {
	/** Stable id (uuid v4 by default). */
	id: string;
	/** First user message (truncated to 100 chars) — used as the session title. */
	title: string;
	/** When the session was first created. ISO-8601 string. */
	createdAt: string;
	/** When the session was last written to. ISO-8601 string. */
	updatedAt: string;
	/** Provider id (e.g. "openai"). */
	provider: ProviderId;
	/** Model id (e.g. "gpt-4o-mini"). */
	model: string;
	/** API style (informational). */
	apiStyle: string;
	/** System prompt — captured at session start so reloads stay consistent. */
	systemPrompt: string;
	/** Number of messages recorded so far. */
	messageCount: number;
	/** Cumulative token usage across the whole session. */
	usage: {
		input: number;
		output: number;
		cacheRead: number;
		cacheWrite: number;
		totalTokens: number;
	};
	/** Number of tool calls made. */
	toolCalls: number;
}

/** A saved session with its transcript. */
export interface SavedSession {
	meta: SessionMeta;
	messages: AgentMessage[];
}

/** Compute a one-line title from a message. */
function deriveTitle(text: string): string {
	const firstLine = text.split("\n", 1)[0]?.trim() ?? "";
	if (firstLine.length <= 100) return firstLine || "(empty)";
	return firstLine.slice(0, 97) + "...";
}

/** Make a fresh session meta. */
export function newSessionMeta(init: {
	provider: ProviderId;
	model: string;
	apiStyle: string;
	systemPrompt: string;
}): SessionMeta {
	const now = new Date().toISOString();
	return {
		id: randomUUID(),
		title: "(new session)",
		createdAt: now,
		updatedAt: now,
		provider: init.provider,
		model: init.model,
		apiStyle: init.apiStyle,
		systemPrompt: init.systemPrompt,
		messageCount: 0,
		usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0 },
		toolCalls: 0,
	};
}

/** Ensure the session directory exists. */
async function ensureSessionDir(id: string): Promise<string> {
	const dir = sessionDir(id);
	await fs.mkdir(dir, { recursive: true, mode: 0o700 });
	return dir;
}

/** Create a brand-new session on disk. Returns the meta (with id). */
export async function createSession(init: {
	provider: ProviderId;
	model: string;
	apiStyle: string;
	systemPrompt: string;
	title?: string;
}): Promise<SessionMeta> {
	const meta = newSessionMeta(init);
	if (init.title) meta.title = init.title;
	await ensureSessionDir(meta.id);
	await writeMeta(meta);
	// Create the messages file (empty).
	await fs.writeFile(messagesPath(meta.id), "", { mode: 0o600 });
	return meta;
}

/** Write the meta.json for a session (atomic temp + rename). */
export async function writeMeta(meta: SessionMeta): Promise<void> {
	const dir = await ensureSessionDir(meta.id);
	const target = metaPath(meta.id);
	const tmp = path.join(dir, `.meta.${process.pid}.tmp`);
	await fs.writeFile(tmp, JSON.stringify(meta, null, 2), { mode: 0o600 });
	await fs.rename(tmp, target);
}

/** Append a single message to a session's JSONL log. */
export async function appendMessage(id: string, message: AgentMessage): Promise<void> {
	const dir = await ensureSessionDir(id);
	const line = JSON.stringify(message) + "\n";
	const target = messagesPath(id);
	const tmp = path.join(dir, `.messages.${process.pid}.${Date.now()}.tmp`);
	// Append via temp + rename for atomicity. We rewrite the file each time
	// because the log is small for typical sessions; for huge transcripts a
	// tail-append would be cheaper, but this keeps semantics simple.
	const existing = await readMessages(id);
	existing.push(message);
	await fs.writeFile(tmp, existing.map((m) => JSON.stringify(m)).join("\n") + (existing.length ? "\n" : ""), {
		mode: 0o600,
	});
	await fs.rename(tmp, target);
}

/** Read every message from a session. Empty array if the file is missing. */
export async function readMessages(id: string): Promise<AgentMessage[]> {
	try {
		const raw = await fs.readFile(messagesPath(id), "utf8");
		const out: AgentMessage[] = [];
		for (const line of raw.split("\n")) {
			const trimmed = line.trim();
			if (!trimmed) continue;
			try {
				out.push(JSON.parse(trimmed) as AgentMessage);
			} catch {
				// Skip corrupted lines so a single bad message doesn't break resume.
			}
		}
		return out;
	} catch (err: any) {
		if (err?.code === "ENOENT") return [];
		throw err;
	}
}

/** Load a session's meta + transcript. */
export async function loadSession(id: string): Promise<SavedSession | undefined> {
	try {
		const metaRaw = await fs.readFile(metaPath(id), "utf8");
		const meta = JSON.parse(metaRaw) as SessionMeta;
		const messages = await readMessages(id);
		return { meta, messages };
	} catch (err: any) {
		if (err?.code === "ENOENT") return undefined;
		throw err;
	}
}

/** Update meta with a new message + totals. Updates title from the first user message. */
export async function recordMessage(
	id: string,
	message: AgentMessage,
	toolCallsAdded = 0,
): Promise<SessionMeta> {
	const meta = await readMeta(id);
	if (!meta) {
		throw new Error(`Session not found: ${id}`);
	}
	// First user message → become the title.
	if (message.role === "user" && meta.title === "(new session)") {
		const text = typeof message.content === "string" ? message.content : "";
		meta.title = deriveTitle(text);
	}
	if (message.role === "assistant") {
		meta.usage.input += message.usage.input;
		meta.usage.output += message.usage.output;
		meta.usage.cacheRead += message.usage.cacheRead;
		meta.usage.cacheWrite += message.usage.cacheWrite;
		meta.usage.totalTokens += message.usage.totalTokens;
	}
	meta.toolCalls += toolCallsAdded;
	meta.messageCount += 1;
	meta.updatedAt = new Date().toISOString();
	await appendMessage(id, message);
	await writeMeta(meta);
	return meta;
}

/** Update only the meta (e.g. to record a model switch). */
export async function updateMeta(
	id: string,
	patch: Partial<Pick<SessionMeta, "model" | "provider" | "title" | "apiStyle" | "systemPrompt">>,
): Promise<SessionMeta> {
	const meta = await readMeta(id);
	if (!meta) {
		throw new Error(`Session not found: ${id}`);
	}
	const next: SessionMeta = { ...meta, ...patch, updatedAt: new Date().toISOString() };
	await writeMeta(next);
	return next;
}

/** Read the meta.json for a session. */
export async function readMeta(id: string): Promise<SessionMeta | undefined> {
	try {
		const raw = await fs.readFile(metaPath(id), "utf8");
		return JSON.parse(raw) as SessionMeta;
	} catch (err: any) {
		if (err?.code === "ENOENT") return undefined;
		throw err;
	}
}

/** List every saved session id (sorted newest first by updatedAt). */
export async function listSessions(): Promise<SessionMeta[]> {
	const dir = getSessionsDir();
	let entries: string[];
	try {
		entries = await fs.readdir(dir);
	} catch (err: any) {
		if (err?.code === "ENOENT") return [];
		throw err;
	}
	const metas: SessionMeta[] = [];
	for (const entry of entries) {
		const meta = await readMeta(entry);
		if (meta) metas.push(meta);
	}
	metas.sort((a, b) => (a.updatedAt < b.updatedAt ? 1 : a.updatedAt > b.updatedAt ? -1 : 0));
	return metas;
}

/** Delete a session directory. Returns true if the session existed. */
export async function deleteSession(id: string): Promise<boolean> {
	const dir = sessionDir(id);
	try {
		await fs.stat(dir);
	} catch (err: any) {
		if (err?.code === "ENOENT") return false;
		throw err;
	}
	await fs.rm(dir, { recursive: true, force: true });
	return true;
}

/** Convenience: get the directory used for session storage (for display). */
export function getSessionsDirPath(): string {
	return getSessionsDir();
}
