import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { promises as fs } from "node:fs";
import os from "node:os";
import path from "node:path";
import { createCheckpoint, finalizeCheckpoint, listCheckpoints, loadCheckpoint, restoreCheckpoint } from "../src/checkpoints.ts";

describe("checkpoints", () => {
	let root: string;
	let workspace: string;
	beforeEach(async () => {
		root = await fs.mkdtemp(path.join(os.tmpdir(), "friday-checkpoint-"));
		workspace = path.join(root, "workspace");
		await fs.mkdir(workspace);
		process.env.FRIDAY_NG_SESSIONS_DIR = path.join(root, "sessions");
	});
	afterEach(async () => {
		delete process.env.FRIDAY_NG_SESSIONS_DIR;
		await fs.rm(root, { recursive: true, force: true });
	});

	it("captures declared missing, empty, binary and mode-preserving files", async () => {
		await fs.writeFile(path.join(workspace, "empty"), "");
		await fs.writeFile(path.join(workspace, "binary"), Buffer.from([0, 255, 1]));
		await fs.writeFile(path.join(workspace, "script"), "echo hi\n", { mode: 0o755 });
		const pending = await createCheckpoint({ sessionId: "s1", workspace, files: ["empty", "binary", "script", "new-file"], todo: { revision: 2 }, transcript: { offset: 42 } });
		expect(pending.status).toBe("pending");
		expect(pending.todo).toEqual({ revision: 2 });
		expect(pending.transcript).toEqual({ offset: 42 });
		const finalized = await finalizeCheckpoint("s1", pending.id);
		expect(finalized.status).toBe("finalized");
		await fs.writeFile(path.join(workspace, "empty"), "not empty");
		await fs.writeFile(path.join(workspace, "binary"), Buffer.from([9]));
		await fs.writeFile(path.join(workspace, "new-file"), "created later");
		await fs.rm(path.join(workspace, "script"));
		const restored = await restoreCheckpoint({ sessionId: "s1", workspace, checkpointId: pending.id });
		expect(await fs.readFile(path.join(workspace, "empty"))).toEqual(Buffer.alloc(0));
		expect(await fs.readFile(path.join(workspace, "binary"))).toEqual(Buffer.from([0, 255, 1]));
		expect(await fs.readFile(path.join(workspace, "script"), "utf8")).toBe("echo hi\n");
		expect((await fs.stat(path.join(workspace, "script"))).mode & 0o777).toBe(process.platform === "win32" ? 0o666 : 0o755);
		await expect(fs.stat(path.join(workspace, "new-file"))).rejects.toMatchObject({ code: "ENOENT" });
		expect(restored.deleted).toContain("new-file");
	});

	it("takes a bounded workspace snapshot, excludes unsafe trees and removes later files", async () => {
		await fs.mkdir(path.join(workspace, "src"));
		await fs.mkdir(path.join(workspace, "node_modules"));
		await fs.writeFile(path.join(workspace, "src", "a.txt"), "before");
		await fs.writeFile(path.join(workspace, "node_modules", "skip"), "skip");
		const pending = await createCheckpoint({ sessionId: "s1", workspace, workspaceSnapshot: true, maxFiles: 5, maxBytes: 100 });
		await finalizeCheckpoint("s1", pending.id);
		await fs.writeFile(path.join(workspace, "src", "a.txt"), "after");
		await fs.writeFile(path.join(workspace, "src", "new.txt"), "new");
		await fs.writeFile(path.join(workspace, "node_modules", "skip"), "changed but excluded");
		await restoreCheckpoint({ sessionId: "s1", workspace });
		expect(await fs.readFile(path.join(workspace, "src", "a.txt"), "utf8")).toBe("before");
		await expect(fs.stat(path.join(workspace, "src", "new.txt"))).rejects.toMatchObject({ code: "ENOENT" });
		expect(await fs.readFile(path.join(workspace, "node_modules", "skip"), "utf8")).toBe("changed but excluded");
	});

	it("restores finalized checkpoints in LIFO order once", async () => {
		await fs.writeFile(path.join(workspace, "value"), "zero");
		const first = await createCheckpoint({ sessionId: "s1", workspace, files: ["value"] });
		await finalizeCheckpoint("s1", first.id);
		await new Promise((resolve) => setTimeout(resolve, 5));
		await fs.writeFile(path.join(workspace, "value"), "one");
		const second = await createCheckpoint({ sessionId: "s1", workspace, files: ["value"] });
		await finalizeCheckpoint("s1", second.id);
		await fs.writeFile(path.join(workspace, "value"), "two");
		expect((await restoreCheckpoint({ sessionId: "s1", workspace })).manifest.id).toBe(second.id);
		expect(await fs.readFile(path.join(workspace, "value"), "utf8")).toBe("one");
		expect((await restoreCheckpoint({ sessionId: "s1", workspace })).manifest.id).toBe(first.id);
		expect(await fs.readFile(path.join(workspace, "value"), "utf8")).toBe("zero");
		await expect(restoreCheckpoint({ sessionId: "s1", workspace })).rejects.toThrow("No restorable");
		expect((await listCheckpoints("s1"))).toHaveLength(2);
		expect((await loadCheckpoint("s1", first.id))?.restoredAt).toBeTruthy();
	});

	it("enforces containment and snapshot limits", async () => {
		await fs.writeFile(path.join(root, "outside"), "x");
		await expect(createCheckpoint({ sessionId: "s1", workspace, files: ["../outside"] })).rejects.toThrow("outside the workspace");
		await fs.writeFile(path.join(workspace, "large"), "12345");
		await expect(createCheckpoint({ sessionId: "s1", workspace, workspaceSnapshot: true, maxBytes: 4 })).rejects.toThrow("maximum size");
		await expect(createCheckpoint({ sessionId: "../bad", workspace, files: ["large"] })).rejects.toThrow("Invalid session id");
	});
});
