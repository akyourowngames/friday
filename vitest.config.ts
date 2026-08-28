import { fileURLToPath } from "node:url";
import { defineConfig } from "vitest/config";

export default defineConfig({
	test: {
		globals: true,
		environment: "node",
		allowNodeNotProcess: true,
		testTimeout: 30000,
		silent: "passed-only",
		teardownTimeout: 500,
		coverage: {
			provider: "v8",
			all: true,
			include: ["src/**/*.ts"],
			exclude: ["src/**/*.d.ts", "src/**/types.ts"],
			reporter: ["text", "html", "lcov"],
			reportsDirectory: "coverage",
		},
	},
	resolve: {
		alias: [{ find: /^friday-ng$/, replacement: fileURLToPath(new URL("./src/index.ts", import.meta.url)) }],
	},
});
