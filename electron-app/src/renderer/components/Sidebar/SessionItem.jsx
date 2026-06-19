import { MessageSquare, Pencil, Trash2, X, Check } from "lucide-react";
import { useState, useRef, useEffect } from "react";

export function SessionItem({ session, active, onClick, onRename, onDelete }) {
  const [menuOpen, setMenuOpen] = useState(false);
  const [renaming, setRenaming] = useState(false);
  const [renameValue, setRenameValue] = useState("");
  const menuRef = useRef(null);
  const inputRef = useRef(null);

  useEffect(() => {
    if (renaming && inputRef.current) {
      inputRef.current.focus();
      inputRef.current.select();
    }
  }, [renaming]);

  useEffect(() => {
    if (!menuOpen) return;
    function handleClick(e) {
      if (menuRef.current && !menuRef.current.contains(e.target)) {
        setMenuOpen(false);
      }
    }
    document.addEventListener("mousedown", handleClick);
    return () => document.removeEventListener("mousedown", handleClick);
  }, [menuOpen]);

  function handleContextMenu(e) {
    e.preventDefault();
    setMenuOpen(true);
  }

  function startRename() {
    setMenuOpen(false);
    setRenameValue(session.title || "");
    setRenaming(true);
  }

  function commitRename() {
    const trimmed = renameValue.trim();
    if (trimmed && trimmed !== session.title) {
      onRename(session.id, trimmed);
    }
    setRenaming(false);
  }

  function handleDelete() {
    setMenuOpen(false);
    onDelete(session.id);
  }

  if (renaming) {
    return (
      <div className="session-item renaming">
        <input
          ref={inputRef}
          className="rename-input"
          value={renameValue}
          onChange={(e) => setRenameValue(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") commitRename();
            if (e.key === "Escape") setRenaming(false);
          }}
          onBlur={commitRename}
        />
        <button className="tiny-icon" type="button" onClick={commitRename} title="Save">
          <Check size={13} />
        </button>
        <button className="tiny-icon" type="button" onClick={() => setRenaming(false)} title="Cancel">
          <X size={13} />
        </button>
      </div>
    );
  }

  return (
    <div className="session-item-wrap" ref={menuRef}>
      <button
        className={`session-item ${active ? "active" : ""}`}
        type="button"
        onClick={() => onClick(session.id)}
        onContextMenu={handleContextMenu}
        title={session.title}
      >
        <MessageSquare size={14} />
        <span>{session.title || "New session"}</span>
        {session.message_count ? <small>{session.message_count}</small> : null}
      </button>
      {menuOpen ? (
        <div className="context-menu">
          <button type="button" onClick={startRename}>
            <Pencil size={13} />
            <span>Rename</span>
          </button>
          <button type="button" className="danger" onClick={handleDelete}>
            <Trash2 size={13} />
            <span>Delete</span>
          </button>
        </div>
      ) : null}
    </div>
  );
}
