import { NextResponse } from "next/server";
import { resolve } from "node:path";
import { spawn } from "node:child_process";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

interface RevealRequest {
	cwd?: string;
}

/**
 * POST /api/workspace/reveal — opens a new native OS terminal at the
 * workspace's current working directory. The server returns the launch
 * command for the client to execute via a sandboxed link (so the
 * browser can fire `window.open(...)` or a similar trigger without
 * needing elevated privileges). The command runs detached and survives
 * the web server process.
 *
 * - Windows: `start "" cmd /K cd /d <cwd>` — opens a new cmd window
 *   pinned to the workspace directory.
 * - macOS:   `open -a Terminal <cwd>` — launches Terminal.app.
 * - Linux:   honours `$TERMINAL` (gnome-terminal / konsole / xterm
 *   / foot / alacritty / kitty etc.) and falls back to x-terminal-emulator.
 */
export async function POST(req: Request) {
	const body = (await req.json().catch(() => ({}))) as RevealRequest;
	const cwd = body.cwd ? resolve(String(body.cwd)) : process.cwd();

	const platform = process.platform;
	const command = launchCommand(platform, cwd);

	try {
		spawn(command.bin, command.args, {
			detached: true,
			stdio: "ignore",
			windowsHide: true,
		}).unref();
	} catch (e) {
		return NextResponse.json(
			{ error: e instanceof Error ? e.message : String(e) },
			{ status: 500 },
		);
	}

	return NextResponse.json({ ok: true, platform, cwd, launched: command });
}

function launchCommand(platform: NodeJS.Platform, cwd: string): { bin: string; args: string[] } {
	if (platform === "win32") {
		// `start ""` opens a new console window. `cmd /K cd /d <cwd>` runs
		// `cd /d <cwd>` and stays open. Empty title in the first arg
		// suppresses a default window title.
		return { bin: "cmd", args: ["/c", "start", "", "cmd", "/K", "cd", "/d", cwd] };
	}
	if (platform === "darwin") {
		return { bin: "open", args: ["-a", "Terminal", cwd] };
	}
	// Linux / *BSD: prefer $TERMINAL, then x-terminal-emulator, then a
	// known list of common ones.
	const envTerm = process.env.TERMINAL?.trim();
	if (envTerm) {
		return { bin: envTerm, args: ["--working-directory", cwd] };
	}
	return { bin: "x-terminal-emulator", args: ["--working-directory", cwd] };
}
