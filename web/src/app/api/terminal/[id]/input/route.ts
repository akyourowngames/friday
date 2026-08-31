import { NextResponse } from "next/server";
import { getTerminal, killTerminal } from "@/server/terminal-registry";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

interface TerminalInputRequest {
	input?: string;
	cols?: number;
	rows?: number;
	kill?: boolean;
}

/**
 * POST /api/terminal/[id] — write keystrokes, resize, or kill the PTY.
 * Input arrives as one JSON write per keystroke/paste batch from the
 * browser; volume is negligible for interactive use.
 */
export async function POST(req: Request, ctx: { params: Promise<{ id: string }> }) {
	const { id } = await ctx.params;
	const body = (await req.json().catch(() => ({}))) as TerminalInputRequest;

	if (body.kill) {
		return NextResponse.json({ killed: killTerminal(id) });
	}

	const session = getTerminal(id);
	if (!session) {
		return NextResponse.json({ error: "Terminal not found" }, { status: 404 });
	}

	try {
		if (typeof body.input === "string") {
			session.pty.write(body.input);
		}
		if (typeof body.cols === "number" && typeof body.rows === "number") {
			session.pty.resize(Math.max(2, Math.floor(body.cols)), Math.max(2, Math.floor(body.rows)));
		}
		return NextResponse.json({ ok: true });
	} catch (error) {
		return NextResponse.json(
			{ error: error instanceof Error ? error.message : "Write failed" },
			{ status: 500 },
		);
	}
}
