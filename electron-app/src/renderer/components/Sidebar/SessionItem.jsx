import { MessageSquare, Pencil, Trash2, X, Check, MoreHorizontal, Copy } from "lucide-react";
import { useState, useRef, useEffect, useCallback } from "react";
import { createPortal } from "react-dom";

export function SessionItem({ session, active, streaming, onClick, onRename, onDelete }) {
  const [menuOpen, setMenuOpen] = useState(false);
  const [menuPos, setMenuPos] = useState({ top: 0, left: 0 });
  const [renaming, setRenaming] = useState(false);
  const [renameValue, setRenameValue] = useState("");
  const wrapRef = useRef(null);
  const menuRef = useRef(null);
  const inputRef = useRef(null);

  useEffect(() => {
    if (renaming && inputRef.current) {
      inputRef.current.focus();
      inputRef.current.select();
    }
  }, [renaming]);

  const closeMenu = useCallback(() => setMenuOpen(false), []);

  useEffect(() => {
    if (!menuOpen) return;
    function handleDown(e) {
      if (menuRef.current && !menuRef.current.contains(e.target)) {
        closeMenu();
      }
    }
    function handleScroll() {
      closeMenu();
    }
    document.addEventListener("mousedown", handleDown, true);
    document.addEventListener("scroll", handleScroll, true);
    return () => {
      document.removeEventListener("mousedown", handleDown, true);
      document.removeEventListener("scroll", handleScroll, true);
    };
  }, [menuOpen, closeMenu]);

  function calcPos(triggerEl) {
    const rect = triggerEl.getBoundingClientRect();
    let top = rect.bottom + 4;
    let left = rect.left;
    if (top + 180 > window.innerHeight) {
      top = rect.top - 180;
      if (top < 4) top = 4;
    }
    if (left + 180 > window.innerWidth) {
      left = window.innerWidth - 180;
    }
    return { top, left };
  }

  function handleContextMenu(e) {
    e.preventDefault();
    setMenuPos(calcPos(wrapRef.current));
    setMenuOpen(true);
  }

  function handleMenuClick(e) {
    e.preventDefault();
    e.stopPropagation();
    if (menuOpen) {
      setMenuOpen(false);
    } else {
      setMenuPos(calcPos(wrapRef.current));
      setMenuOpen(true);
    }
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
    <div className={`session-item-wrap ${active ? "active" : ""} ${streaming ? "streaming" : ""}`} ref={wrapRef}>
      <button
        className="session-item"
        type="button"
        onClick={() => onClick(session.id)}
        onContextMenu={handleContextMenu}
        title={session.title}
      >
        <div className="session-icon-wrap">
          <MessageSquare size={14} className="session-icon" />
        </div>
        <span className="session-title-text">{session.title || "New session"}</span>
        <span className="session-meta">
          {session.message_count ? <small>{session.message_count}</small> : null}
          <div
            className="session-more-btn"
            onClick={handleMenuClick}
            role="button"
            tabIndex={0}
          >
            <MoreHorizontal size={14} />
          </div>
        </span>
      </button>
      {menuOpen ? createPortal(
        <div
          className="context-menu"
          ref={menuRef}
          style={{ position: "fixed", top: menuPos.top, left: menuPos.left, zIndex: 9999 }}
        >
          <button type="button" onClick={() => { navigator.clipboard.writeText(session.id.toString()); closeMenu(); }}>
            <Copy size={13} />
            <span>Copy ID</span>
          </button>
          <button type="button" onClick={startRename}>
            <Pencil size={13} />
            <span>Rename</span>
          </button>
          <div className="context-menu-divider" />
          <button type="button" className="danger" onClick={handleDelete}>
            <Trash2 size={13} />
            <span>Delete</span>
          </button>
        </div>,
        document.body
      ) : null}
    </div>
  );
}
