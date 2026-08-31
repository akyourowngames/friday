import { NextResponse } from "next/server";
import { listSessions } from "@/src/sessions";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET() {
	const sessions = await listSessions();
	return NextResponse.json(
		sessions.map((s) => ({
			id: s.id,
			// Collapse whitespace/escaped newlines so multi-line prompts render
			// as a clean one-line title in the session rail.
			title: s.title.replace(/\\n/g, " ").replace(/\s+/g, " ").trim(),
			updatedAt: s.updatedAt,
			messageCount: s.messageCount,
			provider: s.provider,
			model: s.model,
		})),
	);
}
