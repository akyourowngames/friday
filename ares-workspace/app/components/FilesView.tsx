"use client";

import { Archive, Code2, File, FileImage, FileText, MessageSquarePlus, Search, Trash2, UploadCloud } from "lucide-react";
import { useMemo, useRef, useState } from "react";
import type { WorkspaceFile } from "@/lib/types";

interface Props {
  files: WorkspaceFile[];
  upload: (files: FileList | File[]) => void;
  attach: (file: WorkspaceFile) => void;
  remove: (file: WorkspaceFile) => void;
  uploading: boolean;
}

function formatBytes(bytes: number) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
  return `${(bytes / 1024 ** 3).toFixed(1)} GB`;
}

function kind(file: WorkspaceFile) {
  if (file.type.startsWith("image/")) return "image";
  if (/json|javascript|typescript|python|html|css|xml|yaml|shell/.test(file.type) || /\.(py|ts|tsx|js|jsx|css|html|json|ya?ml|sh|ps1|go|rs|java|cpp|c)$/i.test(file.name)) return "code";
  if (/zip|tar|gzip|rar|7z/.test(file.type)) return "archive";
  return "document";
}

function FileIcon({ file }: { file: WorkspaceFile }) {
  const type = kind(file);
  if (type === "image") return <FileImage />;
  if (type === "code") return <Code2 />;
  if (type === "archive") return <Archive />;
  if (type === "document") return <FileText />;
  return <File />;
}

export function FilesView({ files, upload, attach, remove, uploading }: Props) {
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState("all");
  const [dragging, setDragging] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const visible = useMemo(() => files.filter(file => file.name.toLowerCase().includes(query.toLowerCase()) && (filter === "all" || kind(file) === filter)), [files, query, filter]);
  const totalBytes = files.reduce((sum, file) => sum + file.size, 0);
  const pick = () => inputRef.current?.click();
  const receive = (incoming: FileList | null) => { if (incoming?.length) upload(incoming); };

  return <section className="view is-active"><div className="page-scroll">
    <div className="page-head"><div><p className="eyebrow">PERSISTENT CONTEXT LIBRARY</p><h1>Files</h1><p>Upload once, reuse across threads. Content stays in your local Ares data directory.</p></div><button className="primary-btn" onClick={pick} disabled={uploading}><UploadCloud />{uploading ? "Uploading…" : "Upload files"}</button></div>
    <div className="storage-strip"><div><small>LIBRARY</small><strong>{files.length} file{files.length === 1 ? "" : "s"}</strong></div><div><small>STORAGE USED</small><strong>{formatBytes(totalBytes)}</strong></div><div><small>PER FILE LIMIT</small><strong>25 MB</strong></div><div><small>CHAT BATCH LIMIT</small><strong>50 MB</strong></div></div>
    <div className={`drop-zone ${dragging ? "is-dragging" : ""}`} onDragEnter={event => { event.preventDefault(); setDragging(true); }} onDragOver={event => event.preventDefault()} onDragLeave={() => setDragging(false)} onDrop={event => { event.preventDefault(); setDragging(false); receive(event.dataTransfer.files); }}><span className="drop-icon"><UploadCloud /></span><div><strong>Drop files into Ares</strong><small>PDF, documents, code, images, archives, and audio</small></div><button className="secondary-btn" onClick={pick}>Browse computer</button></div>
    <div className="library-toolbar"><div className="search-field"><Search /><input value={query} onChange={event => setQuery(event.target.value)} placeholder="Search your library" /></div><div className="segmented">{["all", "document", "image", "code"].map(value => <button className={filter === value ? "is-active" : ""} key={value} onClick={() => setFilter(value)}>{value[0].toUpperCase() + value.slice(1)}{value === "document" ? "s" : value === "image" ? "s" : ""}</button>)}</div></div>
    <div className="file-grid">{visible.map(file => <article className="file-card" key={file.id}><span className="file-preview"><FileIcon file={file} /></span><strong title={file.name}>{file.name}</strong><small>{formatBytes(file.size)} · {kind(file).toUpperCase()}</small><small>Updated {new Date(file.modified_at).toLocaleString()}</small><div className="file-card-actions"><button onClick={() => attach(file)}><MessageSquarePlus />Attach to chat</button><button onClick={() => remove(file)}><Trash2 />Delete</button></div></article>)}{!visible.length && <div className="empty-state"><div><FileText size={26} /><p>{files.length ? "No files match this view." : "Your reusable file library is empty. Upload a file to start."}</p></div></div>}</div>
    <input ref={inputRef} type="file" multiple hidden onChange={event => { receive(event.target.files); event.target.value = ""; }} />
  </div></section>;
}
