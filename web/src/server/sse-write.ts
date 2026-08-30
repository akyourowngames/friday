import "server-only";

/**
 * Lightweight Server-Sent Events writer built on the Web Streams API.
 * Next.js App Router's `runtime = "nodejs"` gives us full TransformStream
 * support, so we can return a `Response(stream.readable)` directly.
 */
export interface SseMessage {
	event: string;
	data: unknown;
}

export class SseWriter {
	private writer: WritableStreamDefaultWriter<Uint8Array>;
	private encoder = new TextEncoder();
	private closed = false;

	constructor(transform: TransformStream) {
		this.writer = transform.writable.getWriter();
	}

	/** Send a named event. `data` is JSON.stringified automatically. */
	async send(event: string, data: unknown): Promise<void> {
		if (this.closed) return;
		const payload = `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
		try {
			await this.writer.write(this.encoder.encode(payload));
		} catch (error) {
			// The client disconnected; mark closed so subsequent sends are no-ops.
			if ((error as { name?: string }).name === "AbortError") this.closed = true;
			else throw error;
		}
	}

	/** Send a comment line — useful as a keep-alive ping. */
	async comment(text: string): Promise<void> {
		if (this.closed) return;
		try {
			await this.writer.write(this.encoder.encode(`: ${text}\n\n`));
		} catch {
			this.closed = true;
		}
	}

	async close(): Promise<void> {
		if (this.closed) return;
		this.closed = true;
		try {
			await this.writer.close();
		} catch {
			// already closed
		}
	}

	get isClosed(): boolean {
		return this.closed;
	}
}

/** Build a { readable, writable } pair and wrap the writable in an SseWriter. */
export function createSseStream(): { stream: TransformStream; writer: SseWriter } {
	const transform = new TransformStream<Uint8Array, Uint8Array>();
	return { stream: transform, writer: new SseWriter(transform) };
}
