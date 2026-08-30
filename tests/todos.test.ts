import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { promises as fs } from "node:fs";
import os from "node:os";
import path from "node:path";
import { addTodo, emptyTodoStore, loadTodos, makeTodoWriteTool, removeTodo, saveTodos, setTodoStatus, todoPath, updateTodos, validateSessionId } from "../src/todos.ts";

describe("session todos", () => {
	let root: string;
	beforeEach(async () => {
		root = await fs.mkdtemp(path.join(os.tmpdir(), "friday-todos-"));
		process.env.FRIDAY_NG_SESSIONS_DIR = root;
	});
	afterEach(async () => {
		delete process.env.FRIDAY_NG_SESSIONS_DIR;
		await fs.rm(root, { recursive: true, force: true });
	});

	it("validates session ids and resolves the overridden path", () => {
		expect(todoPath("session-1")).toBe(path.join(root, "session-1", "todos.json"));
		expect(validateSessionId("abc_123.x")).toBe("abc_123.x");
		for (const id of ["", "..", "../escape", "a/b", "a\\b", ".hidden"]) expect(() => validateSessionId(id)).toThrow();
	});

	it("creates UUID items, preserves order, statuses, revisions and timestamps", async () => {
		const first = await addTodo("s1", "first");
		const second = await addTodo("s1", { content: "second", status: "in_progress" });
		expect(first.id).toMatch(/^[0-9a-f-]{36}$/);
		expect(second.status).toBe("in_progress");
		await setTodoStatus("s1", first.id, "done");
		const store = await loadTodos("s1");
		expect(store.version).toBe(1);
		expect(store.revision).toBe(3);
		expect(store.items.map((item) => [item.content, item.status])).toEqual([["first", "done"], ["second", "in_progress"]]);
		expect(Date.parse(store.createdAt)).not.toBeNaN();
		expect(Date.parse(store.updatedAt)).not.toBeNaN();
	});

	it("updates atomically and supports removal", async () => {
		const item = await addTodo("s1", "remove me");
		await updateTodos("s1", (items) => [...items, { ...item, id: "b38e9eb4-a56c-4e9a-890c-c2bcbd5225a5", content: "added" }]);
		expect(await removeTodo("s1", item.id)).toBe(true);
		expect(await removeTodo("s1", item.id)).toBe(false);
		expect((await loadTodos("s1")).items.map((todo) => todo.content)).toEqual(["added"]);
	});

	it("detects optimistic revision conflicts and rejects malformed stores", async () => {
		const initial = emptyTodoStore("2025-01-01T00:00:00.000Z");
		const saved = await saveTodos("s1", initial, 0);
		expect(saved.revision).toBe(1);
		await expect(saveTodos("s1", saved, 0)).rejects.toThrow("revision conflict");
		await fs.writeFile(todoPath("bad"), "{\"version\":999}", { recursive: false }).catch(async () => {
			await fs.mkdir(path.dirname(todoPath("bad")), { recursive: true });
			await fs.writeFile(todoPath("bad"), "{\"version\":999}");
		});
		await expect(loadTodos("bad")).rejects.toThrow("todo store");
	});
});
