import { FileText, FolderOpen } from "lucide-react";

export function FileCard({ args = {}, content }) {
  const path = args.path || content?.path || content?.file || "";
  const isList = Array.isArray(content?.files) || Array.isArray(content);
  const files = Array.isArray(content) ? content : content?.files || [];
  const text = typeof content === "string" ? content : content?.content || content?.text || "";

  return (
    <div className="tool-body">
      <div className="tool-query">
        {isList ? <FolderOpen size={15} /> : <FileText size={15} />}
        <span>{path || "File operation"}</span>
      </div>
      {isList ? (
        <div className="file-list">
          {files.slice(0, 12).map((file, index) => (
            <div className="file-row" key={`${file.path || file.name || index}`}>
              <FileText size={14} />
              <span>{file.path || file.name || String(file)}</span>
            </div>
          ))}
        </div>
      ) : (
        <pre className="file-preview">{text || JSON.stringify(content, null, 2)}</pre>
      )}
    </div>
  );
}
