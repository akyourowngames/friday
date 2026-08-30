#!/usr/bin/env -S node --max-old-space-size=4096
// Public CLI entry. Re-executes the built dist/cli.js (its top-level code
// calls main()). The shim lives at the repo root so the package.json `bin`
// can point to a bare filename ("friday-ng") which npm v12 accepts.
import("./dist/cli.js");
