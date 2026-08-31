import { createSseStream } from "@/server/sse-write";
import { getTerminal } from "@/server/terminal-registry";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/**
 * GET /api/terminal/[id] — SSE stream of the terminal's output.
 * Events: `data` (raw VT output), `exit` (process ended), then close.
 * Closing the browser connection does NOT kill the terminal — it keeps
 * running server-side so it can be reattached later.
 */
export async function GET(req: Request, ctx: { params: Promise<{ id: string }> }) {
	const { id } = await ctx.params;
	const session = getTerminal(id);
	if (!session) {
		return new Response("Terminal not found", { status: 404 });
	}

	const { stream, writer } = createSseStream();

	const onData = (data: string) => {
		void writer.send("data", { text: data });
	};
	const onExit = (info: { exitCode: number }) => {
		void writer.send("exit", info).then(() => writer.close());
	};

	session.subs.add(onData);
	session.exitSubs.add(onExit);

	const keepAlive = setInterval(() => {
		void writer.comment("ping");
	}, 15_000);

	const cleanup = () => {
		clearInterval(keepAlive);
		session.subs.delete(onData);
		session.exitSubs.delete(onExit);
	};
	req.signal.addEventListener("abort", cleanup, { once: true });

	return new Response(stream.readable, {
		headers: {
			"content-type": "text/event-stream; charset=utf-8",
			"cache-control": "no-cache, no-transform",
			connection: "keep-alive",
			"x-accel-buffering": "no",
		},
	});
}
