import { NextResponse } from "next/server";
import { listCheckpoints, restoreCheckpoint } from "@/src/checkpoints.ts";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/**
 * GET /api/checkpoints?sessionId=<id> — list a session's checkpoints
 * (with heavy base64 blobs stripped so the list stays light).
 */
export async function GET(req: Request) {
	const sessionId = new URL(req.url).searchParams.get("sessionId");
	if (!sessionId) {
		return NextResponse.json({ error: "sessionId is required" }, { status: 400 });
	}
	try {
		const manifests = await listCheckpoints(sessionId);
		return NextResponse.json({
			checkpoints: manifests.map((manifest) => ({
				...manifest,
				entries: manifest.entries.map((entry) => ({ ...entry, blob: undefined })),
			})),
		});
	} catch (error) {
		return NextResponse.json(
			{ error: error instanceof Error ? error.message : "Failed to list checkpoints" },
			{ status: 500 },
		);
	}
}

interface RestoreRequest {
	sessionId?: string;
	checkpointId?: string;
}

/** POST /api/checkpoints — time-travel: restore the workspace to a checkpoint. */
export async function POST(req: Request) {
	const body = (await req.json().catch(() => ({}))) as RestoreRequest;
	if (!body.sessionId || !body.checkpointId) {
		return NextResponse.json(
			{ error: "sessionId and checkpointId are required" },
			{ status: 400 },
		);
	}
	try {
		const result = await restoreCheckpoint({
			sessionId: body.sessionId,
			checkpointId: body.checkpointId,
			workspace: process.cwd(),
		});
		return NextResponse.json({
			restored: result.restored,
			deleted: result.deleted,
			id: result.manifest.id,
			createdAt: result.manifest.createdAt,
			toolName: result.manifest.toolName,
		});
	} catch (error) {
		return NextResponse.json(
			{ error: error instanceof Error ? error.message : "Restore failed" },
			{ status: 500 },
		);
	}
}
