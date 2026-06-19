import { Check, Copy } from "lucide-react";
import { useState } from "react";

export function CodeBlock({ language = "text", value = "" }) {
  const [copied, setCopied] = useState(false);

  async function copy() {
    await navigator.clipboard.writeText(value);
    setCopied(true);
    setTimeout(() => setCopied(false), 1200);
  }

  return (
    <div className="code-block">
      <div className="code-header">
        <span>{language}</span>
        <button className="ghost-button compact" type="button" onClick={copy}>
          {copied ? <Check size={14} /> : <Copy size={14} />}
          {copied ? "Copied" : "Copy"}
        </button>
      </div>
      <pre>
        <code>{value}</code>
      </pre>
    </div>
  );
}
