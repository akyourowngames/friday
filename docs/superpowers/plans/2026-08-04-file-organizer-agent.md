# File Organizer Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Node.js/TypeScript CLI agent that uses an LLM to decide how to organize files, shows a plan, and executes after approval.

**Architecture:** Direct tool functions (like Ares), not MCP. Agent loop calls LLM with tool definitions, LLM returns tool calls, agent executes them. Multi-provider LLM client using OpenAI-compatible API format.

**Tech Stack:** TypeScript, Node.js, chalk (colors), commander (CLI), vitest (testing)

---

## File Structure

```
file-organizer/
├── src/
│   ├── llm/
│   │   ├── types.ts         ← Message, ToolCall, ChatResponse types
│   │   ├── providers.ts     ← provider URL + env key configs
│   │   └── client.ts        ← LLMClient class (fetch-based)
│   ├── tools/
│   │   ├── types.ts         ← Tool interface, ToolResult type
│   │   ├── definitions.ts   ← JSON schemas the LLM sees
│   │   ├── filesystem.ts    ← list_dir, get_file_info, search_files
│   │   ├── organizer.ts     ← move_file, rename_file, create_folder, delete_file, done
│   │   └── index.ts         ← tool registry (name → implementation)
│   ├── agent/
│   │   ├── types.ts         ← AgentConfig, QueuedAction types
│   │   └── loop.ts          ← runAgent() core loop
│   ├── config.ts            ← loadConfig, CLI flag parsing
│   └── index.ts             ← CLI entry point
├── tests/
│   ├── llm/
│   │   └── client.test.ts
│   ├── tools/
│   │   ├── filesystem.test.ts
│   │   └── organizer.test.ts
│   └── agent/
│       └── loop.test.ts
├── package.json
├── tsconfig.json
└── vitest.config.ts
```

---

### Task 1: Project Scaffold

**Files:**
- Create: `file-organizer/package.json`
- Create: `file-organizer/tsconfig.json`
- Create: `file-organizer/vitest.config.ts`

- [ ] **Step 1: Create package.json**

```bash
cd /c/Users/anime/friday
mkdir -p file-organizer
```

Create `file-organizer/package.json`:

```json
{
  "name": "file-organizer",
  "version": "0.1.0",
  "description": "AI-powered file organizer CLI",
  "type": "module",
  "main": "dist/index.js",
  "scripts": {
    "build": "tsc",
    "dev": "tsx src/index.ts",
    "test": "vitest run",
    "test:watch": "vitest"
  },
  "dependencies": {
    "chalk": "^5.3.0",
    "commander": "^12.1.0"
  },
  "devDependencies": {
    "typescript": "^5.5.0",
    "@types/node": "^22.0.0",
    "tsx": "^4.19.0",
    "vitest": "^2.1.0"
  }
}
```

- [ ] **Step 2: Create tsconfig.json**

Create `file-organizer/tsconfig.json`:

```json
{
  "compilerOptions": {
    "target": "ES2022",
    "module": "ESNext",
    "moduleResolution": "bundler",
    "outDir": "dist",
    "rootDir": "src",
    "strict": true,
    "esModuleInterop": true,
    "skipLibCheck": true,
    "forceConsistentCasingInFileNames": true,
    "resolveJsonModule": true,
    "declaration": true,
    "declarationMap": true,
    "sourceMap": true
  },
  "include": ["src/**/*"],
  "exclude": ["node_modules", "dist", "tests"]
}
```

- [ ] **Step 3: Create vitest.config.ts**

Create `file-organizer/vitest.config.ts`:

```ts
import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    include: ["tests/**/*.test.ts"],
  },
});
```

- [ ] **Step 4: Install dependencies**

```bash
cd file-organizer && npm install
```

- [ ] **Step 5: Verify setup compiles**

```bash
npx tsc --noEmit
```

Expected: no errors (no source files yet, so clean)

- [ ] **Step 6: Commit**

```bash
cd /c/Users/anime/friday
git add file-organizer/
git commit -m "feat(file-organizer): scaffold project with TypeScript, vitest, CLI deps"
```

---

### Task 2: LLM Types

**Files:**
- Create: `file-organizer/src/llm/types.ts`
- Create: `file-organizer/tests/llm/client.test.ts`

- [ ] **Step 1: Write the type definitions**

Create `file-organizer/src/llm/types.ts`:

```ts
/** A message in the chat conversation */
export interface Message {
  role: "system" | "user" | "assistant" | "tool";
  content: string | null;
  tool_calls?: ToolCall[];
  tool_call_id?: string;
}

/** A tool call returned by the LLM */
export interface ToolCall {
  id: string;
  type: "function";
  function: {
    name: string;
    arguments: string; // JSON string
  };
}

/** A tool definition sent to the LLM */
export interface ToolDefinition {
  type: "function";
  function: {
    name: string;
    description: string;
    parameters: {
      type: "object";
      properties: Record<string, unknown>;
      required?: string[];
    };
  };
}

/** Response from the LLM chat completion */
export interface ChatResponse {
  message: Message;
  finishReason: "stop" | "tool_calls" | "length";
  usage?: {
    promptTokens: number;
    completionTokens: number;
    totalTokens: number;
  };
}

/** Provider configuration */
export interface ProviderConfig {
  baseUrl: string;
  envKey: string;
}
```

- [ ] **Step 2: Commit**

```bash
git add file-organizer/src/llm/types.ts
git commit -m "feat(file-organizer): add LLM type definitions"
```

---

### Task 3: LLM Providers

**Files:**
- Create: `file-organizer/src/llm/providers.ts`

- [ ] **Step 1: Write provider configs**

Create `file-organizer/src/llm/providers.ts`:

```ts
import type { ProviderConfig } from "./types.js";

export const PROVIDERS: Record<string, ProviderConfig> = {
  opencode: {
    baseUrl: "https://opencode.ai/zen/v1",
    envKey: "OPENCODE_API_KEY",
  },
  openai: {
    baseUrl: "https://api.openai.com/v1",
    envKey: "OPENAI_API_KEY",
  },
};

export const DEFAULT_PROVIDER = "opencode";
export const DEFAULT_MODEL = "deepseek-v4-flash-free";

export function resolveProvider(name: string): ProviderConfig {
  const provider = PROVIDERS[name];
  if (!provider) {
    const available = Object.keys(PROVIDERS).join(", ");
    throw new Error(`Unknown provider "${name}". Available: ${available}`);
  }
  return provider;
}
```

- [ ] **Step 2: Commit**

```bash
git add file-organizer/src/llm/providers.ts
git commit -m "feat(file-organizer): add LLM provider configs"
```

---

### Task 4: LLM Client

**Files:**
- Create: `file-organizer/src/llm/client.ts`
- Create: `file-organizer/tests/llm/client.test.ts`

- [ ] **Step 1: Write the failing test**

Create `file-organizer/tests/llm/client.test.ts`:

```ts
import { describe, it, expect } from "vitest";
import { LLMClient } from "../../src/llm/client.js";

describe("LLMClient", () => {
  it("throws when API key is missing", () => {
    const original = process.env.OPENCODE_API_KEY;
    delete process.env.OPENCODE_API_KEY;

    expect(() => new LLMClient("opencode", "test-model")).toThrow(
      "Missing API key"
    );

    if (original) process.env.OPENCODE_API_KEY = original;
  });

  it("constructs with valid provider and API key", () => {
    process.env.OPENCODE_API_KEY = "test-key";
    const client = new LLMClient("opencode", "test-model");
    expect(client).toBeDefined();
    expect(client.model).toBe("test-model");
  });

  it("throws on unknown provider", () => {
    process.env.OPENCODE_API_KEY = "test-key";
    expect(() => new LLMClient("nonexistent", "test-model")).toThrow(
      'Unknown provider "nonexistent"'
    );
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd file-organizer && npx vitest run tests/llm/client.test.ts
```

Expected: FAIL — `Cannot find module '../../src/llm/client.js'`

- [ ] **Step 3: Write minimal implementation**

Create `file-organizer/src/llm/client.ts`:

```ts
import type {
  Message,
  ToolDefinition,
  ChatResponse,
  ToolCall,
} from "./types.js";
import { resolveProvider, DEFAULT_MODEL } from "./providers.js";

export class LLMClient {
  private baseUrl: string;
  private apiKey: string;
  readonly model: string;

  constructor(provider: string, model?: string) {
    const config = resolveProvider(provider);
    this.baseUrl = config.baseUrl;
    this.model = model || DEFAULT_MODEL;

    const apiKey = process.env[config.envKey];
    if (!apiKey) {
      throw new Error(
        `Missing API key: set ${config.envKey} environment variable`
      );
    }
    this.apiKey = apiKey;
  }

  async chat(
    messages: Message[],
    tools?: ToolDefinition[]
  ): Promise<ChatResponse> {
    const body: Record<string, unknown> = {
      model: this.model,
      messages,
    };

    if (tools && tools.length > 0) {
      body.tools = tools;
    }

    const res = await fetch(`${this.baseUrl}/chat/completions`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${this.apiKey}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify(body),
    });

    if (!res.ok) {
      const text = await res.text();
      throw new Error(`LLM API error ${res.status}: ${text}`);
    }

    const data = (await res.json()) as any;
    const choice = data.choices?.[0];

    if (!choice) {
      throw new Error("No choices in LLM response");
    }

    const message: Message = choice.message;
    const toolCalls: ToolCall[] = message.tool_calls || [];

    return {
      message: {
        role: "assistant",
        content: message.content || null,
        tool_calls: toolCalls.length > 0 ? toolCalls : undefined,
      },
      finishReason: choice.finish_reason,
      usage: data.usage
        ? {
            promptTokens: data.usage.prompt_tokens,
            completionTokens: data.usage.completion_tokens,
            totalTokens: data.usage.total_tokens,
          }
        : undefined,
    };
  }
}
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd file-organizer && npx vitest run tests/llm/client.test.ts
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add file-organizer/src/llm/client.ts file-organizer/tests/llm/client.test.ts
git commit -m "feat(file-organizer): implement LLMClient with multi-provider support"
```

---

### Task 5: Tool Types & Definitions

**Files:**
- Create: `file-organizer/src/tools/types.ts`
- Create: `file-organizer/src/tools/definitions.ts`

- [ ] **Step 1: Write tool types**

Create `file-organizer/src/tools/types.ts`:

```ts
/** Result of executing a tool */
export interface ToolResult {
  success: boolean;
  data?: unknown;
  error?: string;
}

/** A tool the agent can call */
export interface Tool {
  name: string;
  description: string;
  parameters: {
    type: "object";
    properties: Record<string, unknown>;
    required?: string[];
  };
  execute(args: Record<string, any>): Promise<ToolResult>;
}
```

- [ ] **Step 2: Write tool definitions (what the LLM sees)**

Create `file-organizer/src/tools/definitions.ts`:

```ts
import type { ToolDefinition } from "../llm/types.js";

export const TOOL_DEFINITIONS: ToolDefinition[] = [
  {
    type: "function",
    function: {
      name: "list_dir",
      description:
        "List the contents of a directory. Returns files and subdirectories with metadata.",
      parameters: {
        type: "object",
        properties: {
          path: {
            type: "string",
            description: "Absolute path to the directory",
          },
        },
        required: ["path"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "get_file_info",
      description:
        "Get detailed information about a specific file: size, dates, extension, type.",
      parameters: {
        type: "object",
        properties: {
          path: {
            type: "string",
            description: "Absolute path to the file",
          },
        },
        required: ["path"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "search_files",
      description:
        "Search for files matching a pattern (glob). Returns list of matching paths.",
      parameters: {
        type: "object",
        properties: {
          directory: {
            type: "string",
            description: "Directory to search in",
          },
          pattern: {
            type: "string",
            description:
              'Glob pattern to match, e.g. "*.jpg", "**/*.pdf"',
          },
        },
        required: ["directory", "pattern"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "move_file",
      description:
        "Move a file from one location to another. Creates destination directory if needed.",
      parameters: {
        type: "object",
        properties: {
          source: {
            type: "string",
            description: "Full path of the file to move",
          },
          destination: {
            type: "string",
            description: "Full path of the new location including filename",
          },
        },
        required: ["source", "destination"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "rename_file",
      description: "Rename a file (same directory, different name).",
      parameters: {
        type: "object",
        properties: {
          path: {
            type: "string",
            description: "Full path of the file to rename",
          },
          new_name: {
            type: "string",
            description: "New filename (not full path)",
          },
        },
        required: ["path", "new_name"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "create_folder",
      description: "Create a directory. Creates parent directories if needed.",
      parameters: {
        type: "object",
        properties: {
          path: {
            type: "string",
            description: "Full path of the directory to create",
          },
        },
        required: ["path"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "delete_file",
      description: "Delete a file. Moves to trash if possible.",
      parameters: {
        type: "object",
        properties: {
          path: {
            type: "string",
            description: "Full path of the file to delete",
          },
        },
        required: ["path"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "done",
      description:
        "Signal that you have finished organizing. Provide a summary of what you did.",
      parameters: {
        type: "object",
        properties: {
          summary: {
            type: "string",
            description:
              "Summary of all actions taken: files moved, folders created, etc.",
          },
        },
        required: ["summary"],
      },
    },
  },
];
```

- [ ] **Step 3: Commit**

```bash
git add file-organizer/src/tools/types.ts file-organizer/src/tools/definitions.ts
git commit -m "feat(file-organizer): add tool types and LLM-facing definitions"
```

---

### Task 6: Filesystem Tools

**Files:**
- Create: `file-organizer/src/tools/filesystem.ts`
- Create: `file-organizer/tests/tools/filesystem.test.ts`

- [ ] **Step 1: Write failing tests**

Create `file-organizer/tests/tools/filesystem.test.ts`:

```ts
import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { mkdtemp, rm, writeFile, mkdir } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { listDir, getFileInfo, searchFiles } from "../../src/tools/filesystem.js";

let tempDir: string;

beforeEach(async () => {
  tempDir = await mkdtemp(join(tmpdir(), "fo-test-"));
});

afterEach(async () => {
  await rm(tempDir, { recursive: true, force: true });
});

describe("listDir", () => {
  it("lists files and directories", async () => {
    await writeFile(join(tempDir, "file.txt"), "hello");
    await mkdir(join(tempDir, "subdir"));

    const result = await listDir(tempDir);

    expect(result.success).toBe(true);
    expect(result.data).toBeDefined();
    const entries = result.data as any[];
    expect(entries).toHaveLength(2);

    const file = entries.find((e: any) => e.name === "file.txt");
    expect(file?.type).toBe("file");

    const dir = entries.find((e: any) => e.name === "subdir");
    expect(dir?.type).toBe("directory");
  });

  it("returns error for non-existent directory", async () => {
    const result = await listDir("/nonexistent/path");
    expect(result.success).toBe(false);
    expect(result.error).toBeDefined();
  });
});

describe("getFileInfo", () => {
  it("returns file metadata", async () => {
    await writeFile(join(tempDir, "test.md"), "# Hello");

    const result = await getFileInfo(join(tempDir, "test.md"));

    expect(result.success).toBe(true);
    const info = result.data as any;
    expect(info.name).toBe("test.md");
    expect(info.extension).toBe(".md");
    expect(info.size).toBeGreaterThan(0);
  });

  it("returns error for non-existent file", async () => {
    const result = await getFileInfo("/nonexistent/file.txt");
    expect(result.success).toBe(false);
  });
});

describe("searchFiles", () => {
  it("finds files matching a pattern", async () => {
    await writeFile(join(tempDir, "a.txt"), "a");
    await writeFile(join(tempDir, "b.txt"), "b");
    await writeFile(join(tempDir, "c.jpg"), "c");

    const result = await searchFiles(tempDir, "*.txt");

    expect(result.success).toBe(true);
    const files = result.data as string[];
    expect(files).toHaveLength(2);
    expect(files.every((f) => f.endsWith(".txt"))).toBe(true);
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd file-organizer && npx vitest run tests/tools/filesystem.test.ts
```

Expected: FAIL — cannot find module

- [ ] **Step 3: Implement filesystem tools**

Create `file-organizer/src/tools/filesystem.ts`:

```ts
import { readdir, stat, access } from "node:fs/promises";
import { join, basename, extname, resolve } from "node:path";
import { glob } from "node:fs/promises";
import type { ToolResult } from "./types.js";

interface DirEntry {
  name: string;
  path: string;
  type: "file" | "directory";
  size?: number;
  extension?: string;
}

interface FileInfo {
  name: string;
  path: string;
  extension: string;
  size: number;
  created: Date;
  modified: Date;
  isDirectory: boolean;
}

export async function listDir(dirPath: string): Promise<ToolResult> {
  try {
    await access(dirPath);
    const entries = await readdir(dirPath, { withFileTypes: true });

    const result: DirEntry[] = await Promise.all(
      entries.map(async (entry) => {
        const fullPath = join(dirPath, entry.name);
        const entryStat = await stat(fullPath).catch(() => null);

        return {
          name: entry.name,
          path: fullPath,
          type: entry.isDirectory() ? "directory" : "file",
          size: entryStat?.size,
          extension: entry.isDirectory() ? undefined : extname(entry.name),
        };
      })
    );

    return { success: true, data: result };
  } catch (err: any) {
    return { success: false, error: err.message };
  }
}

export async function getFileInfo(filePath: string): Promise<ToolResult> {
  try {
    const fileStat = await stat(filePath);

    return {
      success: true,
      data: {
        name: basename(filePath),
        path: resolve(filePath),
        extension: extname(filePath),
        size: fileStat.size,
        created: fileStat.birthtime,
        modified: fileStat.mtime,
        isDirectory: fileStat.isDirectory(),
      } satisfies FileInfo,
    };
  } catch (err: any) {
    return { success: false, error: err.message };
  }
}

export async function searchFiles(
  directory: string,
  pattern: string
): Promise<ToolResult> {
  try {
    const results: string[] = [];

    // Use readdir recursively with pattern matching
    await searchRecursive(directory, pattern, results);

    return { success: true, data: results };
  } catch (err: any) {
    return { success: false, error: err.message };
  }
}

async function searchRecursive(
  dir: string,
  pattern: string,
  results: string[]
): Promise<void> {
  const entries = await readdir(dir, { withFileTypes: true });

  for (const entry of entries) {
    const fullPath = join(dir, entry.name);

    if (entry.isDirectory()) {
      await searchRecursive(fullPath, pattern, results);
    } else if (matchGlob(entry.name, pattern)) {
      results.push(fullPath);
    }
  }
}

function matchGlob(filename: string, pattern: string): boolean {
  // Simple glob matching: *.ext, **/*.ext, exact name
  if (pattern.startsWith("**/")) {
    const ext = pattern.slice(3);
    return filename.endsWith(ext);
  }
  if (pattern.startsWith("*.")) {
    const ext = pattern.slice(1);
    return filename.endsWith(ext);
  }
  if (pattern.includes("*")) {
    const regex = new RegExp(
      "^" + pattern.replace(/\./g, "\\.").replace(/\*/g, ".*") + "$"
    );
    return regex.test(filename);
  }
  return filename === pattern;
}
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd file-organizer && npx vitest run tests/tools/filesystem.test.ts
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add file-organizer/src/tools/filesystem.ts file-organizer/tests/tools/filesystem.test.ts
git commit -m "feat(file-organizer): implement filesystem tools (list_dir, get_file_info, search_files)"
```

---

### Task 7: Organizer Tools

**Files:**
- Create: `file-organizer/src/tools/organizer.ts`
- Create: `file-organizer/tests/tools/organizer.test.ts`

- [ ] **Step 1: Write failing tests**

Create `file-organizer/tests/tools/organizer.test.ts`:

```ts
import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { mkdtemp, rm, writeFile, mkdir, access } from "node:fs/promises";
import { join } from "node:path";
import {
  moveFile,
  renameFile,
  createFolder,
  deleteFile,
  done,
} from "../../src/tools/organizer.js";

let tempDir: string;

beforeEach(async () => {
  tempDir = await mkdtemp(join(tempDir || "", "fo-org-"));
});

afterEach(async () => {
  if (tempDir) await rm(tempDir, { recursive: true, force: true });
});

describe("moveFile", () => {
  it("moves a file to a new location", async () => {
    const src = join(tempDir, "source.txt");
    const dest = join(tempDir, "subdir", "moved.txt");
    await writeFile(src, "content");

    const result = await moveFile(src, dest);

    expect(result.success).toBe(true);
    await access(dest); // file exists at new location

    const { default: fs } = await import("node:fs/promises");
    await expect(fs.access(src)).rejects.toThrow(); // old location gone
  });

  it("returns error for non-existent source", async () => {
    const result = await moveFile(
      "/nonexistent/file.txt",
      "/tmp/dest.txt"
    );
    expect(result.success).toBe(false);
  });
});

describe("renameFile", () => {
  it("renames a file in the same directory", async () => {
    const filePath = join(tempDir, "old.txt");
    await writeFile(filePath, "content");

    const result = await renameFile(filePath, "new.txt");

    expect(result.success).toBe(true);
    await access(join(tempDir, "new.txt"));
  });
});

describe("createFolder", () => {
  it("creates a directory", async () => {
    const dirPath = join(tempDir, "new-folder", "nested");

    const result = await createFolder(dirPath);

    expect(result.success).toBe(true);
    const { default: fs } = await import("node:fs/promises");
    const stat = await fs.stat(dirPath);
    expect(stat.isDirectory()).toBe(true);
  });
});

describe("deleteFile", () => {
  it("deletes a file", async () => {
    const filePath = join(tempDir, "to-delete.txt");
    await writeFile(filePath, "bye");

    const result = await deleteFile(filePath);

    expect(result.success).toBe(true);
    const { default: fs } = await import("node:fs/promises");
    await expect(fs.access(filePath)).rejects.toThrow();
  });
});

describe("done", () => {
  it("returns summary", async () => {
    const result = await done("Organized 5 files");
    expect(result.success).toBe(true);
    expect(result.data).toEqual({ summary: "Organized 5 files" });
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd file-organizer && npx vitest run tests/tools/organizer.test.ts
```

Expected: FAIL — cannot find module

- [ ] **Step 3: Implement organizer tools**

Create `file-organizer/src/tools/organizer.ts`:

```ts
import { rename, unlink, mkdir } from "node:fs/promises";
import { join, dirname } from "node:path";
import type { ToolResult } from "./types.js";

export async function moveFile(
  source: string,
  destination: string
): Promise<ToolResult> {
  try {
    // Create destination directory if needed
    await mkdir(dirname(destination), { recursive: true });
    // Use rename for move (same filesystem)
    await rename(source, destination);
    return {
      success: true,
      data: { from: source, to: destination },
    };
  } catch (err: any) {
    return { success: false, error: err.message };
  }
}

export async function renameFile(
  filePath: string,
  newName: string
): Promise<ToolResult> {
  try {
    const dir = dirname(filePath);
    const newPath = join(dir, newName);
    await rename(filePath, newPath);
    return {
      success: true,
      data: { from: filePath, to: newPath },
    };
  } catch (err: any) {
    return { success: false, error: err.message };
  }
}

export async function createFolder(
  folderPath: string
): Promise<ToolResult> {
  try {
    await mkdir(folderPath, { recursive: true });
    return { success: true, data: { path: folderPath } };
  } catch (err: any) {
    return { success: false, error: err.message };
  }
}

export async function deleteFile(filePath: string): Promise<ToolResult> {
  try {
    await unlink(filePath);
    return { success: true, data: { path: filePath } };
  } catch (err: any) {
    return { success: false, error: err.message };
  }
}

export async function done(summary: string): Promise<ToolResult> {
  return { success: true, data: { summary } };
}
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd file-organizer && npx vitest run tests/tools/organizer.test.ts
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add file-organizer/src/tools/organizer.ts file-organizer/tests/tools/organizer.test.ts
git commit -m "feat(file-organizer): implement organizer tools (move, rename, create, delete, done)"
```

---

### Task 8: Tool Registry

**Files:**
- Create: `file-organizer/src/tools/index.ts`

- [ ] **Step 1: Create tool registry**

Create `file-organizer/src/tools/index.ts`:

```ts
import type { Tool } from "./types.js";
import { listDir, getFileInfo, searchFiles } from "./filesystem.js";
import {
  moveFile,
  renameFile,
  createFolder,
  deleteFile,
  done,
} from "./organizer.js";

export const tools: Record<string, Tool> = {
  list_dir: {
    name: "list_dir",
    description: "List contents of a directory",
    parameters: {
      type: "object",
      properties: {
        path: { type: "string", description: "Directory path" },
      },
      required: ["path"],
    },
    execute: (args) => listDir(args.path),
  },
  get_file_info: {
    name: "get_file_info",
    description: "Get detailed info about a file",
    parameters: {
      type: "object",
      properties: {
        path: { type: "string", description: "File path" },
      },
      required: ["path"],
    },
    execute: (args) => getFileInfo(args.path),
  },
  search_files: {
    name: "search_files",
    description: "Search for files matching a pattern",
    parameters: {
      type: "object",
      properties: {
        directory: { type: "string", description: "Search directory" },
        pattern: { type: "string", description: "Glob pattern" },
      },
      required: ["directory", "pattern"],
    },
    execute: (args) => searchFiles(args.directory, args.pattern),
  },
  move_file: {
    name: "move_file",
    description: "Move a file to a new location",
    parameters: {
      type: "object",
      properties: {
        source: { type: "string", description: "Source path" },
        destination: { type: "string", description: "Destination path" },
      },
      required: ["source", "destination"],
    },
    execute: (args) => moveFile(args.source, args.destination),
  },
  rename_file: {
    name: "rename_file",
    description: "Rename a file",
    parameters: {
      type: "object",
      properties: {
        path: { type: "string", description: "File path" },
        new_name: { type: "string", description: "New name" },
      },
      required: ["path", "new_name"],
    },
    execute: (args) => renameFile(args.path, args.new_name),
  },
  create_folder: {
    name: "create_folder",
    description: "Create a directory",
    parameters: {
      type: "object",
      properties: {
        path: { type: "string", description: "Directory path" },
      },
      required: ["path"],
    },
    execute: (args) => createFolder(args.path),
  },
  delete_file: {
    name: "delete_file",
    description: "Delete a file",
    parameters: {
      type: "object",
      properties: {
        path: { type: "string", description: "File path" },
      },
      required: ["path"],
    },
    execute: (args) => deleteFile(args.path),
  },
  done: {
    name: "done",
    description: "Signal completion",
    parameters: {
      type: "object",
      properties: {
        summary: { type: "string", description: "Summary" },
      },
      required: ["summary"],
    },
    execute: (args) => done(args.summary),
  },
};

export function getTool(name: string): Tool | undefined {
  return tools[name];
}

export function getToolDefinitions() {
  // Import definitions from the separate file
  const { TOOL_DEFINITIONS } = await import("./definitions.js");
  return TOOL_DEFINITIONS;
}
```

Wait — `getToolDefinitions` uses top-level await. Let me fix that:

```ts
import type { Tool } from "./types.js";
import { TOOL_DEFINITIONS } from "./definitions.js";
import { listDir, getFileInfo, searchFiles } from "./filesystem.js";
import {
  moveFile,
  renameFile,
  createFolder,
  deleteFile,
  done,
} from "./organizer.js";

export const tools: Record<string, Tool> = {
  list_dir: {
    name: "list_dir",
    description: "List contents of a directory",
    parameters: {
      type: "object",
      properties: {
        path: { type: "string", description: "Directory path" },
      },
      required: ["path"],
    },
    execute: (args) => listDir(args.path),
  },
  get_file_info: {
    name: "get_file_info",
    description: "Get detailed info about a file",
    parameters: {
      type: "object",
      properties: {
        path: { type: "string", description: "File path" },
      },
      required: ["path"],
    },
    execute: (args) => getFileInfo(args.path),
  },
  search_files: {
    name: "search_files",
    description: "Search for files matching a pattern",
    parameters: {
      type: "object",
      properties: {
        directory: { type: "string", description: "Search directory" },
        pattern: { type: "string", description: "Glob pattern" },
      },
      required: ["directory", "pattern"],
    },
    execute: (args) => searchFiles(args.directory, args.pattern),
  },
  move_file: {
    name: "move_file",
    description: "Move a file to a new location",
    parameters: {
      type: "object",
      properties: {
        source: { type: "string", description: "Source path" },
        destination: { type: "string", description: "Destination path" },
      },
      required: ["source", "destination"],
    },
    execute: (args) => moveFile(args.source, args.destination),
  },
  rename_file: {
    name: "rename_file",
    description: "Rename a file",
    parameters: {
      type: "object",
      properties: {
        path: { type: "string", description: "File path" },
        new_name: { type: "string", description: "New name" },
      },
      required: ["path", "new_name"],
    },
    execute: (args) => renameFile(args.path, args.new_name),
  },
  create_folder: {
    name: "create_folder",
    description: "Create a directory",
    parameters: {
      type: "object",
      properties: {
        path: { type: "string", description: "Directory path" },
      },
      required: ["path"],
    },
    execute: (args) => createFolder(args.path),
  },
  delete_file: {
    name: "delete_file",
    description: "Delete a file",
    parameters: {
      type: "object",
      properties: {
        path: { type: "string", description: "File path" },
      },
      required: ["path"],
    },
    execute: (args) => deleteFile(args.path),
  },
  done: {
    name: "done",
    description: "Signal completion",
    parameters: {
      type: "object",
      properties: {
        summary: { type: "string", description: "Summary" },
      },
      required: ["summary"],
    },
    execute: (args) => done(args.summary),
  },
};

export function getTool(name: string): Tool | undefined {
  return tools[name];
}

export { TOOL_DEFINITIONS };
```

- [ ] **Step 2: Commit**

```bash
git add file-organizer/src/tools/index.ts
git commit -m "feat(file-organizer): add tool registry mapping names to implementations"
```

---

### Task 9: Agent Loop

**Files:**
- Create: `file-organizer/src/agent/types.ts`
- Create: `file-organizer/src/agent/loop.ts`
- Create: `file-organizer/tests/agent/loop.test.ts`

- [ ] **Step 1: Write agent types**

Create `file-organizer/src/agent/types.ts`:

```ts
import type { Message, ToolCall } from "../llm/types.js";

/** Configuration for running the agent */
export interface AgentConfig {
  provider: string;
  model?: string;
  targetDir: string;
  autoMode: boolean; // skip approval
  dryRun: boolean; // show plan only, don't execute
}

/** A queued write action (not executed yet) */
export interface QueuedAction {
  toolCall: ToolCall;
  toolName: string;
  args: Record<string, any>;
}

/** Result of running the agent */
export interface AgentResult {
  actions: QueuedAction[];
  summary: string;
  executed: boolean;
}
```

- [ ] **Step 2: Write failing test**

Create `file-organizer/tests/agent/loop.test.ts`:

```ts
import { describe, it, expect } from "vitest";
import { buildSystemPrompt } from "../../src/agent/loop.js";

describe("buildSystemPrompt", () => {
  it("includes the target directory", () => {
    const prompt = buildSystemPrompt("/home/user/Downloads");
    expect(prompt).toContain("/home/user/Downloads");
  });

  it("includes tool usage instructions", () => {
    const prompt = buildSystemPrompt("/tmp");
    expect(prompt).toContain("list_dir");
    expect(prompt).toContain("move_file");
    expect(prompt).toContain("done");
  });
});
```

- [ ] **Step 3: Run test to verify it fails**

```bash
cd file-organizer && npx vitest run tests/agent/loop.test.ts
```

Expected: FAIL — cannot find module

- [ ] **Step 4: Implement agent loop**

Create `file-organizer/src/agent/loop.ts`:

```ts
import chalk from "chalk";
import { LLMClient } from "../llm/client.js";
import type { Message, ToolCall } from "../llm/types.js";
import { TOOL_DEFINITIONS } from "../tools/definitions.js";
import { tools, getTool } from "../tools/index.js";
import { listDir } from "../tools/filesystem.js";
import type { AgentConfig, QueuedAction, AgentResult } from "./types.js";

export function buildSystemPrompt(targetDir: string): string {
  return `You are a file organizer. You help users organize files into logical folders.

The user wants you to organize: ${targetDir}

You have these tools:
- list_dir: List directory contents
- get_file_info: Get file details
- search_files: Find files by pattern
- move_file: Move a file to a new location
- rename_file: Rename a file
- create_folder: Create a directory
- delete_file: Delete a file
- done: Signal you're finished with a summary

Analyze the files in the target directory and organize them into logical folders.
Group by: file type, project, date, or whatever makes sense.

When finished, call the "done" tool with a summary of what you did.`;
}

export async function runAgent(config: AgentConfig): Promise<AgentResult> {
  const client = new LLMClient(config.provider, config.model);
  const queuedActions: QueuedAction[] = [];

  // Scan the target directory first
  const scan = await listDir(config.targetDir);
  if (!scan.success) {
    throw new Error(`Cannot scan directory: ${scan.error}`);
  }

  const fileList = JSON.stringify(scan.data, null, 2);

  const messages: Message[] = [
    { role: "system", content: buildSystemPrompt(config.targetDir) },
    {
      role: "user",
      content: `Here are the files in ${config.targetDir}:\n\n${fileList}\n\nPlease organize these files.`,
    },
  ];

  let done = false;
  let summary = "";
  const maxIterations = 50; // safety limit

  for (let i = 0; i < maxIterations && !done; i++) {
    // Call LLM
    const response = await client.chat(messages, TOOL_DEFINITIONS);

    // Add assistant message to history
    messages.push(response.message);

    // Handle tool calls
    if (response.message.tool_calls && response.message.tool_calls.length > 0) {
      for (const toolCall of response.message.tool_calls) {
        const args = JSON.parse(toolCall.function.arguments);
        const toolName = toolCall.function.name;

        if (toolName === "done") {
          summary = args.summary || "Done";
          done = true;
          break;
        }

        // Check if this is a write tool (queue it)
        const isWrite = ["move_file", "rename_file", "create_folder", "delete_file"].includes(
          toolName
        );

        if (isWrite && !config.dryRun) {
          // Queue the action instead of executing
          queuedActions.push({ toolCall, toolName, args });

          // Feed back a "queued" result
          messages.push({
            role: "tool",
            tool_call_id: toolCall.id,
            content: JSON.stringify({
              success: true,
              queued: true,
              message: `Action queued for approval: ${toolName}`,
            }),
          });
        } else {
          // Execute read tools immediately
          const tool = getTool(toolName);
          if (tool) {
            const result = await tool.execute(args);
            messages.push({
              role: "tool",
              tool_call_id: toolCall.id,
              content: JSON.stringify(result),
            });
          } else {
            messages.push({
              role: "tool",
              tool_call_id: toolCall.id,
              content: JSON.stringify({ success: false, error: `Unknown tool: ${toolName}` }),
            });
          }
        }
      }
    } else {
      // No tool calls — LLM just responded with text
      // If no done tool was called, prompt it to continue
      messages.push({
        role: "user",
        content: "Please continue organizing or call done when finished.",
      });
    }
  }

  return {
    actions: queuedActions,
    summary,
    executed: false,
  };
}

/** Execute all queued actions */
export async function executeActions(
  actions: QueuedAction[]
): Promise<{ success: boolean; results: any[] }> {
  const results = [];

  for (const action of actions) {
    const tool = getTool(action.toolName);
    if (!tool) {
      results.push({ success: false, error: `Unknown tool: ${action.toolName}` });
      continue;
    }

    const result = await tool.execute(action.args);
    results.push(result);

    if (!result.success) {
      console.error(chalk.red(`Failed: ${action.toolName} — ${result.error}`));
    } else {
      console.log(chalk.green(`✓ ${action.toolName}`));
    }
  }

  return {
    success: results.every((r: any) => r.success),
    results,
  };
}
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
cd file-organizer && npx vitest run tests/agent/loop.test.ts
```

Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add file-organizer/src/agent/types.ts file-organizer/src/agent/loop.ts file-organizer/tests/agent/loop.test.ts
git commit -m "feat(file-organizer): implement agent loop with tool queuing"
```

---

### Task 10: CLI Entry Point

**Files:**
- Create: `file-organizer/src/config.ts`
- Create: `file-organizer/src/index.ts`

- [ ] **Step 1: Create config loader**

Create `file-organizer/src/config.ts`:

```ts
import { Command } from "commander";
import { DEFAULT_PROVIDER, DEFAULT_MODEL } from "./llm/providers.js";

export interface CLIOptions {
  targetDir: string;
  provider: string;
  model: string;
  auto: boolean;
  dryRun: boolean;
}

export function parseArgs(): CLIOptions {
  const program = new Command();

  program
    .name("file-organizer")
    .description("AI-powered file organizer")
    .version("0.1.0")
    .argument("<directory>", "Directory to organize")
    .option("-p, --provider <name>", "LLM provider", DEFAULT_PROVIDER)
    .option("-m, --model <name>", "Model name")
    .option("--auto", "Skip approval, execute immediately")
    .option("--dry-run", "Show plan without executing")
    .parse();

  const opts = program.opts();
  const dir = program.args[0];

  // Resolve ~ to home directory
  const resolvedDir = dir.startsWith("~")
    ? dir.replace("~", process.env.HOME || process.env.USERPROFILE || "")
    : dir;

  return {
    targetDir: resolvedDir,
    provider: opts.provider,
    model: opts.model || DEFAULT_MODEL,
    auto: opts.auto || false,
    dryRun: opts.dryRun || false,
  };
}
```

- [ ] **Step 2: Create CLI entry point**

Create `file-organizer/src/index.ts`:

```ts
#!/usr/bin/env node
import chalk from "chalk";
import { parseArgs } from "./config.js";
import { runAgent, executeActions } from "./agent/loop.js";
import { createInterface } from "node:readline";

async function main() {
  const options = parseArgs();

  console.log(chalk.bold.cyan("\n🗂️  File Organizer\n"));
  console.log(chalk.dim(`Target: ${options.targetDir}`));
  console.log(chalk.dim(`Provider: ${options.provider}`));
  console.log(chalk.dim(`Model: ${options.model}\n`));

  // Run the agent
  console.log(chalk.yellow("Analyzing files...\n"));

  const result = await runAgent(options);

  if (result.actions.length === 0) {
    console.log(chalk.green("Nothing to organize!"));
    return;
  }

  // Show the plan
  console.log(chalk.bold("\n📋 Plan:\n"));
  for (const action of result.actions) {
    const { toolName, args } = action;
    switch (toolName) {
      case "move_file":
        console.log(
          chalk.dim("  ➡️ ") +
            chalk.white(args.source) +
            chalk.dim(" → ") +
            chalk.green(args.destination)
        );
        break;
      case "rename_file":
        console.log(
          chalk.dim("  ✏️  ") +
            chalk.white(args.path) +
            chalk.dim(" → ") +
            chalk.green(args.new_name)
        );
        break;
      case "create_folder":
        console.log(chalk.dim("  📁 ") + chalk.green(args.path));
        break;
      case "delete_file":
        console.log(chalk.dim("  🗑️  ") + chalk.red(args.path));
        break;
    }
  }

  console.log(chalk.dim(`\n  ${result.actions.length} actions queued\n`));

  // Dry run — stop here
  if (options.dryRun) {
    console.log(chalk.yellow("Dry run — no changes made.\n"));
    return;
  }

  // Auto mode — execute without asking
  if (options.auto) {
    console.log(chalk.yellow("Executing...\n"));
    await executeActions(result.actions);
    console.log(chalk.green("\n✅ Done!\n"));
    return;
  }

  // Ask for approval
  const rl = createInterface({ input: process.stdin, output: process.stdout });
  const answer = await new Promise<string>((resolve) => {
    rl.question(chalk.bold("Execute? (y/n) "), resolve);
  });
  rl.close();

  if (answer.toLowerCase() === "y") {
    console.log(chalk.yellow("\nExecuting...\n"));
    await executeActions(result.actions);
    console.log(chalk.green("\n✅ Done!\n"));
  } else {
    console.log(chalk.dim("\nCancelled. No changes made.\n"));
  }
}

main().catch((err) => {
  console.error(chalk.red(`Error: ${err.message}`));
  process.exit(1);
});
```

- [ ] **Step 3: Add bin entry to package.json**

Update `file-organizer/package.json` — add to the existing content:

```json
{
  "bin": {
    "file-organizer": "dist/index.js"
  }
}
```

- [ ] **Step 4: Build and verify**

```bash
cd file-organizer && npx tsc && node dist/index.js --help
```

Expected: Shows help text with usage instructions

- [ ] **Step 5: Commit**

```bash
git add file-organizer/src/config.ts file-organizer/src/index.ts file-organizer/package.json
git commit -m "feat(file-organizer): add CLI entry point with approval flow"
```

---

### Task 11: Run All Tests

- [ ] **Step 1: Run full test suite**

```bash
cd file-organizer && npx vitest run
```

Expected: All tests pass

- [ ] **Step 2: Fix any failures**

If tests fail, fix the issues and re-run.

- [ ] **Step 3: Final commit**

```bash
git add -A
git commit -m "chore(file-organizer): ensure all tests pass"
```

---

### Task 12: End-to-End Smoke Test

- [ ] **Step 1: Create a test directory with sample files**

```bash
mkdir -p /tmp/fo-smoke-test
touch /tmp/fo-smoke-test/{photo.jpg,song.mp3,doc.pdf,notes.txt,report.docx,app.exe}
```

- [ ] **Step 2: Run with --dry-run (no API key needed for this)**

```bash
cd file-organizer && OPENCODE_API_KEY=test node dist/index.js /tmp/fo-smoke-test --dry-run
```

Expected: Should fail gracefully with API error (since test key is invalid), but shows the CLI parses args correctly and attempts to scan the directory.

- [ ] **Step 3: Clean up**

```bash
rm -rf /tmp/fo-smoke-test
```

- [ ] **Step 4: Final commit**

```bash
git add -A
git commit -m "chore(file-organizer): smoke test complete"
```
