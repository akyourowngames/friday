import { NextResponse } from "next/server";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/**
 * GET /api/workspace/cwd — the chat UI needs the server's current
 * working directory so the "Open Terminal" button knows where to land.
 * The directory is the one the user started `npm run web` from.
 */
export function GET() {
	return NextResponse.json({
		cwd: process.cwd(),
		platform: process.platform,
		shell: process.platform === "win32" ? process.env.ComSpec || "cmd.exe" : process.env.SHELL || "/bin/sh",
	});
}
