import { ChevronDown, ChevronRight, FileSearch, Globe, Wrench } from "lucide-react";
import { useState } from "react";
import { FileCard } from "./FileCard.jsx";
import { MemoryCard } from "./MemoryCard.jsx";
import { WebSearchCard } from "./WebSearchCard.jsx";

function ToolIcon({ tool }) {
  if (tool === "web_search" || tool === "fetch_url") {
    return <Globe size={15} />;
  }
  if (tool.includes("file") || tool.includes("read") || tool.includes("list")) {
    return <FileSearch size={15} />;
  }
  return <Wrench size={15} />;
}

function ToolContent({ tool, args, content }) {
  if (tool === "web_search" || tool === "fetch_url") {
    return <WebSearchCard args={args} content={content} />;
  }
  if (tool.includes("memory")) {
    return <MemoryCard content={content} />;
  }
  if (tool.includes("file") || tool.includes("read") || tool.includes("list")) {
    return <FileCard args={args} content={content} />;
  }
  return <pre className="file-preview">{JSON.stringify(content ?? args, null, 2)}</pre>;
}

export function ToolCard({ call }) {
  const [open, setOpen] = useState(call.opened ?? false);
  const title = call.tool?.replaceAll("_", " ") || "tool";

  return (
    <section className={`tool-card ${call.status === "running" ? "running" : ""}`}>
      <button className="tool-header" type="button" onClick={() => setOpen(!open)}>
        {open ? <ChevronDown size={15} /> : <ChevronRight size={15} />}
        <ToolIcon tool={call.tool || ""} />
        <span>{title}</span>
        <small>{call.status === "running" ? "Running" : "Done"}</small>
      </button>
      {open ? <ToolContent tool={call.tool || ""} args={call.args} content={call.content} /> : null}
    </section>
  );
}
