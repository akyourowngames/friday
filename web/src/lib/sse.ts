/**
 * SSE parser. Reads from a ReadableStream<Uint8Array> and yields
 * parsed { event, data } frames. The wire format here is what
 * SseWriter emits: `event: <name>\ndata: <json>\n\n`.
 */
export interface SseFrame {
	event: string;
	data: unknown;
}

export async function* parseSse(stream: ReadableStream<Uint8Array>, signal?: AbortSignal): AsyncGenerator<SseFrame> {
	const reader = stream.getReader();
	const decoder = new TextDecoder();
	let buffer = "";

	try {
		while (true) {
			if (signal?.aborted) return;
			const { value, done } = await reader.read();
			if (done) return;
			buffer += decoder.decode(value, { stream: true });

			// SSE frames are delimited by a blank line (\n\n)
			let separator = buffer.indexOf("\n\n");
			while (separator >= 0) {
				const block = buffer.slice(0, separator);
				buffer = buffer.slice(separator + 2);
				const frame = parseBlock(block);
				if (frame) yield frame;
				separator = buffer.indexOf("\n\n");
			}
		}
	} finally {
		try {
			reader.releaseLock();
		} catch {
			// already released
		}
	}
}

function parseBlock(block: string): SseFrame | null {
	let event = "message";
	let data = "";
	for (const rawLine of block.split("\n")) {
		// Comments start with ':'
		if (rawLine.startsWith(":")) continue;
		const colon = rawLine.indexOf(":");
		if (colon < 0) continue;
		const field = rawLine.slice(0, colon);
		const value = rawLine.slice(colon + 1).replace(/^ /, "");
		if (field === "event") event = value;
		else if (field === "data") data += value;
	}
	if (!data) return null;
	try {
		return { event, data: JSON.parse(data) };
	} catch {
		return { event, data };
	}
}
