import { Brain } from "lucide-react";

export function MemoryCard({ content }) {
  const memories = Array.isArray(content) ? content : content?.memories || content?.results || [];

  return (
    <div className="tool-body">
      {memories.length ? (
        <div className="memory-list">
          {memories.map((memory, index) => (
            <div className="memory-row" key={memory.id || memory.fact_id || index}>
              <Brain size={14} />
              <span>{memory.content || memory.fact_text || memory.text || String(memory)}</span>
            </div>
          ))}
        </div>
      ) : (
        <pre className="file-preview">{JSON.stringify(content, null, 2)}</pre>
      )}
    </div>
  );
}
