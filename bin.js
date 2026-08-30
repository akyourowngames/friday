#!/usr/bin/env -S node --max-old-space-size=4096
// Public CLI entry. Imports the built src/cli.js and re-exports its main.
import("./dist/cli.js").then((mod) => {
	// src/cli.ts has no exports; re-running the side-effecting main would be
	// a no-op here. Just rethrow any import error.
	return mod;
});
