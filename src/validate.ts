import { Value } from "typebox/value";
import type { TSchema } from "typebox";
import type { Tool, ToolCall } from "./types.ts";

function formatPath(error: any): string {
	if (!error || typeof error !== "object") return "(root)";
	const path = error.path as (string | number)[] | undefined;
	return path && path.length > 0 ? path.join("/") : "(root)";
}

/**
 * Result of validating tool arguments.
 */
export interface ValidationResult {
	valid: boolean;
	args?: Record<string, unknown>;
	errors?: string;
}

/**
 * Validates and coerces tool-call arguments against a typebox schema.
 *
 * - Uses typebox `Value.Check` for schema validation.
 * - Throws a descriptive error if validation fails.
 *
 * Ported from @earendil-works/pi-ai utils/validation.ts (adapted API).
 */
export function validateToolArguments(
	schema: TSchema,
	args: Record<string, unknown>,
): ValidationResult {
	const clone = structuredClone(args) as Record<string, unknown>;

	if (Value.Check(schema, clone)) {
		return { valid: true, args: clone };
	}

	const errors =
		Array.from(Value.Errors(schema, clone) as any)
			.map((error: any) => `  - ${formatPath(error.path)}: ${error.message}`)
			.join("\n") || "Unknown validation error";

	return { valid: false, errors };
}

/**
 * High-level validation using a Tool definition and a ToolCall.
 * Throws if validation fails.
 */
export function validateToolArgumentsOrThrow(tool: Tool, toolCall: ToolCall): Record<string, unknown> {
	const schema = tool.parameters as unknown as TSchema;
	const result = validateToolArguments(schema, toolCall.arguments);
	if (!result.valid) {
		throw new Error(
			`Validation failed for tool "${toolCall.name}":\n${result.errors}\n\nReceived arguments:\n${JSON.stringify(toolCall.arguments, null, 2)}`,
		);
	}
	return result.args!;
}
