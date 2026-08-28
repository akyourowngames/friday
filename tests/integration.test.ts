/**
 * End-to-end integration test for the OpenAI-compatible streaming path.
 *
 * We stub the global `fetch` (what the `openai` SDK uses under the hood) to
 * return a hand-built Server-Sent-Events response, then drive the full
 * `runAgentLoop` through `createOpenAICompatStreamFn` and assert that the
 * agent event stream emits `message_start` → `message_update` (text_delta) →
 * `message_end` with the fully assembled assistant message.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const SSE_BODY = [
  `data: {"id":"x","object":"chat.completion.chunk","model":"test-model","choices":[{"index":0,"delta":{"role":"assistant","content":""},"finish_reason":null}]}\n\n`,
  `data: {"id":"x","object":"chat.completion.chunk","model":"test-model","choices":[{"index":0,"delta":{"content":"Hello"},"finish_reason":null}]}\n\n`,
  `data: {"id":"x","object":"chat.completion.chunk","model":"test-model","choices":[{"index":0,"delta":{"content":" world"},"finish_reason":null}]}\n\n`,
  `data: {"id":"x","object":"chat.completion.chunk","model":"test-model","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n`,
  `data: [DONE]\n\n`,
].join("");

function sseFetch(): typeof globalThis.fetch {
  return vi.fn(async () => {
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(new TextEncoder().encode(SSE_BODY));
        controller.close();
      },
    });
    return new Response(stream, {
      status: 200,
      headers: { "content-type": "text/event-stream" },
    });
  }) as unknown as typeof globalThis.fetch;
}

const { createOpenAICompatStreamFn } = await import("../src/providers/openai-compat.ts");
const { runAgentLoop } = await import("../src/agent-loop.ts");
type AgentEvent = import("../src/types.ts").AgentEvent;
type AgentEventSink = import("../src/types.ts").AgentEventSink;
type Model = import("../src/types.ts").Model;

const testModel: Model = {
  id: "test-model",
  name: "Test Model",
  api: "openai",
  provider: "openai",
  baseUrl: "http://localhost:9999/v1",
  reasoning: false,
  input: ["text"],
  cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
  contextWindow: 8192,
  maxTokens: 4096,
};

describe("OpenAI-compatible streaming (end-to-end)", () => {
  let originalFetch: typeof globalThis.fetch;

  beforeEach(() => {
    originalFetch = globalThis.fetch;
    globalThis.fetch = sseFetch();
  });

  afterEach(() => {
    globalThis.fetch = originalFetch;
    vi.restoreAllMocks();
  });

  it("streams start/update/end events and assembles the full message", async () => {
    const streamFn = createOpenAICompatStreamFn({
      model: "test-model",
      apiKey: "sk-test",
      baseUrl: "http://localhost:9999/v1",
    });

    const events: AgentEvent[] = [];
    const emit: AgentEventSink = async (e) => {
      events.push(e);
    };

    const config = {
      model: testModel,
      convertToLlm: (m: AgentEvent[]) => m,
      streamFunction: streamFn,
    } as Parameters<typeof runAgentLoop>[3];

    await runAgentLoop(
      [{ role: "user", content: "Hi", timestamp: Date.now() }],
      { systemPrompt: "You are friday-ng.", messages: [] },
      config,
      emit,
      undefined,
      streamFn,
    );

    const starts = events.filter((e) => e.type === "message_start");
    const updates = events.filter((e) => e.type === "message_update");
    const ends = events.filter((e) => e.type === "message_end");

    // The assistant message gets its own start/end pair.
    expect(starts.length).toBeGreaterThanOrEqual(1);
    expect(ends.length).toBeGreaterThanOrEqual(1);

    // At least one text delta was streamed through message_update.
    const textUpdates = updates.filter(
      (e) => e.type === "message_update" && e.assistantMessageEvent.type === "text_delta",
    );
    expect(textUpdates.length).toBeGreaterThan(0);

    // The final assembled message contains the full text.
    const finalEnd = ends[ends.length - 1]!;
    if (finalEnd.type !== "message_end") throw new Error("expected message_end");
    const text = finalEnd.message.content
      .filter((c) => c.type === "text")
      .map((c) => (c as { text: string }).text)
      .join("");
    expect(text).toBe("Hello world");
  });

  it("honors abort by unwinding with an aborted message", async () => {
    const streamFn = createOpenAICompatStreamFn({
      model: "test-model",
      apiKey: "sk-test",
      baseUrl: "http://localhost:9999/v1",
    });

    const events: AgentEvent[] = [];
    const emit: AgentEventSink = async (e) => {
      events.push(e);
    };

    const config = {
      model: testModel,
      convertToLlm: (m: AgentEvent[]) => m,
      streamFunction: streamFn,
    } as Parameters<typeof runAgentLoop>[3];

    const controller = new AbortController();
    controller.abort();

    const result = await runAgentLoop(
      [{ role: "user", content: "Hi", timestamp: Date.now() }],
      { systemPrompt: "You are friday-ng.", messages: [] },
      config,
      emit,
      controller.signal,
      streamFn,
    );

    const assistant = result.find((m) => m.role === "assistant");
    expect(assistant).toBeDefined();
    if (assistant && "stopReason" in assistant) {
      expect(assistant.stopReason).toBe("aborted");
    }
  });
});
