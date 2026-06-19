import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { CodeBlock } from "./CodeBlock.jsx";

export function MarkdownRenderer({ content }) {
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      components={{
        code({ inline, className, children, ...props }) {
          const match = /language-(\w+)/.exec(className || "");
          const value = String(children).replace(/\n$/, "");
          if (inline) {
            return (
              <code className="inline-code" {...props}>
                {children}
              </code>
            );
          }
          return <CodeBlock language={match?.[1] || "text"} value={value} />;
        },
        a({ children, href }) {
          return (
            <a href={href} target="_blank" rel="noreferrer">
              {children}
            </a>
          );
        }
      }}
    >
      {content || ""}
    </ReactMarkdown>
  );
}
