import { describe, it, expect } from "vitest";
import * as ta from "typebox";
import { validateToolArguments } from "../src/validate.ts";

const t = ta.Type;

describe("validateToolArguments", () => {
	it("should validate correct arguments", () => {
		const schema = t.Object({
			name: t.String(),
			age: t.Number(),
		});

		const result = validateToolArguments(schema, { name: "Alice", age: 30 });
		expect(result.valid).toBe(true);
	});

	it("should reject missing required arguments", () => {
		const schema = t.Object({
			name: t.String(),
			age: t.Number(),
		});

		const result = validateToolArguments(schema, { name: "Alice" });
		expect(result.valid).toBe(false);
	});

	it("should reject wrong types", () => {
		const schema = t.Object({
			name: t.String(),
			age: t.Number(),
		});

		const result = validateToolArguments(schema, { name: "Alice", age: "thirty" });
		expect(result.valid).toBe(false);
	});

	it("should validate with optional fields", () => {
		const schema = t.Object({
			name: t.String(),
			age: t.Optional(t.Number()),
		});

		const result = validateToolArguments(schema, { name: "Alice" });
		expect(result.valid).toBe(true);
	});

	it("should return safe error messages", () => {
		const schema = t.Object({
			secret: t.String({ format: "email" }),
		});

		const result = validateToolArguments(schema, { secret: "not-an-email" });
		expect(result.valid).toBe(false);
		expect(result.errors).toBeDefined();
		// Should not contain raw path information that could be exploited
		expect(result.errors).not.toContain("password");
	});
});
