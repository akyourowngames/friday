import { createHash, randomUUID } from "node:crypto";
import { promises as fs } from "node:fs";
import os from "node:os";
import path from "node:path";
import { atomicUpdateFile } from "./profile.ts";
import { sessionsDir, validateSessionId } from "./todos.ts";

export const CHECKPOINT_VERSION = 1 as const;
export type CheckpointStatus = "pending" | "finalized";

export interface CheckpointEntry {
	path: string;
	type: "file" | "missing";
	mode?: number;
	size?: number;
	sha256?: string;
	blob?: string;
}

export interface CheckpointManifest {
	version: typeof CHECKPOINT_VERSION;
	id: string;
	sessionId: string;
	workspace: string;
	status: CheckpointStatus;
	createdAt: string;
	finalizedAt?: string;
	restoredAt?: string;
	scope: "declared" | "workspace";
	toolCallId?: string;
	toolName?: string;
	entries: CheckpointEntry[];
	exclusions: string[];
	todo?: unknown;
	transcript?: unknown;
}

export interface CreateCheckpointOptions {
	sessionId: string;
	workspace: string;
	files?: readonly string[];
	workspaceSnapshot?: boolean;
	exclude?: readonly string[];
	maxFiles?: number;
	maxBytes?: number;
	toolCallId?: string;
	toolName?: string;
	todo?: unknown;
	transcript?: unknown;
}

export interface RestoreCheckpointOptions {
	sessionId: string;
	workspace: string;
	checkpointId?: string;
}

export interface RestoreCheckpointResult {
	manifest: CheckpointManifest;
	restored: string[];
	deleted: string[];
}

const DEFAULT_EXCLUSIONS = [".git", "node_modules", "dist", "coverage"];
const DEFAULT_MAX_FILES = 10_000;
const DEFAULT_MAX_BYTES = 100 * 1024 * 1024;

export function checkpointsDir(sessionId: string): string {
	return path.join(sessionsDir(), validateSessionId(sessionId), "checkpoints");
}

export function checkpointDir(sessionId: string, checkpointId: string): string {
	validateCheckpointId(checkpointId);
	return path.join(checkpointsDir(sessionId), checkpointId);
}

function validateCheckpointId(id: string): void {
	if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(id)) {
		throw new Error(`Invalid checkpoint id: ${id}`);
	}
}

function manifestPath(sessionId: string, checkpointId: string): string {
	return path.join(checkpointDir(sessionId, checkpointId), "manifest.json");
}

function normalizeRelative(workspace: string, input: string): string {
	if (!input || input.includes("\0")) throw new Error(`Invalid workspace path: ${input}`);
	const root = path.resolve(workspace);
	const candidate = path.resolve(root, input);
	const relative = path.relative(root, candidate);
	if (!relative || relative.startsWith("..") || path.isAbsolute(relative)) throw new Error(`Path is outside the workspace: ${input}`);
	return relative.split(path.sep).join("/");
}

function excluded(relative: string, exclusions: readonly string[]): boolean {
	const segments = relative.split("/");
	return exclusions.some((rule) => {
		const normalized = rule.replaceAll("\\", "/").replace(/^\.\//, "").replace(/\/$/, "");
		return normalized !== "" && (relative === normalized || relative.startsWith(normalized + "/") || (!normalized.includes("/") && segments.includes(normalized)));
	});
}

async function assertRealContainment(workspace: string, candidate: string): Promise<void> {
	const root = await fs.realpath(workspace);
	let cursor = candidate;
	while (true) {
		try {
			const actual = await fs.realpath(cursor);
			const relative = path.relative(root, actual);
			if (relative.startsWith("..") || path.isAbsolute(relative)) throw new Error(`Path escapes workspace: ${candidate}`);
			return;
		} catch (error: any) {
			if (error?.code !== "ENOENT") throw error;
			const parent = path.dirname(cursor);
			if (parent === cursor) throw error;
			cursor = parent;
		}
	}
}

async function collectWorkspaceFiles(root: string, exclusions: readonly string[], maxFiles: number): Promise<string[]> {
	const result: string[] = [];
	async function visit(relativeDir: string): Promise<void> {
		const absolute = path.join(root, relativeDir);
		for (const entry of await fs.readdir(absolute, { withFileTypes: true })) {
			const relative = path.join(relativeDir, entry.name).split(path.sep).join("/");
			if (excluded(relative, exclusions)) continue;
			if (entry.isSymbolicLink()) continue;
			if (entry.isDirectory()) await visit(relative);
			else if (entry.isFile()) {
				result.push(relative);
				if (result.length > maxFiles) throw new Error(`Checkpoint exceeds maximum file count (${maxFiles})`);
			}
		}
	}
	await visit("");
	return result.sort();
}

function parseManifest(value: unknown): CheckpointManifest {
	if (!value || typeof value !== "object") throw new Error("Invalid checkpoint manifest");
	const manifest = value as CheckpointManifest;
	if (
		manifest.version !== CHECKPOINT_VERSION ||
		!manifest.id || !manifest.sessionId || typeof manifest.workspace !== "string" ||
		!(["pending", "finalized"] as const).includes(manifest.status) ||
		!Array.isArray(manifest.entries) || !Array.isArray(manifest.exclusions)
	) throw new Error("Invalid checkpoint manifest");
	validateCheckpointId(manifest.id);
	validateSessionId(manifest.sessionId);
	return manifest;
}

async function writeManifest(manifest: CheckpointManifest): Promise<void> {
	await atomicUpdateFile(manifestPath(manifest.sessionId, manifest.id), JSON.stringify(manifest, null, 2) + "\n");
}

export async function createCheckpoint(options: CreateCheckpointOptions): Promise<CheckpointManifest> {
	validateSessionId(options.sessionId);
	const workspace = path.resolve(options.workspace);
	const rootStat = await fs.stat(workspace);
	if (!rootStat.isDirectory()) throw new Error("Checkpoint workspace must be a directory");
	const workspaceReal = await fs.realpath(workspace);
	const maxFiles = options.maxFiles ?? DEFAULT_MAX_FILES;
	const maxBytes = options.maxBytes ?? DEFAULT_MAX_BYTES;
	if (!Number.isSafeInteger(maxFiles) || maxFiles < 1 || !Number.isSafeInteger(maxBytes) || maxBytes < 0) throw new Error("Invalid checkpoint limits");
	const exclusions = [...new Set([...DEFAULT_EXCLUSIONS, ...(options.exclude ?? [])])];
	const scope = options.workspaceSnapshot ? "workspace" : "declared";
	if (!options.workspaceSnapshot && !options.files) throw new Error("Declared checkpoint files are required");
	let files = options.workspaceSnapshot
		? await collectWorkspaceFiles(workspaceReal, exclusions, maxFiles)
		: [...new Set((options.files ?? []).map((file) => normalizeRelative(workspaceReal, file)))].sort();
	files = files.filter((file) => !excluded(file, exclusions));
	if (files.length > maxFiles) throw new Error(`Checkpoint exceeds maximum file count (${maxFiles})`);
	const id = randomUUID();
	const createdAt = new Date().toISOString();
	const manifest: CheckpointManifest = {
		version: CHECKPOINT_VERSION,
		id,
		sessionId: options.sessionId,
		workspace: workspaceReal,
		status: "pending",
		createdAt,
		scope,
		toolCallId: options.toolCallId,
		toolName: options.toolName,
		entries: [],
		exclusions,
		todo: options.todo,
		transcript: options.transcript,
	};
	await writeManifest(manifest);
	let total = 0;
	try {
		for (const [index, relative] of files.entries()) {
			const absolute = path.join(workspaceReal, ...relative.split("/"));
			await assertRealContainment(workspaceReal, absolute);
			let stat;
			try {
				stat = await fs.lstat(absolute);
			} catch (error: any) {
				if (error?.code === "ENOENT") {
					manifest.entries.push({ path: relative, type: "missing" });
					continue;
				}
				throw error;
			}
			if (stat.isSymbolicLink() || !stat.isFile()) throw new Error(`Checkpoint path is not a regular file: ${relative}`);
			total += stat.size;
			if (total > maxBytes) throw new Error(`Checkpoint exceeds maximum size (${maxBytes} bytes)`);
			const content = await fs.readFile(absolute);
			const blob = path.join("blobs", `${index}.bin`).split(path.sep).join("/");
			await atomicUpdateFile(path.join(checkpointDir(options.sessionId, id), ...blob.split("/")), content, { mode: 0o600 });
			manifest.entries.push({ path: relative, type: "file", mode: stat.mode & 0o777, size: content.length, sha256: createHash("sha256").update(content).digest("hex"), blob });
		}
		await writeManifest(manifest);
		return manifest;
	} catch (error) {
		await writeManifest(manifest).catch(() => undefined);
		throw error;
	}
}

export async function finalizeCheckpoint(sessionId: string, checkpointId: string): Promise<CheckpointManifest> {
	const manifest = await loadCheckpoint(sessionId, checkpointId);
	if (!manifest) throw new Error(`Checkpoint not found: ${checkpointId}`);
	if (manifest.status === "finalized") return manifest;
	for (const entry of manifest.entries) {
		if (entry.type === "missing") continue;
		if (!entry.blob || entry.size === undefined || !entry.sha256) throw new Error("Checkpoint is incomplete");
		const content = await fs.readFile(path.join(checkpointDir(sessionId, checkpointId), ...entry.blob.split("/")));
		if (content.length !== entry.size || createHash("sha256").update(content).digest("hex") !== entry.sha256) throw new Error("Checkpoint blob verification failed");
	}
	manifest.status = "finalized";
	manifest.finalizedAt = new Date().toISOString();
	await writeManifest(manifest);
	return manifest;
}

export async function discardCheckpoint(sessionId: string, checkpointId: string): Promise<void> {
	await fs.rm(checkpointDir(sessionId, checkpointId), { recursive: true, force: true });
}

export async function loadCheckpoint(sessionId: string, checkpointId: string): Promise<CheckpointManifest | undefined> {
	try {
		const manifest = parseManifest(JSON.parse(await fs.readFile(manifestPath(sessionId, checkpointId), "utf8")));
		if (manifest.sessionId !== sessionId || manifest.id !== checkpointId) throw new Error("Checkpoint identity mismatch");
		return manifest;
	} catch (error: any) {
		if (error?.code === "ENOENT") return undefined;
		throw error;
	}
}

export async function listCheckpoints(sessionId: string): Promise<CheckpointManifest[]> {
	const root = checkpointsDir(sessionId);
	let ids: string[];
	try {
		ids = await fs.readdir(root);
	} catch (error: any) {
		if (error?.code === "ENOENT") return [];
		throw error;
	}
	const manifests: CheckpointManifest[] = [];
	for (const id of ids) {
		if (!/^[0-9a-f-]{36}$/i.test(id)) continue;
		const manifest = await loadCheckpoint(sessionId, id).catch(() => undefined);
		if (manifest) manifests.push(manifest);
	}
	return manifests.sort((a, b) => (b.finalizedAt ?? b.createdAt).localeCompare(a.finalizedAt ?? a.createdAt));
}

export async function restoreCheckpoint(options: RestoreCheckpointOptions): Promise<RestoreCheckpointResult> {
	validateSessionId(options.sessionId);
	const workspace = await fs.realpath(path.resolve(options.workspace));
	const manifest = options.checkpointId
		? await loadCheckpoint(options.sessionId, options.checkpointId)
		: (await listCheckpoints(options.sessionId)).find((item) => item.status === "finalized" && !item.restoredAt);
	if (!manifest) throw new Error("No restorable checkpoint found");
	if (manifest.status !== "finalized" || manifest.restoredAt) throw new Error("Checkpoint is not restorable");
	if (path.resolve(manifest.workspace) !== workspace) throw new Error("Checkpoint belongs to a different workspace");
	const byPath = new Map(manifest.entries.map((entry) => [entry.path, entry]));
	const deleted: string[] = [];
	if (manifest.scope === "workspace") {
		const current = await collectWorkspaceFiles(workspace, manifest.exclusions, DEFAULT_MAX_FILES);
		for (const relative of current) {
			if (!byPath.has(relative)) {
				const absolute = path.join(workspace, ...relative.split("/"));
				await assertRealContainment(workspace, absolute);
				await fs.rm(absolute, { force: true });
				deleted.push(relative);
			}
		}
	}
	const restored: string[] = [];
	for (const entry of manifest.entries) {
		const relative = normalizeRelative(workspace, entry.path);
		const absolute = path.join(workspace, ...relative.split("/"));
		await assertRealContainment(workspace, absolute);
		if (entry.type === "missing") {
			await fs.rm(absolute, { force: true, recursive: true });
			deleted.push(relative);
			continue;
		}
		if (!entry.blob || entry.sha256 === undefined || entry.size === undefined) throw new Error("Checkpoint entry is incomplete");
		const content = await fs.readFile(path.join(checkpointDir(options.sessionId, manifest.id), ...entry.blob.split("/")));
		if (content.length !== entry.size || createHash("sha256").update(content).digest("hex") !== entry.sha256) throw new Error(`Checkpoint blob verification failed: ${relative}`);
		await atomicUpdateFile(absolute, content, { mode: entry.mode ?? 0o600 });
		restored.push(relative);
	}
	manifest.restoredAt = new Date().toISOString();
	await writeManifest(manifest);
	return { manifest, restored, deleted: [...new Set(deleted)] };
}
