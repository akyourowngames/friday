import { describe, it, expect } from "vitest";
import { setupConsoleEncoding, readConsoleStatus, consoleHasAnsiSupport, enableVirtualTerminalProcessing } from "../src/console-setup.ts";

describe("setupConsoleEncoding", () => {
	it("is idempotent and never throws", () => {
		expect(() => setupConsoleEncoding()).not.toThrow();
		expect(() => setupConsoleEncoding()).not.toThrow();
		expect(() => setupConsoleEncoding()).not.toThrow();
	});
});

describe("consoleHasAnsiSupport", () => {
	it("recognises the terminals that provide ANSI out of the box", () => {
		const keys = ["WT_SESSION", "TERM_PROGRAM", "ConEmuANSI", "ANSICON"] as const;
		// Assigning `undefined` to process.env would store the *string*
		// "undefined", so restore by deleting instead.
		const saved = new Map<string, string | undefined>();
		for (const k of keys) saved.set(k, process.env[k]);
		try {
			for (const k of keys) delete process.env[k];
			expect(consoleHasAnsiSupport()).toBe(false);

			process.env.WT_SESSION = "abc-123";
			expect(consoleHasAnsiSupport()).toBe(true);

			delete process.env.WT_SESSION;
			process.env.ConEmuANSI = "ON";
			expect(consoleHasAnsiSupport()).toBe(true);
		} finally {
			for (const k of keys) {
				const v = saved.get(k);
				if (v === undefined) delete process.env[k];
				else process.env[k] = v;
			}
		}
	});
});

describe("enableVirtualTerminalProcessing", () => {
	it("never throws, and short-circuits when there is no TTY to fix", () => {
		// Under vitest stdout is a pipe, so on Windows this must bail out
		// without ever spawning PowerShell — that guard is what keeps tests fast.
		expect(() => enableVirtualTerminalProcessing()).not.toThrow();
		if (process.platform === "win32" && !process.stdout.isTTY) {
			expect(enableVirtualTerminalProcessing()).toBe(false);
		}
	});
});

describe("readConsoleStatus", () => {
	it("returns a sane Utf8Status object", () => {
		const s = readConsoleStatus();
		expect(typeof s.platform).toBe("string");
		expect(s.platform.length).toBeGreaterThan(0);
		// POSIX is always UTF-8; on Windows we read the actual codepage.
		if (s.platform !== "win32") {
			expect(s.codePageIsUtf8).toBe(true);
		}
	});
});

describe("applyWindowsUtf8Default (registry-touching, Windows only)", () => {
	it.skipIf(process.platform !== "win32")(
		"writes CodePage=65001 and VT=1 under HKCU, then revert removes them",
		async () => {
			const { applyWindowsUtf8Default, revertWindowsUtf8Default } = await import(
				"../src/console-setup.ts"
			);
			const before = readConsoleStatus();
			try {
				applyWindowsUtf8Default();
				const after = readConsoleStatus();
				expect(after.vtEnabled).toBe(true);
			} finally {
				// Always restore prior state so the test is non-destructive.
				if (!before.vtEnabled) {
					try { revertWindowsUtf8Default(); } catch { /* ignore */ }
				}
			}
		},
	);
});
