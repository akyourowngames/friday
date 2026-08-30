/**
 * Thin typed fetch wrapper for the /api routes. All app data flows through
 * this — no ad-hoc fetch() calls scattered around components.
 */

export interface ApiError extends Error {
	status: number;
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
	const response = await fetch(path, {
		...init,
		headers: { "content-type": "application/json", ...(init?.headers ?? {}) },
	});
	if (!response.ok) {
		const text = await response.text().catch(() => "");
		const error = new Error(text || `${response.status} ${response.statusText}`) as ApiError;
		error.status = response.status;
		throw error;
	}
	if (response.status === 204) return undefined as T;
	return (await response.json()) as T;
}

export const api = {
	get: <T>(path: string) => call<T>(path),
	post: <T>(path: string, body: unknown) => call<T>(path, { method: "POST", body: JSON.stringify(body) }),
};
