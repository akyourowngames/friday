import type { StreamFn } from "./types.ts";

let defaultStreamFn: StreamFn | undefined;

/**
 * Configure the fallback used when callers omit streamFn.
 *
 * Hosts that provide a default model runtime can install its stream function here
 * without making the agent depend on a provider catalog.
 */
export function setDefaultStreamFn(streamFn: StreamFn | undefined): void {
	defaultStreamFn = streamFn;
}

export function getDefaultStreamFn(): StreamFn {
	if (!defaultStreamFn) {
		throw new Error("No default stream function configured. Pass streamFunction explicitly or call setDefaultStreamFn().");
	}
	return defaultStreamFn;
}
