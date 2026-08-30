"use client";

import { Fragment, type ReactNode } from "react";
import { renderMarkdown, type MarkdownLine, type MarkdownSpan } from "@/src/markdown";

/**
 * Render assistant text as proper semantic HTML instead of raw pre-wrapped
 * text. Uses the shared `renderMarkdown` parser so the web chat shows the
 * same structure as the TUI: headings, bullets, ordered lists, blockquotes,
 * fenced code blocks with language hints, and tables.
 */
export function MarkdownView({ source, className }: { source: string; className?: string }) {
	const lines = renderMarkdown(source);
	return (
		<div className={className ?? "harness-md"}>
			{lines.map((line, i) => (
				<MarkdownLineView key={i} line={line} />
			))}
		</div>
	);
}

function MarkdownLineView({ line }: { line: MarkdownLine }) {
	const span = line.spans[0];

	// Single-span lines map to a block element.
	if (line.spans.length === 1 && span) {
		if (span.kind === "text" && span.heading) {
			const level = span.heading;
			const Tag = (`h${level}` as unknown) as "h1" | "h2" | "h3" | "h4" | "h5" | "h6";
			return (
				<Tag className={`harness-md-heading is-h${level}`}>
					<span className="harness-md-heading-bar" aria-hidden="true" />
					<span>
						<InlineSpans nodes={parseInline(span.text)} />
					</span>
				</Tag>
			);
		}
		if (span.kind === "code-block") {
			return (
				<pre className="harness-md-code" data-lang={span.lang || undefined}>
					<code className={`lang-${span.lang || "plain"}`}>{span.text}</code>
				</pre>
			);
		}
		if (span.kind === "bullet") {
			return (
				<div
					className="harness-md-bullet"
					style={{ paddingLeft: `${(span.depth ?? 0) * 1.25}rem` }}
				>
					<span className="harness-md-bullet-marker" aria-hidden="true">•</span>
					<span>
						<InlineSpans nodes={parseInline(span.text)} />
					</span>
				</div>
			);
		}
		if (span.kind === "ordered") {
			return (
				<div className="harness-md-ordered">
					<span className="harness-md-ordered-marker" aria-hidden="true">{span.index}.</span>
					<span>
						<InlineSpans nodes={parseInline(span.text)} />
					</span>
				</div>
			);
		}
		if (span.kind === "quote") {
			return (
				<blockquote className="harness-md-quote">
					<InlineSpans nodes={parseInline(span.text)} />
				</blockquote>
			);
		}
		if (span.kind === "rule") {
			return <hr className="harness-md-rule" />;
		}
		if (span.kind === "text") {
			return (
				<p className="harness-md-paragraph">
					<InlineSpans nodes={parseInline(span.text)} />
				</p>
			);
		}
	}

	// Multi-span line: render as a paragraph.
	return (
		<p className="harness-md-paragraph">
			{line.spans.map((s, i) => (
				<Fragment key={i}>{renderBlockSpan(s)}</Fragment>
			))}
		</p>
	);
}

function renderBlockSpan(span: MarkdownSpan): ReactNode {
	if (span.kind === "bullet")
		return (
			<span
				className="harness-md-bullet"
				style={{ paddingLeft: `${(span.depth ?? 0) * 1.25}rem` }}
			>
				<span className="harness-md-bullet-marker" aria-hidden="true">•</span>{" "}
				<InlineSpans nodes={parseInline(span.text)} />
			</span>
		);
	if (span.kind === "ordered")
		return (
			<span className="harness-md-ordered">
				<span className="harness-md-ordered-marker" aria-hidden="true">{span.index}.</span>{" "}
				<InlineSpans nodes={parseInline(span.text)} />
			</span>
		);
	if (span.kind === "quote")
		return (
			<blockquote className="harness-md-quote">
				<InlineSpans nodes={parseInline(span.text)} />
			</blockquote>
		);
	if (span.kind === "rule") return <hr className="harness-md-rule" />;
	if (span.kind === "text")
		return <InlineSpans nodes={parseInline(span.text)} />;
	return null;
}

// ----- Inline token model -----

type InlineNode =
	| { type: "text"; text: string }
	| { type: "bold"; text: string }
	| { type: "italic"; text: string }
	| { type: "code"; text: string }
	| { type: "link"; href: string; text: string };

/** Tokenize a string into inline runs: text, bold, italic, code, link. */
function parseInline(input: string): InlineNode[] {
	const out: InlineNode[] = [];
	let i = 0;
	while (i < input.length) {
		// URL — match http(s) and split off trailing punctuation.
		if (/https?:\/\//.test(input[i]! + (input[i + 1] ?? ""))) {
			const match = /https?:\/\/[^\s<>]+/.exec(input.slice(i));
			if (match) {
				const raw = match[0];
				let url = raw;
				let suffix = "";
				while (/[),.;!?\]}]$/.test(url)) {
					suffix = url.slice(-1) + suffix;
					url = url.slice(0, -1);
				}
				out.push({ type: "link", href: url, text: url });
				i += url.length + suffix.length;
				continue;
			}
		}
		if (input[i] === "`") {
			const end = input.indexOf("`", i + 1);
			if (end > i) {
				out.push({ type: "code", text: input.slice(i + 1, end) });
				i = end + 1;
				continue;
			}
		}
		if (input.startsWith("**", i)) {
			const end = input.indexOf("**", i + 2);
			if (end > i) {
				out.push({ type: "bold", text: input.slice(i + 2, end) });
				i = end + 2;
				continue;
			}
		}
		if (input[i] === "*" && input[i + 1] !== "*") {
			const end = input.indexOf("*", i + 1);
			if (end > i) {
				out.push({ type: "italic", text: input.slice(i + 1, end) });
				i = end + 1;
				continue;
			}
		}
		let end = i;
		while (end < input.length && !/[\`*]/.test(input[end]!) && !/https?:\/\//.test(input.slice(end, end + 8))) end++;
		out.push({ type: "text", text: input.slice(i, end) });
		i = end;
	}
	// Merge adjacent text runs.
	const merged: InlineNode[] = [];
	for (const node of out) {
		const last = merged[merged.length - 1];
		if (last && last.type === "text" && node.type === "text") last.text += node.text;
		else merged.push(node);
	}
	return merged;
}

function InlineSpans({ nodes }: { nodes: InlineNode[] }) {
	return (
		<>
			{nodes.map((node, i) => {
				if (node.type === "text") return <Fragment key={i}>{node.text}</Fragment>;
				if (node.type === "bold") return <strong key={i}>{node.text}</strong>;
				if (node.type === "italic") return <em key={i}>{node.text}</em>;
				if (node.type === "code") return <code key={i} className="harness-md-inline-code">{node.text}</code>;
				return (
					<a key={i} href={node.href} target="_blank" rel="noopener noreferrer" className="harness-md-link">
						{node.text}
					</a>
				);
			})}
		</>
	);
}
