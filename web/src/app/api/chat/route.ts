import { NextResponse } from "next/server";
import { Agent } from "@/src/agent";
import { buildStreamFunction } from "@/src/interactive";
import { loadConfig, saveConfig, withLastModel, withLastProvider } from "@/src/config";
import { findProvider } from "@/src/providers/registry";
import { createSession, loadSession, recordMessage, updateMeta } from "@/src/sessions";
import { SettingsStore } from "@/src/settings";
import { buildSystemPrompt } from "@/src/system-prompt";
import { buildModel, defaultTools } from "@/server/route-helpers";
import { createSseStream } from "@/server/sse-write";
import { getAgent, registerAgent } from "@/server/agent-registry";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";
export const maxDuration = 600; // 10 min — long enough for any single agent run.

interface ChatRequest {
	sessionId?: string;
	prompt?: string;
	provider?: string;
	model?: string;
}

export async function POST(req: Request) {
	const body = (await req.json().catch(() => ({}))) as ChatRequest;
	const prompt = body.prompt?.trim();
	if (!prompt) {
		return NextResponse.json({ error: "A prompt is required" }, { status: 400 });
	}

	// Resolve provider + model
	const config = await loadConfig();
	const providerId = body.provider ?? config.lastProvider ?? "faux";
	const provider = findProvider(providerId);
	if (!provider) {
		return NextResponse.json({ error: `Unknown provider: ${providerId}` }, { status: 400 });
	}
	const modelId = body.model ?? config.providers[providerId]?.lastModel ?? provider.defaultModel;

	// Persist the user's last-used provider/model so the next session starts
	// on the same combination. (Light-touch: only write when they actually
	// differ from the stored value, to avoid fs thrash.)
	if (config.lastProvider !== providerId) {
		await saveConfig(withLastProvider(config, providerId));
	}
	if (config.providers[providerId]?.lastModel !== modelId) {
		const refreshed = await loadConfig();
		await saveConfig(withLastModel(refreshed, providerId, modelId));
	}

	// Load or create the session record
	const loaded = body.sessionId ? await loadSession(body.sessionId) : undefined;
	const session =
		loaded?.meta ??
		(await createSession({
			provider: providerId,
			model: modelId,
			apiStyle: provider.apiStyle,
			systemPrompt: await buildSystemPrompt(modelId, process.cwd()),
		}));

	// Reuse a live agent for this session, or construct a fresh one
	let agent = getAgent(session.id);
	if (!agent) {
		const streamFn = await buildStreamFunction(providerId, {
			model: modelId,
			apiKey: config.providers[providerId]?.apiKey ?? "",
			baseUrl: config.providers[providerId]?.baseUrl,
			authToken: config.providers[providerId]?.authToken,
		});
		const settings = new SettingsStore({ config });
		// Pull the user's web-search keys (Tavily) and the SearXNG instance
		// URL out of the persisted settings store. Brave is wired into the
		// provider list but not yet wired into the tool — exposed in the
		// UI so the user can store the key now and pick it up later.
		const tavilyApiKey = (settings.get("tavilyApiKey") as string | null) || undefined;
		const searxngUrl = (settings.get("searxngUrl") as string | null) || undefined;
		const searchConfig = {
			...(tavilyApiKey ? { tavilyApiKey } : {}),
			...(searxngUrl ? { searxngUrl } : {}),
		};
		agent = new Agent({
			sessionId: session.id,
			initialState: {
				systemPrompt: session.systemPrompt,
				tools: defaultTools(searchConfig),
				model: buildModel(provider, modelId),
				messages: loaded?.messages ?? [],
			},
			streamFunction: streamFn,
			toolExecution: "sequential",
			maxTokens: Number(settings.get("maxTokens")) || undefined,
			temperature: Number(settings.get("temperature")) >= 0 ? Number(settings.get("temperature")) : undefined,
		});
		registerAgent(session.id, agent);
	}

	// Open the SSE stream. NOTE: We must NOT await any writes before returning
	// the Response — Node Web Streams apply backpressure, so awaiting a write
	// while the readable has no consumer yet deadlocks the request. All writes
	// happen inside the background IIFE below, kicked off the moment the
	// browser starts consuming the stream.
	const { stream, writer } = createSseStream();

	// Forward every agent event onto the wire. `message_end` triggers
	// persistence so the session transcript survives server restarts.
	const unsubscribe = agent.subscribe(async (event) => {
		try {
			await writer.send("agent-event", event);
			if (event.type === "message_end") {
				const toolCallsAdded = event.message.role === "assistant"
					? event.message.content.filter((part) => part.type === "toolCall").length
					: 0;
				await recordMessage(session.id, event.message, toolCallsAdded);
			}
		} catch {
			// client likely disconnected; let the cleanup path handle it
		}
	});

	// Hook client-side cancellation: when the browser aborts the fetch, the
	// request's AbortSignal fires and we stop the agent + close the stream.
	const onAbort = () => {
		try {
			agent?.abort();
		} catch {
			// ignore
		}
		unsubscribe();
		writer.close().catch(() => undefined);
	};
	req.signal.addEventListener("abort", onAbort, { once: true });

	// Return the Response BEFORE driving the agent. This lets the browser
	// start consuming the readable side of the stream; any subsequent
	// `writer.send()` will succeed because there's a reader attached.
	const response = new Response(stream.readable, {
		headers: {
			"content-type": "text/event-stream; charset=utf-8",
			"cache-control": "no-cache, no-transform",
			connection: "keep-alive",
			"x-accel-buffering": "no",
		},
	});

	// Now drive the agent. Everything here runs after the response has been
	// returned, so writes will be consumed promptly.
	void (async () => {
		try {
			await writer.send("session", { id: session.id, provider: providerId, model: modelId });
			await agent!.prompt(prompt);
			await agent!.waitForIdle();
			await updateMeta(session.id, { model: modelId, provider: providerId });
			await writer.send("complete", {});
		} catch (error) {
			await writer.send("error", {
				message: error instanceof Error ? error.message : String(error),
			});
		} finally {
			unsubscribe();
			req.signal.removeEventListener("abort", onAbort);
			await writer.close();
		}
	})();

	return response;
}
