#!/usr/bin/env node
// Smoke test for the new Quick Run + workspace endpoints.
// Run with: `node web/scripts/smoke.mjs` after `npm run start` is up.
// Hits the routes over loopback, asserts on the JSON shape, and prints
// a pass/fail summary. Exits non-zero on any failure.

const BASE = process.env.SMOKE_URL ?? "http://127.0.0.1:4317";

let passed = 0;
let failed = 0;

async function expect(label, fn) {
	try {
		await fn();
		passed++;
		console.log(`  ok  ${label}`);
	} catch (e) {
		failed++;
		console.error(`  FAIL ${label}: ${e?.message ?? e}`);
	}
}

function assert(cond, msg) {
	if (!cond) throw new Error(msg);
}

async function post(path, body) {
	const res = await fetch(`${BASE}${path}`, {
		method: "POST",
		headers: { "content-type": "application/json" },
		body: JSON.stringify(body),
	});
	return res;
}

async function get(path) {
	const res = await fetch(`${BASE}${path}`);
	return res;
}

await expect("GET /api/health returns ok", async () => {
	const res = await get("/api/health");
	assert(res.ok, `status ${res.status}`);
	const json = await res.json();
	assert(json.ok === true, "ok field missing");
});

await expect("GET /api/workspace/cwd returns server cwd", async () => {
	const res = await get("/api/workspace/cwd");
	assert(res.ok, `status ${res.status}`);
	const json = await res.json();
	assert(typeof json.cwd === "string" && json.cwd.length > 0, "cwd empty");
	assert(json.platform === process.platform, "platform mismatch");
});

await expect("POST /api/run returns stdout + exit code 0", async () => {
	const res = await post("/api/run", { command: "echo smoke-ok" });
	assert(res.ok, `status ${res.status}`);
	const json = await res.json();
	assert(json.stdout.includes("smoke-ok"), `stdout: ${json.stdout}`);
	assert(json.exitCode === 0, `exitCode: ${json.exitCode}`);
	assert(json.durationMs >= 0, "duration negative");
	assert(json.platform === process.platform, "platform mismatch");
});

await expect("POST /api/run rejects empty command with 400", async () => {
	const res = await post("/api/run", { command: "   " });
	assert(res.status === 400, `status ${res.status}`);
});

await expect("POST /api/run rejects dangerous command with 403", async () => {
	const res = await post("/api/run", { command: "rm -rf /" });
	assert(res.status === 403, `status ${res.status}`);
});

await expect("POST /api/run handles failing command with non-zero exit", async () => {
	// `dir /Z` is not a valid Windows flag — cmd exits with code 1.
	const res = await post("/api/run", { command: "dir /Z" });
	assert(res.ok, `status ${res.status}`);
	const json = await res.json();
	assert(json.exitCode !== 0, `expected non-zero exitCode, got ${json.exitCode}`);
});

await expect("POST /api/workspace/reveal responds ok", async () => {
	const res = await post("/api/workspace/reveal", {});
	assert(res.ok, `status ${res.status}`);
	const json = await res.json();
	assert(json.ok === true, "ok field missing");
	assert(typeof json.launched?.bin === "string", "launched.bin missing");
});

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed > 0 ? 1 : 0);
