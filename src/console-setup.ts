/**
 * Console UTF-8 setup.
 *
 * Per-invocation: switch the active console to UTF-8 (`chcp 65001`) with
 * inherited stdio so the change reaches the parent's terminal, and force
 * Node stdout/stderr to emit UTF-8 bytes. No-op on POSIX.
 *
 * Machine-wide: write the Windows registry values that make every future
 * cmd.exe / Conhost session start in UTF-8 with VT processing enabled.
 * This is opt-in (call `applyWindowsUtf8Default()` explicitly) because it
 * mutates per-user registry keys under HKCU.
 */
import { spawnSync } from "node:child_process";

let ran = false;

/** Run the per-invocation console setup. Idempotent. */
export function setupConsoleEncoding(): void {
	if (ran) return;
	ran = true;

	if (process.platform !== "win32") return;

	// Force the Node side to emit UTF-8 regardless of any inherited mode.
	try {
		if (typeof (process.stdout as any).setDefaultEncoding === "function") {
			(process.stdout as any).setDefaultEncoding("utf8");
		}
		if (typeof (process.stderr as any).setDefaultEncoding === "function") {
			(process.stderr as any).setDefaultEncoding("utf8");
		}
	} catch {
		// ignore
	}

	// Switch the Win32 console code page to UTF-8. We use spawnSync with
	// stdio:"inherit" so the chcp process shares our console handles —
	// that's what makes the codepage change apply to the user's terminal,
	// not just to the chcp child's transient console buffer.
	try {
		spawnSync("chcp", ["65001"], {
			stdio: "inherit",
			windowsHide: true,
		});
	} catch {
		// Some sandboxes block chcp; that's fine, the next-best thing is the
		// Node-side encoding fix above.
	}
}

export interface Utf8Status {
	platform: NodeJS.Platform;
	codePage: number | null;
	codePageIsUtf8: boolean;
	vtEnabled: boolean | null;
	applied: boolean;
}

/** Read the current console state (no side effects). */
export function readConsoleStatus(): Utf8Status {
	const status: Utf8Status = {
		platform: process.platform,
		codePage: null,
		codePageIsUtf8: false,
		vtEnabled: null,
		applied: false,
	};
	if (process.platform !== "win32") {
		status.codePageIsUtf8 = true; // POSIX is always UTF-8
		return status;
	}
	try {
		const cp = spawnSync("chcp", [], {
			stdio: ["ignore", "pipe", "ignore"],
			windowsHide: true,
		});
		const m = /(\d+)/.exec(cp.stdout?.toString() ?? "");
		if (m) {
			status.codePage = parseInt(m[1]!, 10);
			status.codePageIsUtf8 = status.codePage === 65001;
		}
	} catch {
		// ignore
	}
	try {
		const reg = spawnSync("reg", [
			"query",
			"HKCU\\Console",
			"/v",
			"VirtualTerminalLevel",
		], { stdio: ["ignore", "pipe", "ignore"], windowsHide: true });
		const out = reg.stdout?.toString() ?? "";
		const m = /VirtualTerminalLevel\s+REG_DWORD\s+0x([0-9a-f]+)/i.exec(out);
		if (m) status.vtEnabled = parseInt(m[1]!, 16) === 1;
	} catch {
		// ignore
	}
	return status;
}

/**
 * Make UTF-8 + VT processing the Windows default for this user account.
 *
 * Writes (under HKCU, so no admin needed):
 *   - Console\CodePage = 65001
 *       → the default code page for new Conhost windows opened by this
 *         user (interactive double-click on cmd.exe, `start cmd`, etc.).
 *   - Console\VirtualTerminalLevel = 1
 *       → enables ANSI escape sequence processing in Conhost
 *         (colors, cursor moves, etc.). On by default in Windows
 *         Terminal, off in plain `cmd.exe`.
 *   - Software\Microsoft\Command Processor\Autorun = "chcp 65001 >nul"
 *       → runs `chcp 65001` at the start of every cmd.exe invocation,
 *         including non-interactive ones (`cmd /c ...`, scripts, etc.).
 *         This is the most reliable knob for every-day shell use.
 *
 * Idempotent. Throws if `reg.exe` is missing or permission is denied.
 */
export function applyWindowsUtf8Default(): void {
	if (process.platform !== "win32") {
		throw new Error("applyWindowsUtf8Default is Windows-only");
	}
	const dwordArgs = (value: string, data: string) => [
		"add",
		"HKCU\\Console",
		"/v",
		value,
		"/t",
		"REG_DWORD",
		"/d",
		data,
		"/f",
	];
	const stringArgs = (key: string, value: string, data: string) => [
		"add",
		key,
		"/v",
		value,
		"/t",
		"REG_SZ",
		"/d",
		data,
		"/f",
	];

	const r1 = spawnSync("reg", dwordArgs("CodePage", "65001"), {
		stdio: "ignore",
		windowsHide: true,
	});
	if (r1.status !== 0) {
		throw new Error(
			`Failed to set HKCU\\Console\\CodePage: ${r1.stderr?.toString() || r1.error?.message}`,
		);
	}
	const r2 = spawnSync("reg", dwordArgs("VirtualTerminalLevel", "1"), {
		stdio: "ignore",
		windowsHide: true,
	});
	if (r2.status !== 0) {
		throw new Error(
			`Failed to set HKCU\\Console\\VirtualTerminalLevel: ${r2.stderr?.toString() || r2.error?.message}`,
		);
	}
	const r3 = spawnSync(
		"reg",
		stringArgs("HKCU\\Software\\Microsoft\\Command Processor", "Autorun", "chcp 65001 >nul"),
		{ stdio: "ignore", windowsHide: true },
	);
	if (r3.status !== 0) {
		throw new Error(
			`Failed to set HKCU\\...\\Command Processor\\Autorun: ${r3.stderr?.toString() || r3.error?.message}`,
		);
	}
}

/** Reverse of `applyWindowsUtf8Default` — removes the registry values. */
export function revertWindowsUtf8Default(): void {
	if (process.platform !== "win32") {
		throw new Error("revertWindowsUtf8Default is Windows-only");
	}
	const deletes: Array<{ args: string[]; key: string }> = [
		{ args: ["delete", "HKCU\\Console", "/v", "CodePage", "/f"], key: "HKCU\\Console\\CodePage" },
		{ args: ["delete", "HKCU\\Console", "/v", "VirtualTerminalLevel", "/f"], key: "HKCU\\Console\\VirtualTerminalLevel" },
		{
			args: ["delete", "HKCU\\Software\\Microsoft\\Command Processor", "/v", "Autorun", "/f"],
			key: "HKCU\\Software\\Microsoft\\Command Processor\\Autorun",
		},
	];
	for (const { args, key } of deletes) {
		const r = spawnSync("reg", args, { stdio: "ignore", windowsHide: true });
		// `reg delete` returns 1 if the value didn't exist — that's fine.
		if (r.status !== 0 && r.status !== 1) {
			throw new Error(
				`Failed to delete ${key}: ${r.stderr?.toString() || r.error?.message}`,
			);
		}
	}
}
