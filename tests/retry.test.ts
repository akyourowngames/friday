import { describe, it, expect, vi } from "vitest";
import { isRetryable, backoffDelay, retryAfterMs, withRetry } from "../src/retry.ts";
import type { StreamFn, Model } from "../src/types.ts";

describe("isRetryable", () => {
	it("matches overloaded errors", () => {
		expect(isRetryable(new Error("server is overloaded"))).toBe(true);
	});
	it("matches 5xx HTTP errors", () => {
		expect(isRetryable(new Error("HTTP 503 service unavailable"))).toBe(true);
		expect(isRetryable(new Error("HTTP 429 too many requests"))).toBe(true);
	});
	it("matches network errors", () => {
		expect(isRetryable(new Error("ETIMEDOUT"))).toBe(true);
		expect(isRetryable(new Error("ECONNRESET"))).toBe(true);
	});
	it("does not match 4xx client errors", () => {
		expect(isRetryable(new Error("HTTP 401 unauthorized"))).toBe(false);
		expect(isRetryable(new Error("HTTP 400 bad request"))).toBe(false);
	});
	it("does not match validation errors", () => {
		expect(isRetryable(new Error("invalid api key"))).toBe(false);
	});
	it("handles non-Error values", () => {
		expect(isRetryable("server is overloaded")).toBe(true);
		expect(isRetryable(null)).toBe(false);
	});
});

describe("backoffDelay", () => {
	it("grows exponentially", () => {
		const d1 = backoffDelay(1, { baseDelayMs: 100, maxDelayMs: 10000 });
		const d2 = backoffDelay(2, { baseDelayMs: 100, maxDelayMs: 10000 });
		const d3 = backoffDelay(3, { baseDelayMs: 100, maxDelayMs: 10000 });
		expect(d1).toBeGreaterThanOrEqual(100);
		expect(d2).toBeGreaterThanOrEqual(200);
		expect(d3).toBeGreaterThanOrEqual(400);
	});
	it("respects the cap", () => {
		const d = backoffDelay(20, { baseDelayMs: 1000, maxDelayMs: 5000 });
		expect(d).toBeLessThanOrEqual(5000);
	});
});

describe("retryAfterMs", () => {
	it("parses seconds", () => {
		const err: any = { headers: { get: (k: string) => (k === "retry-after" ? "5" : null) } };
		expect(retryAfterMs(err)).toBe(5000);
	});
	it("returns undefined when header is missing", () => {
		expect(retryAfterMs(new Error("plain"))).toBeUndefined();
	});
});

describe("withRetry", () => {
	const fakeModel = { id: "x" } as any as Model;
	const fakeContext = { systemPrompt: "", messages: [] } as any;

	it("returns immediately on success", async () => {
		const inner: StreamFn = vi.fn(async () => "ok" as any);
		const wrapped = withRetry(inner, { maxRetries: 2, baseDelayMs: 1 });
		const r = await wrapped(fakeModel, fakeContext, {} as any);
		expect(r).toBe("ok");
		expect(inner).toHaveBeenCalledTimes(1);
	});

	it("retries on transient errors and eventually succeeds", async () => {
		let calls = 0;
		const inner: StreamFn = async () => {
			calls++;
			if (calls < 3) throw new Error("server is overloaded");
			return "ok" as any;
		};
		const wrapped = withRetry(inner, { maxRetries: 3, baseDelayMs: 1 });
		const r = await wrapped(fakeModel, fakeContext, {} as any);
		expect(r).toBe("ok");
		expect(calls).toBe(3);
	});

	it("throws immediately on non-retryable errors", async () => {
		const inner: StreamFn = async () => {
			throw new Error("HTTP 401 unauthorized");
		};
		const wrapped = withRetry(inner, { maxRetries: 5, baseDelayMs: 1 });
		await expect(wrapped(fakeModel, fakeContext, {} as any)).rejects.toThrow("401");
	});

	it("throws after maxRetries exhausted", async () => {
		const inner: StreamFn = async () => {
			throw new Error("server overloaded");
		};
		const wrapped = withRetry(inner, { maxRetries: 2, baseDelayMs: 1 });
		await expect(wrapped(fakeModel, fakeContext, {} as any)).rejects.toThrow("overloaded");
	});

	it("fires onRetry callback with attempt info", async () => {
		let calls = 0;
		const inner: StreamFn = async () => {
			calls++;
			if (calls < 2) throw new Error("overloaded");
			return "ok" as any;
		};
		const events: number[] = [];
		const wrapped = withRetry(inner, {
			maxRetries: 3,
			baseDelayMs: 1,
			onRetry: (e) => events.push(e.attempt),
		});
		await wrapped(fakeModel, fakeContext, {} as any);
		expect(events).toEqual([1]);
	});

	it("aborts when signal is set", async () => {
		const ac = new AbortController();
		ac.abort();
		const inner: StreamFn = async () => "ok" as any;
		const wrapped = withRetry(inner, { maxRetries: 3, baseDelayMs: 1, signal: ac.signal });
		await expect(wrapped(fakeModel, fakeContext, {} as any)).rejects.toThrow("aborted");
	});

	it("is a no-op when disabled", async () => {
		let calls = 0;
		const inner: StreamFn = async () => {
			calls++;
			throw new Error("overloaded");
		};
		const wrapped = withRetry(inner, { enabled: false, maxRetries: 5, baseDelayMs: 1 });
		await expect(wrapped(fakeModel, fakeContext, {} as any)).rejects.toThrow("overloaded");
		expect(calls).toBe(1);
	});
});
