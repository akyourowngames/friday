/**
 * Provider-level retry helper for friday-ng.
 *
 * Wraps a `StreamFn` so transient stream failures (network errors, HTTP 5xx,
 * rate limits) are retried with exponential backoff. Non-retryable errors
 * (4xx, validation, context overflow) are surfaced immediately.
 *
 * Modeled after pi-harness' `settings.retry` machinery, but much simpler
 * because friday-ng has one stream function per agent.
 */
import type { StreamFn, StreamOptions } from "./types.ts";

export interface RetryOptions {
	/** Whether to retry at all. Default: `true`. */
	enabled?: boolean;
	/** Maximum number of retry attempts. Default: `3`. */
	maxRetries?: number;
	/** Base delay in ms for exponential backoff. Default: `2000`. */
	baseDelayMs?: number;
	/** Cap on any single backoff delay. Default: `60000`. */
	maxDelayMs?: number;
	/** Abort signal that cancels the retry loop. */
	signal?: AbortSignal;
	/** Optional callback fired before each retry. */
	onRetry?: (info: { attempt: number; delayMs: number; error: Error }) => void;
}

const RETRY_AFTER_DEFAULT = 5_000;

const RETRYABLE_PATTERNS = [
	/overloaded/i,
	/rate.?limit/i,
	/server.?error/i,
	/internal.?error/i,
	/timeout/i,
	/ETIMEDOUT/i,
	/ECONNRESET/i,
	/EAI_AGAIN/i,
	/HTTP\s*5\d\d/,
	/HTTP\s*429/,
	/terminated/i,
	/aborted/i,
];

/** Decide whether an error is worth retrying. */
export function isRetryable(err: unknown): boolean {
	if (!err) return false;
	const msg = err instanceof Error ? err.message : String(err);
	return RETRYABLE_PATTERNS.some((p) => p.test(msg));
}

/** Compute a backoff delay with exponential growth + jitter. */
export function backoffDelay(attempt: number, opts: Pick<RetryOptions, "baseDelayMs" | "maxDelayMs">): number {
	const base = opts.baseDelayMs ?? 2000;
	const cap = opts.maxDelayMs ?? 60_000;
	const exp = base * Math.pow(2, Math.max(0, attempt - 1));
	const jitter = Math.random() * (base / 2);
	return Math.min(cap, exp + jitter);
}

/** Read a `Retry-After` header value (in ms) from a fetch-style error. */
export function retryAfterMs(err: unknown): number | undefined {
	const e: any = err;
	const headers = e?.headers ?? e?.response?.headers;
	if (!headers) return undefined;
	const raw = headers.get?.("retry-after") ?? headers["retry-after"];
	if (!raw) return undefined;
	const seconds = Number(raw);
	if (Number.isFinite(seconds)) return seconds * 1000;
	const dateMs = Date.parse(raw);
	if (Number.isFinite(dateMs)) return Math.max(0, dateMs - Date.now());
	return undefined;
}

/** Sleep for `ms`, honoring an AbortSignal. */
function sleep(ms: number, signal?: AbortSignal): Promise<void> {
	return new Promise((resolve, reject) => {
		if (signal?.aborted) return reject(new Error("aborted"));
		const t = setTimeout(() => {
			signal?.removeEventListener("abort", onAbort);
			resolve();
		}, ms);
		const onAbort = () => {
			clearTimeout(t);
			reject(new Error("aborted"));
		};
		signal?.addEventListener("abort", onAbort, { once: true });
	});
}

/** Wrap a StreamFn with retry-on-transient-error. */
export function withRetry(streamFn: StreamFn, opts: RetryOptions = {}): StreamFn {
	const enabled = opts.enabled ?? true;
	const maxRetries = opts.maxRetries ?? 3;
	if (!enabled || maxRetries <= 0) return streamFn;

	return async (model, context, options: StreamOptions = {}) => {
		let lastError: unknown;
		for (let attempt = 1; attempt <= maxRetries + 1; attempt++) {
			if (opts.signal?.aborted) throw new Error("aborted");
			try {
				return await streamFn(model, context, options);
			} catch (err) {
				lastError = err;
				if (attempt > maxRetries) break;
				if (!isRetryable(err)) throw err;
				const hint = retryAfterMs(err);
				const delay = hint ?? backoffDelay(attempt, opts);
				opts.onRetry?.({ attempt, delayMs: delay, error: err as Error });
				try {
					await sleep(delay, opts.signal);
				} catch (abortErr) {
					throw abortErr;
				}
			}
		}
		throw lastError instanceof Error ? lastError : new Error(String(lastError));
	};
}
