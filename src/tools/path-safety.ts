/**
 * Path safety helpers used by the built-in tools.
 *
 * `resolveSafePath(root, input)` returns the absolute path of `input`
 * (which may be absolute or relative) without throwing on invalid input.
 * `isPathInside(root, candidate)` returns true if `candidate` is inside
 * `root` (after normalization). The two together gate file tools to a
 * workspace.
 */
import * as path from "node:path";
import { promises as fs } from "node:fs";

/** Normalize a path. If `input` is absolute, it's returned as-is (resolved). */
export function resolveSafePath(root: string, input: string): string {
	if (!input) return root;
	const resolvedRoot = path.resolve(root);
	if (path.isAbsolute(input)) return path.normalize(input);
	return path.normalize(path.join(resolvedRoot, input));
}

/** Return true if `candidate` is the same as or a descendant of `root`. */
export function isPathInside(root: string, candidate: string): boolean {
	const rel = path.relative(path.resolve(root), path.resolve(candidate));
	if (rel.startsWith("..")) return false;
	if (path.isAbsolute(rel)) return false;
	return true;
}

/** Convenience: try to stat a path. Returns null if it doesn't exist. */
export async function tryStat(p: string): Promise<import("node:fs").Stats | null> {
	try {
		return await fs.stat(p);
	} catch (err: any) {
		if (err?.code === "ENOENT") return null;
		throw err;
	}
}
