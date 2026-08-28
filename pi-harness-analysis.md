# Pi Agent Harness — Streaming Architecture Analysis

Repo: https://github.com/earendil-works/pi (cloned locally to `C:\Users\anime\Desktop\pi-harness`)
Purpose: Understand the streaming/token-dispatch pipeline so friday-ng can reuse the same approach for faster token appearance.

## Monorepo layout

```
packages/
  ai/            # @earendil-works/pi-ai — unified multi-provider LLM API (177 files)
  agent/         # @earendil-works/pi-agent-core — agent runtime (50 files)
  tui/           # @earendil-works/pi-tui — terminal UI w/ differential rendering (40 files)
  coding-agent/  # @earendil-works/pi-coding-agent — CLI app (the TUI wiring)
  telemetry/     # vendor-neutral telemetry contracts
```

Build: `npm install --ignore-scripts; npm run build` (deps are pinned exact versions, offline-capable)
Check: `npm run check` (biome lint + typecheck via tsgo)
Test: `./test.sh`

## 1. The streaming contract (packages/ai/src/types.ts)

Every provider implements `ProviderStreams`:

```ts
export interface ProviderStreams {
  stream(model, context, options?): AssistantMessageEventStream;
  streamSimple(model, context, options?): AssistantMessageEventStream;
}
```

### AssistantMessageEventStream (packages/ai/src/utils/event-stream.ts)

A custom `AsyncIterable` — NOT `ReadableStream` or a generator. This is important.

```
class EventStream<T, R = T> implements AsyncIterable<T>
  - internal queue + waiter[] — classic producer/consumer
  - push(event) — delivers to a waiting consumer or queues
  - end(result) — flushes all waiters with done:true
  - [Symbol.asyncIterator]() — yields events one at a time
  - result() — returns Promise<R> (the final resolved value)
```

`AssistantMessageEventStream extends EventStream<AssistantMessageEvent, AssistantMessage>`.

**Why this matters for "fast tokens":** the `push()` method immediately hands an event to a waiting consumer — there's no buffering, no debounce, no `queueMicrotask`/setTimeout batching. Each `text_delta` event that arrives from the HTTP stream is pushed synchronously, and the async-iterator consumer picks it up on the next microtask. This is why tokens "appear instantly."

## 2. The event protocol (packages/ai/src/types.ts lines 535-551)

```
{ type: "start"; partial: AssistantMessage }            — stream began
{ type: "text_start"; contentIndex: number; partial }   — first text chunk
{ type: "text_delta"; contentIndex: number; delta: string; partial } — each token/fragment
{ type: "text_end"; contentIndex: number; content: string; partial }
{ type: "thinking_start" | "thinking_delta" | "thinking_end"; ... }   — reasoning
{ type: "toolcall_start" | "toolcall_delta" | "toolcall_end"; ... }  — tool call args
{ type: "done"; reason: StopReason; message: AssistantMessage }      — terminal success
{ type: "error"; reason: StopReason; error: AssistantMessage }       — terminal error
```

Key: each delta event carries a `partial` AssistantMessage — the full state so far. The consumer does NOT need to track deltas itself; it just reads `partial.content` each time.

## 3. The agent loop (packages/agent/src/agent-loop.ts)

### Entry points

- `createAgentStream()` / `runAgentLoop()` — the public entry, returns an `AgentEventStream`
- `runLoop(...)` — inner loop: call LLM → execute tools → repeat until stop
- `streamAssistantResponse(...)` — **the core streaming function** (lines 281-372)

### streamAssistantResponse — THE streaming core

```ts
async function streamAssistantResponse(
  context, config, signal, emit, streamFunction
): Promise<AssistantMessage> {
  const response = await streamFunction(config.model, llmContext, {...config, apiKey, signal});
  let partialMessage: AssistantMessage | null = null;
  let addedPartial = false;

  for await (const event of response) {          // ← async iteration over event stream
    switch (event.type) {
      case "start":
        partialMessage = event.partial;
        context.messages.push(partialMessage);
        addedPartial = true;
        await emit({ type: "message_start", message: { ...partialMessage } });
        break;

      case "text_delta":                        // ← each token arrives here
      case "text_start":
      case "thinking_delta":
      case "toolcall_delta":
        // ...
      case "text_end":
      case "thinking_end":
      case "toolcall_end":
        if (partialMessage) {
          partialMessage = event.partial;                 // full state update
          context.messages[context.messages.length - 1] = partialMessage;
          await emit({
            type: "message_update",
            assistantMessageEvent: event,                 // raw event for type-specific handling
            message: { ...partialMessage },
          });
        }
        break;

      case "done":
      case "error":
        const finalMessage = await response.result();
        // finalize, emit message_start if not added, then message_end
        return finalMessage;
    }
  }
}
```

**For-your-AI takeaway:** the `for await (const event of response)` loop is where you'd splice in any token-level interception. The `event` carries `type` + `delta` (the raw token string) + `partial` (full accumulated state). Emit `message_update` on every delta → TUI re-renders.

## 4. Event flow: Agent → TUI (packages/coding-agent/src/core/agent-session.ts + modes/interactive)

AgentLoopConfig (packages/agent/src/types.ts lines 149-293) is the key integration point. It has:
- `convertToLlm: (AgentMessage[]) => Message[]` — message format bridge
- `streamFunction: StreamFn` — inject your own LLM streamer
- `getApiKey?: (provider) => Promise<string>` — dynamic key resolution
- `transformContext?: (messages) => Promise<AgentMessage[]>` — context pruning/injection
- `beforeToolCall?` / `afterToolCall?` — tool lifecycle hooks
- `toolExecution?: "parallel" | "sequential"`

### AgentSession event emission (agent-session.ts lines 622-666)

The session subscribes to the agent's event stream and bridges to:
1. `_emitExtensionEvent(event)` — for extensions/plugins
2. `_emit(event)` — for TUI listeners (via `AgentSessionEventListener`)

### TUI rendering hot path (interactive-mode.ts lines 3221-3318)

```ts
case "message_start":
  if (event.message.role === "assistant") {
    this.streamingComponent = new AssistantMessageComponent(...);
    this.streamingMessage = event.message;
    this.chatContainer.addChild(this.streamingComponent);
    this.streamingComponent.updateContent(this.streamingMessage, true);  // true = streaming
    this.ui.requestRender();
  }
  break;

case "message_update":                       // ← fires on EVERY text_delta
  if (this.streamingComponent && event.message.role === "assistant") {
    this.streamingMessage = event.message;
    this.streamingComponent.updateContent(this.streamingMessage, true);
    // also handles tool call arg streaming:
    for (const content of this.streamingMessage.content) {
      if (content.type === "toolCall") {
        if (!this.pendingTools.has(content.id)) {
          // create ToolExecutionComponent
        } else {
          this.pendingTools.get(content.id)?.updateArgs(content.arguments);  // ← args stream too
        }
      }
    }
    this.ui.requestRender();                 // ← re-render on each delta
  }
  break;
```

**Differential rendering** comes from `@earendil-works/pi-tui` (packages/tui/src). The TUI uses a reconciliation approach: it computes a diff between the current virtual tree and the previous render, then writes only the changed cells to stdout. The output is chunked in 1 MiB pieces to avoid V8 string limits.

See `packages/tui/src/tui-main-screen.ts` — the `append()` method flushes in chunks, and the render loop diffs frames before writing.

## 5. How to reuse this in friday-ng

### Option A: Drop-in the agent core
The agent loop (`@earendil-works/pi-agent-core`) is provider-agnostic. You can:
1. `npm install @earendil-works/pi-agent-core` (or build from source)
2. Create an `AgentLoopConfig` pointing at your model provider
3. Set `streamFunction` to `provider.streamSimple` or a custom one
4. Subscribe to events and render with your own TUI or the pi-tui

### Option B: Use pi-tui directly
`@earendil-works/pi-tui` is a standalone TUI library. Import it:
```ts
import { Terminal } from "@earendil-works/pi-tui";
```
The terminal component handles differential rendering, input, scrollbars, etc.

### Option C: Study + rebuild (cleanest for friday-ng)
Key files to study as reference implementations:
- `packages/ai/src/utils/event-stream.ts` — copy the AsyncIterable pattern
- `packages/agent/src/agent-loop.ts` lines 281-372 — the streaming loop
- `packages/agent/src/types.ts` — the AgentEvent type union (message_start/update/end)
- `packages/coding-agent/src/modes/interactive/interactive-mode.ts` lines 3221-3318 — event → TUI wiring
- `packages/tui/src/tui-main-screen.ts` — differential rendering + chunked output

The "fast tokens" trick is simply: async-iterate the event stream, emit on every delta, call `requestRender()` immediately — no debouncing.

## Key insight

The entire "faster tokens appearing" behavior comes from:
1. `for await (const event of response)` — immediate microtask delivery
2. `emit({ type: "message_update", ... })` — on every text_delta
3. `ui.requestRender()` — synchronous redraw on every update
4. Differential TUI renderer — only changed cells hit stdout

No debouncing, no batching, no throttling. Tokens appear as soon as the HTTP SSE chunk is parsed.

## Files studied

| File | Role |
|------|------|
| packages/ai/src/types.ts | Streaming event types, provider contracts |
| packages/ai/src/utils/event-stream.ts | EventStream / AssistantMessageEventStream (AsyncIterable) |
| packages/agent/src/agent-loop.ts | Core streaming loop: streamAssistantResponse() |
| packages/agent/src/types.ts | AgentEvent union, AgentLoopConfig, AgentTool |
| packages/agent/src/agent.ts | Agent class, subscribe() |
| packages/coding-agent/src/core/agent-session.ts | Event bridge: agent → extensions + TUI |
| packages/coding-agent/src/modes/interactive/interactive-mode.ts | TUI event handling (message_update rendering) |
| packages/tui/src/tui-main-screen.ts | Differential rendering, chunked output |
