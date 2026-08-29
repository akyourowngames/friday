/**
 * Tests for the environment context block appended to the system prompt —
 * the fix for "the model burned tool calls to learn today's date / the OS".
 */
import { describe, expect, it } from "vitest";
import { buildEnvironmentContext, shellDescription } from "../src/env-context.ts";

describe("buildEnvironmentContext", () => {
	it("includes the OS, shell, cwd, and a formatted date/time", () => {
		const ctx = buildEnvironmentContext(new Date("2026-08-29T23:19:00"));
		expect(ctx).toContain("## Environment");
		expect(ctx).toContain("- Operating system:");
		expect(ctx).toContain("- Shell used by the bash tool:");
		expect(ctx).toContain(`- Current working directory: ${process.cwd()}`);
		expect(ctx).toContain("Saturday, August 29, 2026");
		expect(ctx).toContain("23:19");
	});

	it("tells the model to use the context instead of running commands", () => {
		const ctx = buildEnvironmentContext();
		expect(ctx.toLowerCase()).toContain("never run a command");
	});

	it("names cmd.exe and warns it is not POSIX on Windows", () => {
		const { shell } = shellDescription();
		if (process.platform === "win32") {
			expect(shell).toContain("cmd.exe");
			expect(shell).toContain("NOT POSIX");
		} else {
			expect(shell).toContain("/bin/sh");
		}
	});
});
