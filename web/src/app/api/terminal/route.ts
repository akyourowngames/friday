import { NextResponse } from "next/server";
import { createTerminal } from "@/server/terminal-registry";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

interface CreateTerminalRequest {
	cwd?: string;
}

/** POST /api/terminal — spawn a new PTY-backed shell, return its id. */
export async function POST(req: Request) {
	const body = (await req.json().catch(() => ({}))) as CreateTerminalRequest;
	try {
		const session = createTerminal(body.cwd);
		return NextResponse.json({
			id: session.id,
			cwd: session.cwd,
			shell: session.shell,
			pid: session.pty.pid,
		});
	} catch (error) {
		return NextResponse.json(
			{ error: error instanceof Error ? error.message : "Failed to spawn terminal" },
			{ status: 500 },
		);
	}
}
