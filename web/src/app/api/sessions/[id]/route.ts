import { NextResponse } from "next/server";
import { loadSession } from "@/src/sessions";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET(_req: Request, { params }: { params: Promise<{ id: string }> }) {
	const { id } = await params;
	const session = await loadSession(decodeURIComponent(id));
	if (!session) {
		return NextResponse.json({ error: "Not found" }, { status: 404 });
	}
	return NextResponse.json(session);
}
