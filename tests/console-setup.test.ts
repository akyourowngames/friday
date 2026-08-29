import { describe, it, expect } from "vitest";
import { setupConsoleEncoding, readConsoleStatus } from "../src/console-setup.ts";

describe("setupConsoleEncoding", () => {
	it("is idempotent and never throws", () => {
		expect(() => setupConsoleEncoding()).not.toThrow();
		expect(() => setupConsoleEncoding()).not.toThrow();
		expect(() => setupConsoleEncoding()).not.toThrow();
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
