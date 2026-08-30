import { promises as fs } from "node:fs";
import os from "node:os";
import path from "node:path";
import { randomUUID } from "node:crypto";
import { Type } from "typebox";
import type { Tool } from "./types.ts";
import { atomicUpdateFile } from "./profile.ts";

export const TODO_STORE_VERSION = 1 as const;
export const TODO_STATUSES = ["pending", "in_progress", "done"] as const;
export type TodoStatus = (typeof TODO_STATUSES)[number];

export interface TodoItem {
	id: string;
	content: string;
	status: TodoStatus;
	createdAt: string;
	updatedAt: string;
}

export interface TodoStore {
	version: typeof TODO_STORE_VERSION;
	revision: number;
	createdAt: string;
	updatedAt: string;
	items: TodoItem[];
}

export type TodoInput = string | { content: string; status?: TodoStatus };

export function validateSessionId(sessionId: string): string {
	if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(sessionId) || sessionId === "." || sessionId === "..") {
		throw new Error(`Invalid session id: ${sessionId}`);
	}
	return sessionId;
}

export function sessionsDir(): string {
	return path.resolve(process.env.FRIDAY_NG_SESSIONS_DIR ?? path.join(os.homedir(), ".friday-ng", "sessions"));
}

export function todoPath(sessionId: string): string {
	return path.join(sessionsDir(), validateSessionId(sessionId), "todos.json");
}

function timestamp(): string {
	return new Date().toISOString();
}

export function emptyTodoStore(now = timestamp()): TodoStore {
	return { version: TODO_STORE_VERSION, revision: 0, createdAt: now, updatedAt: now, items: [] };
}

function parseStore(value: unknown): TodoStore {
	if (!value || typeof value !== "object") throw new Error("Invalid todo store");
	const candidate = value as Record<string, unknown>;
	if (candidate.version !== TODO_STORE_VERSION || !Number.isSafeInteger(candidate.revision) || (candidate.revision as number) < 0) {
		throw new Error("Unsupported or invalid todo store");
	}
	if (typeof candidate.createdAt !== "string" || typeof candidate.updatedAt !== "string" || !Array.isArray(candidate.items)) {
		throw new Error("Invalid todo store");
	}
	const ids = new Set<string>();
	const items = candidate.items.map((value) => {
		if (!value || typeof value !== "object") throw new Error("Invalid todo item");
		const item = value as Record<string, unknown>;
		if (
			typeof item.id !== "string" || !/^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(item.id) ||
			typeof item.content !== "string" || !item.content.trim() ||
			!TODO_STATUSES.includes(item.status as TodoStatus) ||
			typeof item.createdAt !== "string" || typeof item.updatedAt !== "string" || ids.has(item.id)
		) throw new Error("Invalid todo item");
		ids.add(item.id);
		return item as unknown as TodoItem;
	});
	return { version: TODO_STORE_VERSION, revision: candidate.revision as number, createdAt: candidate.createdAt, updatedAt: candidate.updatedAt, items };
}

export async function loadTodos(sessionId: string): Promise<TodoStore> {
	const target = todoPath(sessionId);
	try {
		return parseStore(JSON.parse(await fs.readFile(target, "utf8")));
	} catch (error: any) {
		if (error?.code === "ENOENT") return emptyTodoStore();
		throw error;
	}
}

export async function saveTodos(sessionId: string, store: TodoStore, expectedRevision?: number): Promise<TodoStore> {
	const target = todoPath(sessionId);
	let saved!: TodoStore;
	await atomicUpdateFile(target, (current) => {
		const existing = current ? parseStore(JSON.parse(current.toString("utf8"))) : undefined;
		const actualRevision = existing?.revision ?? 0;
		if (expectedRevision !== undefined && expectedRevision !== actualRevision) {
			throw new Error(`Todo revision conflict: expected ${expectedRevision}, found ${actualRevision}`);
		}
		const now = timestamp();
		saved = parseStore({
			...store,
			version: TODO_STORE_VERSION,
			revision: actualRevision + 1,
			createdAt: existing?.createdAt ?? store.createdAt ?? now,
			updatedAt: now,
		});
		return JSON.stringify(saved, null, 2) + "\n";
	});
	return saved;
}

export async function updateTodos(
	sessionId: string,
	update: (items: readonly TodoItem[], store: Readonly<TodoStore>) => readonly TodoItem[] | Promise<readonly TodoItem[]>,
): Promise<TodoStore> {
	const target = todoPath(sessionId);
	let saved!: TodoStore;
	await atomicUpdateFile(target, async (current) => {
		const existing = current ? parseStore(JSON.parse(current.toString("utf8"))) : emptyTodoStore();
		const items = await update(existing.items.map((item) => ({ ...item })), existing);
		const now = timestamp();
		saved = parseStore({ ...existing, revision: existing.revision + 1, updatedAt: now, items });
		return JSON.stringify(saved, null, 2) + "\n";
	});
	return saved;
}

export async function addTodo(sessionId: string, input: TodoInput): Promise<TodoItem> {
	let added!: TodoItem;
	await updateTodos(sessionId, (items) => {
		const content = typeof input === "string" ? input : input.content;
		const status = typeof input === "string" ? "pending" : input.status ?? "pending";
		if (!content.trim() || !TODO_STATUSES.includes(status)) throw new Error("Invalid todo input");
		const now = timestamp();
		added = { id: randomUUID(), content, status, createdAt: now, updatedAt: now };
		return [...items, added];
	});
	return added;
}

export async function setTodoStatus(sessionId: string, id: string, status: TodoStatus): Promise<TodoItem> {
	if (!TODO_STATUSES.includes(status)) throw new Error(`Invalid todo status: ${status}`);
	let changed: TodoItem | undefined;
	await updateTodos(sessionId, (items) => items.map((item) => {
		if (item.id !== id) return item;
		changed = { ...item, status, updatedAt: timestamp() };
		return changed;
	}));
	if (!changed) throw new Error(`Todo not found: ${id}`);
	return changed;
}

export async function removeTodo(sessionId: string, id: string): Promise<boolean> {
	let removed = false;
	await updateTodos(sessionId, (items) => items.filter((item) => {
		if (item.id === id) removed = true;
		return item.id !== id;
	}));
	return removed;
}

const TodoWriteParameters = Type.Object({
	items: Type.Array(Type.Object({
		content: Type.String({ minLength: 1 }),
		status: Type.Union(TODO_STATUSES.map((status) => Type.Literal(status))),
	})),
});

export function makeTodoWriteTool(deps: {
	getSessionId: () => string;
	onChange?: (store: TodoStore) => Promise<void> | void;
}): Tool<typeof TodoWriteParameters> {
	return {
		name: "todoWrite",
		description: "Replace the current session checklist with an ordered list of todo items and statuses.",
		parameters: TodoWriteParameters,
		executionMode: "sequential",
		execute: async (_id, params) => {
			const sessionId = deps.getSessionId();
			const current = await loadTodos(sessionId);
			const now = timestamp();
			const items = params.items.map((item, index) => {
				const previous = current.items[index];
				return {
					id: previous?.id ?? randomUUID(),
					content: item.content.trim(),
					status: item.status,
					createdAt: previous?.createdAt ?? now,
					updatedAt: now,
				};
			});
			if (items.some((item) => !item.content)) throw new Error("Todo content cannot be empty");
			const saved = await saveTodos(sessionId, { ...current, items }, current.revision);
			await deps.onChange?.(saved);
			return {
				content: [{ type: "text", text: `Updated ${saved.items.length} todo item${saved.items.length === 1 ? "" : "s"}.` }],
				details: saved,
			};
		},
	};
}
