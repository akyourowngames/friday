import type { AssistantMessage, AssistantMessageEvent } from "./types.ts";

/**
 * Generic async-iterable event stream.
 *
 * This is the core of Pi's "instant token" streaming architecture. Each call
 * to `push()` hands an event directly to a waiting consumer (via the async
 * iterator) on the next microtask, with no buffering, debouncing, or batching.
 *
 * Consumers iterate with `for await (const event of stream)`. The stream
 * resolves its terminal result via `result()` when an event satisfies
 * `isComplete`.
 */
export class EventStream<T, R = T> implements AsyncIterable<T> {
	private queue: T[] = [];
	private waiting: ((value: IteratorResult<T>) => void)[] = [];
	private done = false;
	private finalResultPromise: Promise<R>;
	private resolveFinalResult!: (result: R) => void;
	private isComplete: (event: T) => boolean;
	private extractResult: (event: T) => R;

	constructor(isComplete: (event: T) => boolean, extractResult: (event: T) => R) {
		this.isComplete = isComplete;
		this.extractResult = extractResult;
		this.finalResultPromise = new Promise((resolve) => {
			this.resolveFinalResult = resolve;
		});
	}

	/** Push an event to consumers immediately. If a consumer is waiting, it
	 *  receives the event on the next microtask. Otherwise the event is queued. */
	push(event: T): void {
		if (this.done) return;

		if (this.isComplete(event)) {
			this.done = true;
			this.resolveFinalResult(this.extractResult(event));
		}

		const waiter = this.waiting.shift();
		if (waiter) {
			waiter({ value: event, done: false });
		} else {
			this.queue.push(event);
		}
	}

	/** End the stream, optionally with a final result. All waiting consumers
	 *  are flushed with done:true. */
	end(result?: R): void {
		this.done = true;
		if (result !== undefined) {
			this.resolveFinalResult(result);
		}
		while (this.waiting.length > 0) {
			const waiter = this.waiting.shift()!;
			waiter({ value: undefined as any, done: true });
		}
	}

	/** Async iterator — yields events as they are pushed. Returns when the
	 *  terminal event is seen or `end()` is called. */
	async *[Symbol.asyncIterator](): AsyncIterator<T> {
		while (true) {
			if (this.queue.length > 0) {
				yield this.queue.shift()!;
			} else if (this.done) {
				return;
			} else {
				const result = await new Promise<IteratorResult<T>>((resolve) => this.waiting.push(resolve));
				if (result.done) return;
				yield result.value;
			}
		}
	}

	/** Promise that resolves to the final result when a terminal event arrives. */
	result(): Promise<R> {
		return this.finalResultPromise;
	}
}

/** Event stream specialized for LLM assistant message events.
 *  Terminal events are `done` and `error`. */
export class AssistantMessageEventStream extends EventStream<AssistantMessageEvent, AssistantMessage> {
	constructor() {
		super(
			(event) => event.type === "done" || event.type === "error",
			(event) => {
				if (event.type === "done") {
					return event.message;
				} else if (event.type === "error") {
					return event.error;
				}
				throw new Error("Unexpected event type for final result");
			},
		);
	}
}

/** Factory function (used by providers and tests). */
export function createAssistantMessageEventStream(): AssistantMessageEventStream {
	return new AssistantMessageEventStream();
}
