import { NextResponse } from "next/server";
import { abortSession } from "@/server/agent-registry";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function POST(req: Request) {
	const body = (await req.json().catch(() => ({}))) as { sessionId?: string };
	const id = typeof body.sessionId === "string" ? body.sessionId : "";
	const ok = id ? abortSession(id) : false;
	return NextResponse.json({ ok });
}
