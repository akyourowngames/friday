import { NextResponse } from "next/server";
import { resolve } from "node:path";
import { runShell } from "@/src/tools/shell";
import { isDangerousShellCommand } from "@/src/permissions";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/** Hard ceiling for Quick Run — longer than this and we kill the process. */
const RUN_MAX_TIMEOUT_MS = 10 * 60_000; // 10 minutes
const RUN_DEFAULT_TIMEOUT_MS = 60_000; // 1 minute

interface RunRequest {
	command?: string;
	cwd?: string;
	timeoutMs?: number;
}

export async function POST(req: Request) {
	const body = (await req.json().catch(() => ({}))) as RunRequest;
	const command = String(body.command ?? "").trim();
	if (!command) {
		return NextResponse.json({ error: "A command is required" }, { status: 400 });
	}

	// Reuse the same dangerous-command guard the bash tool applies. We
	// don't want the Quick Run bar to be a privileged backdoor past the
	// `permissions.ts` policy.
	if (isDangerousShellCommand(command)) {
		return NextResponse.json(
			{ error: `Refusing to run potentially dangerous command: ${command}` },
			{ status: 403 },
		);
	}

	const cwd = body.cwd ? resolve(String(body.cwd)) : process.cwd();
	const timeoutMs = Math.min(
		RUN_MAX_TIMEOUT_MS,
		Math.max(1, Number(body.timeoutMs ?? RUN_DEFAULT_TIMEOUT_MS) || RUN_DEFAULT_TIMEOUT_MS),
	);

	const isWin = process.platform === "win32";
	const shellCmd = isWin ? "cmd" : "/bin/sh";
	const shellArgs = isWin ? ["/c", command] : ["-c", command];

	const t0 = Date.now();
	try {
		const { stdout, stderr, code, timedOut, aborted } = await runShell(
			shellCmd,
			shellArgs,
			cwd,
			timeoutMs,
			req.signal,
		);
		return NextResponse.json({
			command,
			cwd,
			platform: process.platform,
			shell: shellCmd,
			stdout,
			stderr,
			exitCode: code,
			timedOut,
			aborted,
			durationMs: Date.now() - t0,
		});
	} catch (e) {
		if (req.signal.aborted) {
			return NextResponse.json({ error: "aborted", aborted: true }, { status: 499 });
		}
		return NextResponse.json(
			{ error: e instanceof Error ? e.message : String(e) },
			{ status: 500 },
		);
	}
}
