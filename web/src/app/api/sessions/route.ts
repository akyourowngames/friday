import { NextResponse } from "next/server";
import { listSessions } from "@/src/sessions";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET() {
	const sessions = await listSessions();
	return NextResponse.json(
		sessions.map((s) => ({
			id: s.id,
			title: s.title,
			updatedAt: s.updatedAt,
			messageCount: s.messageCount,
			provider: s.provider,
			model: s.model,
		})),
	);
}
