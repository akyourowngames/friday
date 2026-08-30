import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { promises as fs } from "node:fs";
import os from "node:os";
import path from "node:path";
import { appendProfile, atomicAppendFile, atomicUpdateFile, loadProfile, loadProjectFile, profileDir, profileExists, profilePath, updateProfile } from "../src/profile.ts";

describe("profile persistence", () => {
	let root: string;
	beforeEach(async () => {
		root = await fs.mkdtemp(path.join(os.tmpdir(), "friday-profile-"));
		process.env.FRIDAY_NG_CONFIG_DIR = path.join(root, "config");
	});
	afterEach(async () => {
		delete process.env.FRIDAY_NG_CONFIG_DIR;
		await fs.rm(root, { recursive: true, force: true });
	});

	it("respects config override and handles a missing profile", async () => {
		expect(profileDir()).toBe(path.join(root, "config"));
		expect(profilePath()).toBe(path.join(root, "config", "PROFILE.md"));
		expect(await profileExists()).toBe(false);
		expect(await loadProfile()).toBeUndefined();
	});

	it("atomically updates and appends profile content", async () => {
		await updateProfile("alpha");
		await appendProfile(" beta");
		expect(await loadProfile()).toBe("alpha beta");
		expect(await profileExists()).toBe(true);
		expect((await fs.readdir(profileDir())).some((name) => name.endsWith(".tmp"))).toBe(false);
	});

	it("supports buffer-safe generic atomic helpers", async () => {
		const target = path.join(root, "nested", "data.bin");
		await atomicUpdateFile(target, Buffer.from([0, 1]));
		await atomicAppendFile(target, Buffer.from([2, 255]));
		expect(await fs.readFile(target)).toEqual(Buffer.from([0, 1, 2, 255]));
		const changed = await atomicUpdateFile(target, () => undefined);
		expect(changed).toBe(false);
	});

	it("loads only contained regular project files", async () => {
		const workspace = path.join(root, "work");
		await fs.mkdir(workspace);
		await fs.writeFile(path.join(workspace, "AGENTS.md"), "project rules");
		expect(await loadProjectFile(workspace)).toBe("project rules");
		expect(await loadProjectFile(path.join(root, "missing"))).toBeUndefined();
		await expect(loadProjectFile(workspace, "../outside.md")).rejects.toThrow("inside the workspace");
	});
});
