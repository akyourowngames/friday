# Document Writer Policy

Runtime loads sections by heading. The doc_write tool reads this file to decide
document type, format, output path, and delivery method. Edit this file to add
new document types or change delivery behavior — no code changes needed.

## Document Types

- database_schema: SQL DDL for tables, indexes, constraints. Format: .sql.
- api_spec: API endpoint definitions, request/response shapes. Format: .md.
- project_outline: High-level project structure, phases, milestones. Format: .md.
- technical_spec: Detailed technical design with architecture decisions. Format: .md.
- report: Summary of progress, status, or findings. Format: .md.
- code_snippet: A standalone piece of code. Format: matches language (.py, .js, .ts, etc.).
- config_file: Configuration or manifest. Format: .json or .yaml.
- readme: Project readme or documentation. Format: .md.
- other: Anything that doesn't fit the above. Format: .md by default.

## Format Detection

Determine the output format from the content, not the file extension the user
might have in mind. Rules:

- If the user says "schema", "DDL", "tables", "SQL" → .sql
- If the user says "API spec", "endpoints", "routes" → .md
- If the user says "outline", "plan", "roadmap" → .md
- If the user says "config", "manifest", "settings" → .json
- If the user says "code", "snippet", "script", "function" → matches language
- If the user says "report", "summary", "status" → .md
- If the user says "readme", "documentation" → .md
- Default → .md

## Structure Planning

Before generating content, plan the structure. The LLM should produce a brief
outline (sections, tables, endpoints) and then fill it in. This avoids flat
 dumps and ensures complete, well-organized documents.

For database schemas:
- Identify entities from the request context
- Define tables with columns, types, constraints
- Add indexes for common query patterns
- Include foreign key relationships

For API specs:
- List endpoints with method, path, description
- Define request/response shapes
- Include error cases

For technical specs:
- Problem statement
- Proposed solution
- Architecture decisions
- Trade-offs considered

For project outlines:
- Phases with milestones
- Key deliverables per phase
- Dependencies and risks

## Output Paths

All documents go to the docs directory (configured as settings.docs_dir).

Naming rules:
- Use kebab-case for filenames: "database schema" → "database-schema.sql"
- Prefix with project slug if a project is specified: "budget-tracker-schema.sql"
- Add a date suffix for reports: "progress-report-2026-05-31.md"
- Never overwrite an existing file — append a number: "schema-2.sql" if "schema.sql" exists

## Delivery Rules

Decide how to deliver the finished document. The tool auto-decides unless the
user overrides with the delivery parameter.

- .sql, .py, .js, .ts, .json, .yaml, .toml → open locally (code editors handle these natively)
- .md under 3000 chars → show in terminal (fits in one screen)
- .md over 3000 chars → open locally
- Report type + telegram configured → send via telegram
- delivery="telegram" override → always send via telegram
- delivery="terminal" override → always show in terminal
- delivery="open" override → always open locally

When opening locally:
- Windows: os.startfile(path)
- macOS: subprocess.run(["open", path])
- Linux: subprocess.run(["xdg-open", path])

When sending via telegram:
- Use the telegram_watcher tool's send_cli_message
- Send a summary as the caption, attach the file
- If the file is over 5MB, send only the summary text

When showing in terminal:
- Print the full content with clear section headers
- If content is over 2000 chars, truncate with "...[truncated]" and note the file path

## Content Quality Rules

- Write for a reader who will use this document, not for display
- Be specific and grounded in the project context — no generic filler
- For code: use proper syntax, include imports, follow the project's style
- For schemas: use consistent naming (snake_case), include comments
- For specs: include concrete examples, not just abstract descriptions
- Never invent project details that weren't in the request or context
