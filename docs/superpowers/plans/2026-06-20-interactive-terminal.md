# Interactive Terminal Panel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a persistent interactive terminal panel (node-pty + xterm.js) to the Ares desktop app with agent-driven commands, user typing, and send-to-chat (`@terminal:15`) references.

**Architecture:** node-pty spawns a real shell in the Electron main process. xterm.js renders it in the renderer. A `TerminalManager` class manages PTY lifecycle. IPC bridges main↔renderer. A new `terminal_exec` tool lets Ares send commands to the visible terminal. A `TerminalStore` (Zustand) manages panel state and the send-to-chat feature.

**Tech Stack:** node-pty (native PTY), @xterm/xterm + @xterm/addon-fit + @xterm/addon-web-links (terminal UI), Zustand (state), Electron IPC (bridge)

---

## File Structure

| Action | File | Responsibility |
|--------|------|---------------|
| Create | `electron-app/src/main/terminal-manager.js` | PTY lifecycle management |
| Create | `electron-app/src/renderer/stores/terminalStore.js` | Terminal panel state + send-to-chat |
| Create | `electron-app/src/renderer/components/TerminalPanel.jsx` | xterm.js terminal component |
| Create | `electron-app/src/renderer/components/TerminalHeader.jsx` | Terminal panel header bar |
| Modify | `electron-app/src/main/index.js` | Register terminal IPC handlers |
| Modify | `electron-app/src/main/preload.js` | Add terminal API to window.aresDesktop |
| Modify | `electron-app/src/renderer/App.jsx` | Split panel layout (chat left, terminal right) |
| Modify | `electron-app/src/renderer/components/Composer.jsx` | @terminal reference chips |
| Modify | `electron-app/src/renderer/styles/components.css` | Terminal panel styles |
| Modify | `ares/tools.py` | Add terminal_exec tool definition + handler |
| Modify | `ares/server.py` | Add terminal WebSocket message handling |
| Modify | `ares/agent.py` | Store terminal output history for @terminal refs |

---

### Task 1: Install dependencies

**Files:**
- Modify: `electron-app/package.json`

- [ ] **Step 1: Install node-pty and xterm.js packages**

Run in `electron-app/` directory:
```bash
cd electron-app
npm install node-pty
npm install @xterm/xterm @xterm/addon-fit @xterm/addon-web-links
```

- [ ] **Step 2: Verify installation**

Run: `cd electron-app && npm ls node-pty @xterm/xterm @xterm/addon-fit @xterm/addon-web-links`
Expected: All four packages listed with versions

- [ ] **Step 3: Commit**

```bash
git add electron-app/package.json electron-app/package-lock.json
git commit -m "deps: install node-pty, xterm.js, and addons for terminal panel"
```

---

### Task 2: TerminalManager — PTY lifecycle management

**Files:**
- Create: `electron-app/src/main/terminal-manager.js`

- [ ] **Step 1: Create TerminalManager class**

Create `electron-app/src/main/terminal-manager.js`:

```javascript
'use strict';

const os = require('os');
const { v4: uuidv4 } = require('crypto');

/**
 * Manages PTY process lifecycle for the interactive terminal panel.
 * One active terminal session at a time.
 */
class TerminalManager {
  constructor() {
    this.ptyProcess = null;
    this.sessionId = null;
    this.mainWindow = null;
  }

  /**
   * Set the BrowserWindow reference (called after window creation).
   * @param {import('electron').BrowserWindow} win
   */
  setWindow(win) {
    this.mainWindow = win;
  }

  /**
   * Spawn a new PTY shell process.
   * @returns {{ sessionId: string }} The session ID.
   */
  create() {
    // Kill existing PTY if any
    if (this.ptyProcess) {
      this.kill();
    }

    const pty = require('node-pty');

    // Select shell based on platform
    let shell;
    let shellArgs = [];
    if (process.platform === 'win32') {
      shell = process.env.COMSPEC || 'powershell.exe';
      // PowerShell: use -NoLogo to keep it clean
      if (shell.toLowerCase().includes('powershell')) {
        shellArgs = ['-NoLogo'];
      }
    } else {
      shell = process.env.SHELL || '/bin/bash';
    }

    this.sessionId = this._generateId();

    this.ptyProcess = pty.spawn(shell, shellArgs, {
      name: 'xterm-256color',
      cols: 80,
      rows: 24,
      cwd: process.env.HOME || process.env.USERPROFILE,
      env: { ...process.env, TERM: 'xterm-256color' },
    });

    // Forward PTY output to renderer
    this.ptyProcess.onData((data) => {
      if (this.mainWindow && !this.mainWindow.isDestroyed()) {
        this.mainWindow.webContents.send('terminal:data', data);
      }
    });

    // Handle PTY exit
    this.ptyProcess.onExit(({ exitCode, signal }) => {
      if (this.mainWindow && !this.mainWindow.isDestroyed()) {
        this.mainWindow.webContents.send('terminal:exit', { exitCode, signal });
      }
      this.ptyProcess = null;
      this.sessionId = null;
    });

    // Notify renderer that PTY is ready
    if (this.mainWindow && !this.mainWindow.isDestroyed()) {
      this.mainWindow.webContents.send('terminal:create', { sessionId: this.sessionId });
    }

    return { sessionId: this.sessionId };
  }

  /**
   * Write data to the PTY (user input or command).
   * @param {string} data
   */
  write(data) {
    if (this.ptyProcess) {
      this.ptyProcess.write(data);
    }
  }

  /**
   * Resize the PTY dimensions.
   * @param {number} cols
   * @param {number} rows
   */
  resize(cols, rows) {
    if (this.ptyProcess) {
      try {
        this.ptyProcess.resize(cols, rows);
      } catch (e) {
        // Resize can fail if PTY is exiting
      }
    }
  }

  /**
   * Kill the active PTY process.
   */
  kill() {
    if (this.ptyProcess) {
      try {
        this.ptyProcess.kill();
      } catch (e) {
        // Process may already be dead
      }
      this.ptyProcess = null;
      this.sessionId = null;
    }
  }

  /**
   * Check if a PTY session is active.
   * @returns {boolean}
   */
  isActive() {
    return this.ptyProcess !== null;
  }

  /**
   * Generate a unique session ID.
   * @returns {string}
   */
  _generateId() {
    return 'term-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 8);
  }
}

module.exports = TerminalManager;
```

- [ ] **Step 2: Commit**

```bash
git add electron-app/src/main/terminal-manager.js
git commit -m "feat: add TerminalManager class for PTY lifecycle"
```

---

### Task 3: Register IPC handlers in main process

**Files:**
- Modify: `electron-app/src/main/index.js`

- [ ] **Step 1: Add TerminalManager import and IPC handlers**

In `electron-app/src/main/index.js`, add the TerminalManager import at the top (after existing imports):

```javascript
const TerminalManager = require('./terminal-manager');
const terminalManager = new TerminalManager();
```

Then add the IPC handlers after the existing `ares:get-app-version` handler (after line 60):

```javascript
// ── Terminal IPC handlers ──────────────────────────────────────

ipcMain.handle('ares:terminal:create', () => {
  return terminalManager.create();
});

ipcMain.on('ares:terminal:write', (_, data) => {
  terminalManager.write(data);
});

ipcMain.on('ares:terminal:resize', (_, { cols, rows }) => {
  terminalManager.resize(cols, rows);
});

ipcMain.on('ares:terminal:kill', () => {
  terminalManager.kill();
});

ipcMain.handle('ares:terminal:isActive', () => {
  return terminalManager.isActive();
});
```

- [ ] **Step 2: Pass window reference to TerminalManager**

In the `createWindow()` function, after the window is created (after `mainWindow = new BrowserWindow(...)`), add:

```javascript
terminalManager.setWindow(mainWindow);
```

- [ ] **Step 3: Kill terminal on window close**

In the `mainWindow.on('closed', ...)` handler, add terminal cleanup:

```javascript
mainWindow.on('closed', () => {
  terminalManager.kill();
  terminalManager.setWindow(null);
  mainWindow = null;
});
```

- [ ] **Step 4: Commit**

```bash
git add electron-app/src/main/index.js
git commit -m "feat: register terminal IPC handlers in main process"
```

---

### Task 4: Extend preload script with terminal API

**Files:**
- Modify: `electron-app/src/main/preload.js`

- [ ] **Step 1: Add terminal API to window.aresDesktop**

In `electron-app/src/main/preload.js`, add the terminal section to the `contextBridge.exposeInMainWorld` call. Find the existing `window.aresDesktop` object and add the `terminal` property:

```javascript
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('aresDesktop', {
  getServerUrl: () => ipcRenderer.invoke('ares:get-server-url'),
  restartServer: () => ipcRenderer.invoke('ares:restart-server'),
  getAppVersion: () => ipcRenderer.invoke('ares:get-app-version'),
  platform: process.platform,

  // ── Terminal API ──────────────────────────────────────────
  terminal: {
    /** Create a new PTY session. Returns { sessionId }. */
    create: () => ipcRenderer.invoke('ares:terminal:create'),

    /** Write data (string) to the PTY. */
    write: (data) => ipcRenderer.send('ares:terminal:write', data),

    /** Resize the PTY to new dimensions. */
    resize: (cols, rows) => ipcRenderer.send('ares:terminal:resize', { cols, rows }),

    /** Kill the active PTY process. */
    kill: () => ipcRenderer.send('ares:terminal:kill'),

    /** Check if a PTY is active. Returns boolean. */
    isActive: () => ipcRenderer.invoke('ares:terminal:isActive'),

    /** Subscribe to PTY output data (string chunks). */
    onData: (callback) => {
      const handler = (_, data) => callback(data);
      ipcRenderer.on('terminal:data', handler);
      return () => ipcRenderer.removeListener('terminal:data', handler);
    },

    /** Subscribe to PTY exit event. */
    onExit: (callback) => {
      const handler = (_, info) => callback(info);
      ipcRenderer.on('terminal:exit', handler);
      return () => ipcRenderer.removeListener('terminal:exit', handler);
    },

    /** Subscribe to PTY creation event. */
    onCreate: (callback) => {
      const handler = (_, info) => callback(info);
      ipcRenderer.on('terminal:create', handler);
      return () => ipcRenderer.removeListener('terminal:create', handler);
    },

    /** Remove all terminal IPC listeners. */
    removeAllListeners: () => {
      ipcRenderer.removeAllListeners('terminal:data');
      ipcRenderer.removeAllListeners('terminal:exit');
      ipcRenderer.removeAllListeners('terminal:create');
    },
  },
});
```

- [ ] **Step 2: Commit**

```bash
git add electron-app/src/main/preload.js
git commit -m "feat: extend preload with terminal API"
```

---

### Task 5: TerminalStore — Zustand state management

**Files:**
- Create: `electron-app/src/renderer/stores/terminalStore.js`

- [ ] **Step 1: Create TerminalStore**

Create `electron-app/src/renderer/stores/terminalStore.js`:

```javascript
import { create } from 'zustand';

const useTerminalStore = create((set, get) => ({
  // ── State ──────────────────────────────────────────────────
  isOpen: false,
  isConnected: false,
  sessionId: null,
  lastSelection: null,        // { text, startLine, endLine }
  commandHistory: [],         // recent commands for context

  // ── Actions ────────────────────────────────────────────────

  /** Toggle terminal panel visibility. */
  togglePanel: () => {
    const { isOpen } = get();
    if (isOpen) {
      get().closePanel();
    } else {
      get().openPanel();
    }
  },

  /** Show terminal panel and create PTY if needed. */
  openPanel: async () => {
    set({ isOpen: true });

    // Create PTY if not already connected
    if (!get().isConnected) {
      await get().createTerminal();
    }
  },

  /** Hide terminal panel. */
  closePanel: () => {
    set({ isOpen: false });
  },

  /** Create a new PTY session via the main process. */
  createTerminal: async () => {
    const aresDesktop = window.aresDesktop;
    if (!aresDesktop?.terminal) {
      console.error('Terminal API not available');
      return;
    }

    const result = await aresDesktop.terminal.create();
    set({ isConnected: true, sessionId: result.sessionId });

    // Listen for PTY exit
    aresDesktop.terminal.onExit(({ exitCode }) => {
      console.log(`Terminal exited with code ${exitCode}`);
      set({ isConnected: false, sessionId: null });
    });
  },

  /** Send input data to the PTY. */
  writeToTerminal: (data) => {
    const aresDesktop = window.aresDesktop;
    if (aresDesktop?.terminal && get().isConnected) {
      aresDesktop.terminal.write(data);
    }
  },

  /** Resize the PTY to match container dimensions. */
  resizeTerminal: (cols, rows) => {
    const aresDesktop = window.aresDesktop;
    if (aresDesktop?.terminal && get().isConnected) {
      aresDesktop.terminal.resize(cols, rows);
    }
  },

  /** Update the current terminal selection. */
  setSelection: (text, startLine, endLine) => {
    set({ lastSelection: { text, startLine, endLine } });
  },

  /** Clear the current selection. */
  clearSelection: () => {
    set({ lastSelection: null });
  },

  /** Format selection as @terminal reference and return it for the composer. */
  sendSelectionToChat: () => {
    const { lastSelection } = get();
    if (!lastSelection) return null;

    const { text, startLine, endLine } = lastSelection;
    const ref = {
      type: 'terminal',
      text,
      startLine,
      endLine,
      label: startLine === endLine
        ? `@terminal:${startLine}`
        : `@terminal:${startLine}-${endLine}`,
    };

    return ref;
  },

  /** Add a command to history (for Ares context). */
  addCommandToHistory: (cmd) => {
    set((state) => ({
      commandHistory: [...state.commandHistory.slice(-99), cmd],
    }));
  },

  /** Kill the PTY and reset state. */
  killTerminal: () => {
    const aresDesktop = window.aresDesktop;
    if (aresDesktop?.terminal) {
      aresDesktop.terminal.kill();
    }
    set({ isConnected: false, sessionId: null });
  },
}));

export default useTerminalStore;
```

- [ ] **Step 2: Commit**

```bash
git add electron-app/src/renderer/stores/terminalStore.js
git commit -m "feat: add TerminalStore for terminal panel state"
```

---

### Task 6: TerminalHeader component

**Files:**
- Create: `electron-app/src/renderer/components/TerminalHeader.jsx`

- [ ] **Step 1: Create TerminalHeader**

Create `electron-app/src/renderer/components/TerminalHeader.jsx`:

```jsx
import React from 'react';
import useTerminalStore from '../stores/terminalStore';

/**
 * Terminal panel header bar with title and close button.
 */
export default function TerminalHeader() {
  const { sessionId, isConnected, killTerminal, closePanel } = useTerminalStore();

  const handleClose = () => {
    killTerminal();
    closePanel();
  };

  return (
    <div className="terminal-header">
      <div className="terminal-header-left">
        <span className="terminal-dot" />
        <span className="terminal-title">Terminal</span>
        {isConnected && sessionId && (
          <span className="terminal-session-id">{sessionId}</span>
        )}
      </div>
      <div className="terminal-header-right">
        {!isConnected && (
          <button
            className="terminal-restart-btn"
            onClick={() => useTerminalStore.getState().createTerminal()}
            title="Restart terminal"
          >
            ↻
          </button>
        )}
        <button
          className="terminal-close-btn"
          onClick={handleClose}
          title="Close terminal"
        >
          ✕
        </button>
      </div>
    </div>
  );
}
```

- [ ] **Step 2: Commit**

```bash
git add electron-app/src/renderer/components/TerminalHeader.jsx
git commit -m "feat: add TerminalHeader component"
```

---

### Task 7: TerminalPanel component — xterm.js integration

**Files:**
- Create: `electron-app/src/renderer/components/TerminalPanel.jsx`

- [ ] **Step 1: Create TerminalPanel**

Create `electron-app/src/renderer/components/TerminalPanel.jsx`:

```jsx
import React, { useEffect, useRef, useCallback } from 'react';
import { Terminal } from '@xterm/xterm';
import { FitAddon } from '@xterm/addon-fit';
import { WebLinksAddon } from '@xterm/addon-web-links';
import '@xterm/xterm/css/xterm.css';
import useTerminalStore from '../stores/terminalStore';

/**
 * Interactive terminal panel using xterm.js + node-pty.
 * Renders a real shell that the user can type in.
 */
export default function TerminalPanel() {
  const containerRef = useRef(null);
  const termRef = useRef(null);
  const fitAddonRef = useRef(null);
  const unsubscribersRef = useRef([]);

  const {
    isConnected,
    writeToTerminal,
    resizeTerminal,
    setSelection,
    clearSelection,
  } = useTerminalStore();

  // Initialize xterm.js terminal
  useEffect(() => {
    if (!containerRef.current) return;

    const term = new Terminal({
      theme: {
        background: '#0e0e12',
        foreground: '#e0e0e0',
        cursor: '#6B5B95',
        cursorAccent: '#0e0e12',
        selectionBackground: '#6B5B9540',
        selectionForeground: '#ffffff',
        black: '#1a1a2e',
        red: '#e74c3c',
        green: '#2ecc71',
        yellow: '#f39c12',
        blue: '#3498db',
        magenta: '#9b59b6',
        cyan: '#1abc9c',
        white: '#ecf0f1',
        brightBlack: '#7f8c8d',
        brightRed: '#e74c3c',
        brightGreen: '#2ecc71',
        brightYellow: '#f1c40f',
        brightBlue: '#3498db',
        brightMagenta: '#9b59b6',
        brightCyan: '#1abc9c',
        brightWhite: '#ffffff',
      },
      fontFamily: '"Cascadia Code", "Fira Code", "JetBrains Mono", "Consolas", monospace',
      fontSize: 14,
      lineHeight: 1.2,
      cursorBlink: true,
      cursorStyle: 'bar',
      scrollback: 10000,
      allowProposedApi: true,
    });

    const fitAddon = new FitAddon();
    const webLinksAddon = new WebLinksAddon();

    term.loadAddon(fitAddon);
    term.loadAddon(webLinksAddon);

    term.open(containerRef.current);

    // Fit to container after a small delay to ensure layout is settled
    requestAnimationFrame(() => {
      fitAddon.fit();
    });

    // Store refs
    termRef.current = term;
    fitAddonRef.current = fitAddon;

    // ── Wire up data flow ────────────────────────────────────

    // User types in xterm → send to PTY
    const dataDisposable = term.onData((data) => {
      writeToTerminal(data);
    });

    // Subscribe to PTY output → write to xterm
    const aresDesktop = window.aresDesktop;
    if (aresDesktop?.terminal) {
      const unsubData = aresDesktop.terminal.onData((data) => {
        term.write(data);
      });
      unsubscribersRef.current.push(unsubData);
    }

    // ── Selection tracking ───────────────────────────────────
    const selectionDisposable = term.onSelectionChange(() => {
      const selection = term.getSelection();
      if (!selection) {
        clearSelection();
        return;
      }

      const position = term.getSelectionPosition();
      if (!position) {
        clearSelection();
        return;
      }

      // Get absolute line numbers from buffer
      const buf = term.buffer.active;
      setSelection(selection, position.start.y, position.end.y);
    });

    // ── Resize handling ──────────────────────────────────────
    const handleResize = () => {
      fitAddon.fit();
      const dims = fitAddon.proposeDimensions();
      if (dims) {
        resizeTerminal(dims.cols, dims.rows);
      }
    };

    const resizeObserver = new ResizeObserver(handleResize);
    resizeObserver.observe(containerRef.current);

    // Also handle window resize
    window.addEventListener('resize', handleResize);

    // ── Cleanup ──────────────────────────────────────────────
    return () => {
      window.removeEventListener('resize', handleResize);
      resizeObserver.disconnect();
      selectionDisposable.dispose();
      dataDisposable.dispose();
      unsubscribersRef.current.forEach((unsub) => unsub());
      unsubscribersRef.current = [];
      term.dispose();
      termRef.current = null;
      fitAddonRef.current = null;
    };
  }, []);  // Empty deps — initialize once

  // Handle Ctrl+Enter for send-to-chat
  useEffect(() => {
    const term = termRef.current;
    if (!term) return;

    const keyDisposable = term.onKey(({ key, domEvent }) => {
      if (domEvent.ctrlKey && domEvent.key === 'Enter') {
        domEvent.preventDefault();
        const ref = useTerminalStore.getState().sendSelectionToChat();
        if (ref) {
          // Dispatch a custom event that Composer can listen to
          window.dispatchEvent(new CustomEvent('terminal:sendToChat', { detail: ref }));
        }
      }
    });

    return () => keyDisposable.dispose();
  }, [isConnected]);

  return (
    <div className="terminal-container" ref={containerRef} />
  );
}
```

- [ ] **Step 2: Commit**

```bash
git add electron-app/src/renderer/components/TerminalPanel.jsx
git commit -m "feat: add TerminalPanel component with xterm.js"
```

---

### Task 8: Split panel layout in App.jsx

**Files:**
- Modify: `electron-app/src/renderer/App.jsx`

- [ ] **Step 1: Add terminal imports and split panel layout**

In `electron-app/src/renderer/App.jsx`, add imports for the terminal components:

```jsx
import TerminalPanel from './components/TerminalPanel';
import TerminalHeader from './components/TerminalHeader';
import useTerminalStore from './stores/terminalStore';
```

Then modify the main layout to include the terminal panel. Replace the main content area with a split layout:

```jsx
function App() {
  const { isOpen: isTerminalOpen } = useTerminalStore();
  const [splitPos, setSplitPos] = React.useState(60); // 60% chat, 40% terminal
  const isDragging = React.useRef(false);

  const handleDividerMouseDown = (e) => {
    isDragging.current = true;
    e.preventDefault();
  };

  React.useEffect(() => {
    const handleMouseMove = (e) => {
      if (!isDragging.current) return;
      const container = document.querySelector('.app-main');
      if (!container) return;
      const rect = container.getBoundingClientRect();
      const pct = ((e.clientX - rect.left) / rect.width) * 100;
      setSplitPos(Math.min(Math.max(pct, 30), 80)); // 30%–80% range
    };

    const handleMouseUp = () => {
      isDragging.current = false;
    };

    document.addEventListener('mousemove', handleMouseMove);
    document.addEventListener('mouseup', handleMouseUp);
    return () => {
      document.removeEventListener('mousemove', handleMouseMove);
      document.removeEventListener('mouseup', handleMouseUp);
    };
  }, []);

  // Keyboard shortcut: Ctrl+` to toggle terminal
  React.useEffect(() => {
    const handleKeyDown = (e) => {
      if (e.ctrlKey && e.key === '`') {
        e.preventDefault();
        useTerminalStore.getState().togglePanel();
      }
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, []);

  return (
    <div className="app-layout">
      {isTerminalOpen ? (
        <>
          <div className="chat-panel" style={{ width: `${splitPos}%` }}>
            <Sidebar />
            <ChatArea />
          </div>
          <div
            className="split-divider"
            onMouseDown={handleDividerMouseDown}
          />
          <div className="terminal-panel">
            <TerminalHeader />
            <TerminalPanel />
          </div>
        </>
      ) : (
        <div className="chat-panel full-width">
          <Sidebar />
          <ChatArea />
        </div>
      )}
      <StatusBar />
    </div>
  );
}
```

- [ ] **Step 2: Commit**

```bash
git add electron-app/src/renderer/App.jsx
git commit -m "feat: add split panel layout with terminal"
```

---

### Task 9: @terminal reference chips in Composer

**Files:**
- Modify: `electron-app/src/renderer/components/Composer.jsx`

- [ ] **Step 1: Add terminal reference chip support**

In `Composer.jsx`, add state for terminal references and the `@terminal:sendToChat` event listener:

```jsx
import React, { useState, useEffect, useRef } from 'react';

export default function Composer({ onSend }) {
  const [text, setText] = useState('');
  const [terminalRefs, setTerminalRefs] = useState([]);

  // Listen for send-to-chat events from the terminal
  useEffect(() => {
    const handleTerminalRef = (e) => {
      const ref = e.detail;
      if (ref) {
        setTerminalRefs((prev) => [...prev, ref]);
        // Optionally insert the label at cursor position
        setText((prev) => prev ? `${prev} ${ref.label}` : ref.label);
      }
    };

    window.addEventListener('terminal:sendToChat', handleTerminalRef);
    return () => window.removeEventListener('terminal:sendToChat', handleTerminalRef);
  }, []);

  const handleSend = () => {
    if (!text.trim() && terminalRefs.length === 0) return;

    onSend({
      content: text,
      terminalRefs: terminalRefs.length > 0 ? terminalRefs : undefined,
    });

    setText('');
    setTerminalRefs([]);
  };

  const removeTerminalRef = (index) => {
    setTerminalRefs((prev) => prev.filter((_, i) => i !== index));
  };

  return (
    <div className="composer">
      {/* Terminal reference chips */}
      {terminalRefs.length > 0 && (
        <div className="terminal-refs">
          {terminalRefs.map((ref, i) => (
            <span key={i} className="terminal-ref-chip">
              {ref.label}
              <button
                className="terminal-ref-remove"
                onClick={() => removeTerminalRef(i)}
              >
                ✕
              </button>
            </span>
          ))}
        </div>
      )}

      <textarea
        className="composer-input"
        value={text}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            handleSend();
          }
        }}
        placeholder="Message Ares..."
      />

      <button className="composer-send" onClick={handleSend} disabled={!text.trim()}>
        ↑
      </button>
    </div>
  );
}
```

- [ ] **Step 2: Commit**

```bash
git add electron-app/src/renderer/components/Composer.jsx
git commit -m "feat: add @terminal reference chips in Composer"
```

---

### Task 10: Terminal panel CSS styles

**Files:**
- Modify: `electron-app/src/renderer/styles/components.css`

- [ ] **Step 1: Add terminal panel styles**

Append to `electron-app/src/renderer/styles/components.css`:

```css
/* ── Terminal Panel ────────────────────────────────────────────── */

.app-layout {
  display: flex;
  height: 100vh;
  overflow: hidden;
}

.chat-panel {
  display: flex;
  min-width: 0;
  overflow: hidden;
}

.chat-panel.full-width {
  width: 100%;
}

/* Split divider */
.split-divider {
  width: 4px;
  background: #1a1a2e;
  cursor: col-resize;
  flex-shrink: 0;
  position: relative;
  transition: background 0.15s;
}

.split-divider:hover,
.split-divider:active {
  background: #6B5B95;
}

.split-divider::after {
  content: '⋮';
  position: absolute;
  top: 50%;
  left: 50%;
  transform: translate(-50%, -50%);
  color: #555;
  font-size: 12px;
  pointer-events: none;
}

.split-divider:hover::after {
  color: #6B5B95;
}

/* Terminal panel */
.terminal-panel {
  display: flex;
  flex-direction: column;
  min-width: 200px;
  width: 40%;
  background: #0e0e12;
  border-left: 1px solid #1a1a2e;
}

/* Terminal header */
.terminal-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 12px;
  background: #0a0a0e;
  border-bottom: 1px solid #1a1a2e;
  min-height: 36px;
}

.terminal-header-left {
  display: flex;
  align-items: center;
  gap: 8px;
}

.terminal-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #2ecc71;
}

.terminal-title {
  font-size: 12px;
  font-weight: 600;
  color: #e0e0e0;
  text-transform: uppercase;
  letter-spacing: 0.5px;
}

.terminal-session-id {
  font-size: 10px;
  color: #555;
  font-family: monospace;
}

.terminal-header-right {
  display: flex;
  align-items: center;
  gap: 4px;
}

.terminal-restart-btn,
.terminal-close-btn {
  background: none;
  border: none;
  color: #666;
  cursor: pointer;
  padding: 4px 6px;
  border-radius: 4px;
  font-size: 12px;
  line-height: 1;
  transition: all 0.15s;
}

.terminal-restart-btn:hover {
  color: #2ecc71;
  background: #2ecc7115;
}

.terminal-close-btn:hover {
  color: #e74c3c;
  background: #e74c3c15;
}

/* Terminal container (xterm.js renders here) */
.terminal-container {
  flex: 1;
  padding: 4px;
  overflow: hidden;
}

/* xterm.js overrides for dark theme */
.terminal-container .xterm {
  height: 100%;
}

.terminal-container .xterm-viewport {
  overflow-y: auto !important;
}

/* ── Composer @terminal chips ──────────────────────────────────── */

.terminal-refs {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  padding: 6px 12px 0;
}

.terminal-ref-chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 3px 8px;
  background: #6B5B9520;
  border: 1px solid #6B5B9540;
  border-radius: 12px;
  font-size: 11px;
  color: #9b8ec4;
  font-family: monospace;
}

.terminal-ref-remove {
  background: none;
  border: none;
  color: #9b8ec4;
  cursor: pointer;
  padding: 0;
  font-size: 10px;
  line-height: 1;
  opacity: 0.6;
}

.terminal-ref-remove:hover {
  opacity: 1;
  color: #e74c3c;
}
```

- [ ] **Step 2: Commit**

```bash
git add electron-app/src/renderer/styles/components.css
git commit -m "feat: add terminal panel and @terminal chip styles"
```

---

### Task 11: Python backend — terminal_exec tool

**Files:**
- Modify: `ares/tools.py` (add tool definition + handler)
- Modify: `ares/server.py` (add terminal WebSocket messages)

- [ ] **Step 1: Add terminal_exec tool definition**

In `ares/tools.py`, add to `get_tool_definitions()` (after the last `_tool(...)` call):

```python
    _tool(
        "terminal_exec",
        "Send a command to the interactive terminal panel and wait for it to complete. Use when you need visible output the user can see, or when running commands that need interactive shell features. For simple one-shot commands that don't need visibility, prefer run_command.",
        {
            "command": {"type": "string", "description": "Shell command to execute in the terminal"},
            "wait": {"type": "boolean", "description": "Wait for command to complete (default true)"},
            "timeout": {"type": "integer", "description": "Max seconds to wait for completion (default 30)"},
        },
        required=["command"],
    ),
```

- [ ] **Step 2: Add terminal_exec handler to ToolExecutor**

In the `ToolExecutor` class in `ares/tools.py`, add a handler method:

```python
    def _terminal_exec(self, args: dict) -> str:
        """Send a command to the interactive terminal panel.

        This tool sends a command to the frontend terminal via a callback.
        The callback is set by the server when a WebSocket client connects.
        """
        command = args["command"]
        wait = bool(args.get("wait", True))
        timeout = int(args.get("timeout", 30))

        if not hasattr(self, '_terminal_exec_callback') or self._terminal_exec_callback is None:
            return "Error: No terminal connected. Open the terminal panel in the desktop app first."

        try:
            result = self._terminal_exec_callback(command, wait=wait, timeout=timeout)
            return result
        except Exception as e:
            return f"Error executing in terminal: {e}"
```

- [ ] **Step 3: Register terminal_exec in the handlers dict**

In `ToolExecutor.execute()`, add to the `handlers` dict:

```python
            "terminal_exec": self._terminal_exec,
```

- [ ] **Step 4: Add terminal WebSocket message handling to server.py**

In `ares/server.py`, add a `_terminal_output_buffer` dict to store terminal output for commands:

```python
    def __init__(self, ...):
        # ... existing init code ...
        self._terminal_output_buffer: dict[str, str] = {}
        self._terminal_command_events: dict[str, asyncio.Event] = {}
```

Add the `terminal:exec` handler in `handle_message()`:

```python
        elif msg_type == "terminal:exec":
            await self._handle_terminal_exec(websocket, message)
```

Add the handler method:

```python
    async def _handle_terminal_exec(self, websocket: Any, message: dict) -> None:
        """Forward a command to the frontend terminal."""
        command = message.get("command", "")
        cmd_id = f"cmd-{id(message)}"

        self._terminal_output_buffer[cmd_id] = ""
        self._terminal_command_events[cmd_id] = asyncio.Event()

        # Send command to all connected frontends
        await self._send(websocket, {
            "type": "terminal:exec",
            "command": command,
            "cmd_id": cmd_id,
        })

        # Wait for the result (with timeout)
        timeout = message.get("timeout", 30)
        try:
            await asyncio.wait_for(
                self._terminal_command_events[cmd_id].wait(),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            return f"Error: Terminal command timed out after {timeout}s"
        finally:
            # Cleanup
            output = self._terminal_output_buffer.pop(cmd_id, "")
            self._terminal_command_events.pop(cmd_id, None)

        return output
```

Add handling for `terminal:exec_result` in `handle_message()`:

```python
        elif msg_type == "terminal:exec_result":
            cmd_id = message.get("cmd_id", "")
            output = message.get("output", "")
            if cmd_id in self._terminal_command_events:
                self._terminal_output_buffer[cmd_id] = output
                self._terminal_command_events[cmd_id].set()
```

- [ ] **Step 5: Wire terminal_exec_callback to ToolExecutor**

In the `__init__` of `AresServer`, set up the callback:

```python
    def __init__(self, ...):
        # ... existing init code ...
        self.agent.tool_executor._terminal_exec_callback = self._terminal_exec_via_websocket
```

Add the callback method:

```python
    async def _terminal_exec_via_websocket(self, command: str, wait: bool = True, timeout: int = 30) -> str:
        """Send a command to the frontend terminal and optionally wait for result."""
        if not self._connected_websockets:
            return "Error: No desktop client connected."

        cmd_id = f"cmd-{int(asyncio.get_event_loop().time() * 1000)}"
        self._terminal_output_buffer[cmd_id] = ""
        event = asyncio.Event()
        self._terminal_command_events[cmd_id] = event

        # Send to first connected client
        ws = self._connected_websockets[0]
        await self._send(ws, {
            "type": "terminal:exec",
            "command": command,
            "cmd_id": cmd_id,
        })

        if not wait:
            return f"Command sent to terminal: {command}"

        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return f"Error: Terminal command timed out after {timeout}s"
        finally:
            output = self._terminal_output_buffer.pop(cmd_id, "")
            self._terminal_command_events.pop(cmd_id, None)

        return output if output else f"Command completed (no output): {command}"
```

- [ ] **Step 6: Commit**

```bash
git add ares/tools.py ares/server.py
git commit -m "feat: add terminal_exec tool and WebSocket routing"
```

---

### Task 12: Handle terminal:exec in frontend (renderer)

**Files:**
- Modify: `electron-app/src/renderer/stores/terminalStore.js`

- [ ] **Step 1: Add terminal:exec handler in TerminalStore**

In `terminalStore.js`, add a method to handle incoming `terminal:exec` commands from the backend. This needs to be called from the WebSocket handler.

Add to the store:

```javascript
  /** Handle a terminal:exec command from the backend. */
  handleExecCommand: (command, cmdId, websocket) => {
    const store = get();
    if (!store.isConnected) {
      // Auto-open terminal if not open
      store.openPanel().then(() => {
        // Write command + Enter to execute it
        setTimeout(() => {
          store.writeToTerminal(command + '\r');
          // Collect output for a few seconds, then send result back
          store._collectOutput(command, cmdId, websocket);
        }, 500);
      });
      return;
    }

    // Write command + Enter
    store.writeToTerminal(command + '\r');
    store._collectOutput(command, cmdId, websocket);
  },

  /** Collect terminal output after a command and send result back. */
  _collectOutput: (command, cmdId, websocket) => {
    let output = '';
    const startTime = Date.now();
    const maxWait = 30000; // 30s max

    // Subscribe to terminal data
    const aresDesktop = window.aresDesktop;
    if (!aresDesktop?.terminal) return;

    const unsub = aresDesktop.terminal.onData((data) => {
      output += data;
    });

    // Poll for command completion
    const interval = setInterval(() => {
      const elapsed = Date.now() - startTime;
      if (elapsed >= maxWait) {
        clearInterval(interval);
        unsub();
        // Send result back
        if (websocket) {
          websocket.send(JSON.stringify({
            type: 'terminal:exec_result',
            cmd_id: cmdId,
            output: output.slice(-50000), // Cap at 50KB
          }));
        }
      }
    }, 500);

    // Also stop after a reasonable delay (command probably done)
    setTimeout(() => {
      clearInterval(interval);
      unsub();
      if (websocket) {
        websocket.send(JSON.stringify({
          type: 'terminal:exec_result',
          cmd_id: cmdId,
          output: output.slice(-50000),
        }));
      }
    }, 5000); // 5 second default collection window
  },
```

- [ ] **Step 2: Commit**

```bash
git add electron-app/src/renderer/stores/terminalStore.js
git commit -m "feat: add terminal:exec command handling in frontend"
```

---

### Task 13: Wire WebSocket to terminal in App.jsx

**Files:**
- Modify: `electron-app/src/renderer/App.jsx`

- [ ] **Step 1: Add WebSocket terminal message handling**

In `App.jsx`, add a `useEffect` that listens for `terminal:exec` messages from the WebSocket and routes them to the terminal:

```jsx
import { useEffect } from 'react';
import useTerminalStore from './stores/terminalStore';
import useChatStore from './stores/chatStore';

// In the App component, add this effect:
useEffect(() => {
  // Listen for terminal:exec messages from the backend
  const handleWsMessage = (e) => {
    try {
      const msg = JSON.parse(e.data);
      if (msg.type === 'terminal:exec') {
        const store = useTerminalStore.getState();
        store.handleExecCommand(msg.command, msg.cmd_id);
      }
    } catch (err) {
      // Not JSON or not our message
    }
  };

  // Access the WebSocket instance from chatStore
  const ws = useChatStore.getState().websocket;
  if (ws) {
    ws.addEventListener('message', handleWsMessage);
    return () => ws.removeEventListener('message', handleWsMessage);
  }
}, []);
```

- [ ] **Step 2: Commit**

```bash
git add electron-app/src/renderer/App.jsx
git commit -m "feat: wire WebSocket terminal:exec messages to terminal panel"
```

---

### Task 14: Manual smoke test

- [ ] **Step 1: Start the Electron app**

Run in `electron-app/`:
```bash
npm run dev
```

- [ ] **Step 2: Open terminal panel**

- Press Ctrl+` to toggle terminal panel
- Verify terminal appears on the right side
- Verify shell prompt appears (PowerShell on Windows, bash on macOS/Linux)
- Verify you can type and see output

- [ ] **Step 3: Test basic commands**

Type in the terminal:
```
echo "hello from Ares terminal"
ls
pwd
```
Verify each command produces visible output.

- [ ] **Step 4: Test agent-driven commands**

In the chat, ask Ares:
```
Run the command "echo agent-driven" in the terminal
```
Verify Ares uses `terminal_exec` tool and output appears in the terminal panel.

- [ ] **Step 5: Test send-to-chat**

1. In the terminal, run `ls -la` (or `dir` on Windows)
2. Select some output text with mouse
3. Press Ctrl+Enter
4. Verify a `@terminal:N-M` chip appears in the Composer
5. Type a message and send it
6. Verify Ares receives the terminal context

- [ ] **Step 6: Test panel toggle**

1. Press Ctrl+` to close terminal
2. Verify chat takes full width
3. Press Ctrl+` to reopen
4. Verify terminal reconnects with previous session

- [ ] **Step 7: Fix any issues found**

```bash
git add -A
git commit -m "fix: address issues from terminal panel smoke test"
```

---

## Summary

| Task | Component | What It Does |
|------|-----------|-------------|
| 1 | Dependencies | Install node-pty, xterm.js, addons |
| 2 | TerminalManager | PTY lifecycle (spawn, write, resize, kill) |
| 3 | IPC handlers | Bridge main↔renderer for terminal |
| 4 | Preload script | Expose terminal API to renderer |
| 5 | TerminalStore | Zustand state for panel + selection |
| 6 | TerminalHeader | Panel header bar with controls |
| 7 | TerminalPanel | xterm.js terminal component |
| 8 | App layout | Split panel (chat left, terminal right) |
| 9 | Composer | @terminal reference chips |
| 10 | CSS styles | Terminal panel + chip styling |
| 11 | Python backend | terminal_exec tool + WebSocket routing |
| 12 | Frontend exec handler | Handle commands from backend |
| 13 | WebSocket wiring | Connect WS messages to terminal |
| 14 | Smoke test | Manual verification |

**New files:** 5 (terminal-manager.js, terminalStore.js, TerminalPanel.jsx, TerminalHeader.jsx)
**Modified files:** 6 (index.js, preload.js, App.jsx, Composer.jsx, components.css, tools.py, server.py)
**New dependencies:** 4 (node-pty, @xterm/xterm, @xterm/addon-fit, @xterm/addon-web-links)
