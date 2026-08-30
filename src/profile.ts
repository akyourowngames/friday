import { promises as fs } from "node:fs";
import os from "node:os";
import path from "node:path";
import { randomUUID } from "node:crypto";

export const PROFILE_FILENAME = "PROFILE.md";
export const PROJECT_FILENAME = "AGENTS.md";

export function profileDir(): string {
	return path.resolve(process.env.FRIDAY_NG_CONFIG_DIR ?? path.join(os.homedir(), ".friday-ng"));
}

export function profilePath(): string {
	return path.join(profileDir(), PROFILE_FILENAME);
}

export async function atomicUpdateFile(
	target: string,
	update: string | Buffer | ((current: Buffer | undefined) => string | Buffer | undefined | Promise<string | Buffer | undefined>),
	options: { mode?: number } = {},
): Promise<boolean> {
	const absolute = path.resolve(target);
	const dir = path.dirname(absolute);
	await fs.mkdir(dir, { recursive: true, mode: 0o700 });
	let current: Buffer | undefined;
	try {
		current = await fs.readFile(absolute);
	} catch (error: any) {
		if (error?.code !== "ENOENT") throw error;
	}
	const next = typeof update === "function" ? await update(current) : update;
	if (next === undefined) return false;
	const temporary = path.join(dir, `.${path.basename(absolute)}.${process.pid}.${randomUUID()}.tmp`);
	try {
		await fs.writeFile(temporary, next, { mode: options.mode ?? 0o600, flag: "wx" });
		await fs.rename(temporary, absolute);
		try {
			await fs.chmod(absolute, options.mode ?? 0o600);
		} catch {}
	} finally {
		await fs.rm(temporary, { force: true }).catch(() => undefined);
	}
	return true;
}

export async function atomicAppendFile(
	target: string,
	content: string | Buffer,
	options: { mode?: number } = {},
): Promise<void> {
	await atomicUpdateFile(target, (current) => Buffer.concat([current ?? Buffer.alloc(0), Buffer.from(content)]), options);
}

export async function loadProfile(): Promise<string | undefined> {
	try {
		return await fs.readFile(profilePath(), "utf8");
	} catch (error: any) {
		if (error?.code === "ENOENT") return undefined;
		throw error;
	}
}

export async function profileExists(): Promise<boolean> {
	try {
		return (await fs.stat(profilePath())).isFile();
	} catch (error: any) {
		if (error?.code === "ENOENT") return false;
		throw error;
	}
}

export async function updateProfile(content: string): Promise<void> {
	await atomicUpdateFile(profilePath(), content);
}

export async function appendProfile(content: string): Promise<void> {
	await atomicAppendFile(profilePath(), content);
}

export async function loadProjectFile(
	workspace = process.cwd(),
	fileName = PROJECT_FILENAME,
): Promise<string | undefined> {
	const root = path.resolve(workspace);
	const target = path.resolve(root, fileName);
	const relative = path.relative(root, target);
	if (relative.startsWith("..") || path.isAbsolute(relative)) throw new Error("Project file must be inside the workspace");
	try {
		const stat = await fs.lstat(target);
		if (!stat.isFile() || stat.isSymbolicLink()) return undefined;
		return await fs.readFile(target, "utf8");
	} catch (error: any) {
		if (error?.code === "ENOENT") return undefined;
		throw error;
	}
}

export async function updateProjectFile(workspace: string, content: string, fileName = PROJECT_FILENAME): Promise<void> {
	const root = path.resolve(workspace);
	const target = path.resolve(root, fileName);
	const relative = path.relative(root, target);
	if (relative.startsWith("..") || path.isAbsolute(relative)) throw new Error("Project file must be inside the workspace");
	await atomicUpdateFile(target, content);
}

export async function appendProjectFile(workspace: string, content: string, fileName = PROJECT_FILENAME): Promise<void> {
	const root = path.resolve(workspace);
	const target = path.resolve(root, fileName);
	const relative = path.relative(root, target);
	if (relative.startsWith("..") || path.isAbsolute(relative)) throw new Error("Project file must be inside the workspace");
	await atomicAppendFile(target, content);
}
