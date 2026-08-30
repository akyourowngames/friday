import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { promises as fs } from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import {
	appendMessage,
	createSession,
	deleteSession,
	getSessionsDirPath,
	listSessions,
	loadSession,
	newSessionMeta,
	readMeta,
	readMessages,
	recordMessage,
	replaceSessionMessages,
	updateMeta,
	writeMeta,
	type SessionMeta,
} from "../src/sessions.ts";
import type { AgentMessage } from "../src/types.ts";

describe("sessions", () => {
	let tmp: string;
	beforeEach(async () => {
		tmp = path.join(os.tmpdir(), `friday-ng-sessions-${Date.now()}-${Math.random()}`);
		process.env.FRIDAY_NG_SESSIONS_DIR = tmp;
	});
	afterEach(async () => {
		delete process.env.FRIDAY_NG_SESSIONS_DIR;
		await fs.rm(tmp, { recursive: true, force: true });
	});

	it("createSession writes meta + empty messages file", async () => {
		const meta = await createSession({
			provider: "openai",
			model: "gpt-4o-mini",
			apiStyle: "openai",
			systemPrompt: "be brief",
		});
		expect(meta.id).toMatch(/[0-9a-f-]{36}/);
		expect(await fs.stat(path.join(tmp, meta.id, "meta.json"))).toBeDefined();
		expect(await fs.stat(path.join(tmp, meta.id, "messages.jsonl"))).toBeDefined();
	});

	it("appendMessage writes JSONL records in order", async () => {
		const meta = await createSession({
			provider: "openai",
			model: "gpt-4o-mini",
			apiStyle: "openai",
			systemPrompt: "x",
		});
		const m1: AgentMessage = { role: "user", content: "hi", timestamp: 1 };
		const m2: AgentMessage = {
			role: "assistant",
			content: [{ type: "text", text: "hello" }],
			api: "openai",
			provider: "openai",
			model: "gpt-4o-mini",
			usage: {
				input: 1, output: 2, cacheRead: 0, cacheWrite: 0, totalTokens: 3,
				cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
			},
			stopReason: "stop",
			timestamp: 2,
		};
		await appendMessage(meta.id, m1);
		await appendMessage(meta.id, m2);
		const read = await readMessages(meta.id);
		expect(read).toHaveLength(2);
		expect(read[0]).toEqual(m1);
		expect(read[1]).toEqual(m2);
	});

	it("recordMessage updates meta with usage + title from first user message", async () => {
		const meta = await createSession({
			provider: "anthropic",
			model: "claude-3-5-sonnet-latest",
			apiStyle: "anthropic",
			systemPrompt: "x",
		});
		const userMsg: AgentMessage = {
			role: "user",
			content: "Tell me a joke about TypeScript",
			timestamp: 1,
		};
		const updated = await recordMessage(meta.id, userMsg);
		expect(updated.title).toBe("Tell me a joke about TypeScript");
		expect(updated.messageCount).toBe(1);

		const assistantMsg: AgentMessage = {
			role: "assistant",
			content: [{ type: "text", text: "Here's one..." }],
			api: "anthropic",
			provider: "anthropic",
			model: "claude-3-5-sonnet-latest",
			usage: {
				input: 10, output: 20, cacheRead: 0, cacheWrite: 0, totalTokens: 30,
				cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
			},
			stopReason: "stop",
			timestamp: 2,
		};
		const updated2 = await recordMessage(meta.id, assistantMsg, 0);
		expect(updated2.usage.input).toBe(10);
		expect(updated2.usage.output).toBe(20);
		expect(updated2.usage.totalTokens).toBe(30);
		expect(updated2.messageCount).toBe(2);
	});

	it("readMessages returns [] for a session with no messages", async () => {
		const meta = await createSession({
			provider: "faux",
			model: "faux-1",
			apiStyle: "faux",
			systemPrompt: "x",
		});
		expect(await readMessages(meta.id)).toEqual([]);
	});

	it("listSessions returns every session sorted by updatedAt desc", async () => {
		const a = await createSession({ provider: "openai", model: "gpt-4o-mini", apiStyle: "openai", systemPrompt: "x" });
		const b = await createSession({ provider: "anthropic", model: "claude-3-5-sonnet-latest", apiStyle: "anthropic", systemPrompt: "x" });
		await recordMessage(a.id, { role: "user", content: "hi", timestamp: 1 });
		await new Promise((r) => setTimeout(r, 5));
		await recordMessage(b.id, { role: "user", content: "later", timestamp: 2 });
		const all = await listSessions();
		expect(all.length).toBeGreaterThanOrEqual(2);
		// Most recently updated should be first.
		expect(all[0]!.updatedAt >= all[1]!.updatedAt).toBe(true);
	});

	it("loadSession returns meta + messages", async () => {
		const meta = await createSession({ provider: "openai", model: "gpt-4o-mini", apiStyle: "openai", systemPrompt: "x" });
		await recordMessage(meta.id, { role: "user", content: "hi", timestamp: 1 });
		const loaded = await loadSession(meta.id);
		expect(loaded?.meta.id).toBe(meta.id);
		expect(loaded?.messages).toHaveLength(1);
	});

	it("loadSession returns undefined for unknown id", async () => {
		expect(await loadSession("does-not-exist")).toBeUndefined();
	});

	it("updateMeta patches model/provider/title", async () => {
		const meta = await createSession({ provider: "openai", model: "gpt-4o-mini", apiStyle: "openai", systemPrompt: "x" });
		const next = await updateMeta(meta.id, { model: "gpt-4o", title: "New title" });
		expect(next.model).toBe("gpt-4o");
		expect(next.title).toBe("New title");
		const reloaded = await readMeta(meta.id);
		expect(reloaded?.model).toBe("gpt-4o");
	});

	it("deleteSession removes the directory", async () => {
		const meta = await createSession({ provider: "openai", model: "gpt-4o-mini", apiStyle: "openai", systemPrompt: "x" });
		expect(await deleteSession(meta.id)).toBe(true);
		expect(await loadSession(meta.id)).toBeUndefined();
		expect(await deleteSession(meta.id)).toBe(false);
	});

	it("readMessages skips corrupted lines", async () => {
		const meta = await createSession({ provider: "openai", model: "gpt-4o-mini", apiStyle: "openai", systemPrompt: "x" });
		await appendMessage(meta.id, { role: "user", content: "hi", timestamp: 1 });
		// Manually corrupt the file by adding a bad line.
		const p = path.join(tmp, meta.id, "messages.jsonl");
		const existing = await fs.readFile(p, "utf8");
		await fs.writeFile(p, existing + "not-json\n");
		const read = await readMessages(meta.id);
		expect(read).toHaveLength(1); // corrupted line skipped
	});

	it("getSessionsDirPath reflects env override", () => {
		expect(getSessionsDirPath()).toBe(tmp);
	});

	it("newSessionMeta has all required fields", () => {
		const meta = newSessionMeta({
			provider: "openai",
			model: "gpt-4o-mini",
			apiStyle: "openai",
			systemPrompt: "x",
		});
		expect(meta.id).toBeTruthy();
		expect(meta.createdAt).toBeTruthy();
		expect(meta.updatedAt).toBeTruthy();
		expect(meta.messageCount).toBe(0);
		expect(meta.usage.totalTokens).toBe(0);
		expect(meta.toolCalls).toBe(0);
	});
});
