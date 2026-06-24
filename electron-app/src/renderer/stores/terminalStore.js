import { create } from 'zustand';

const useTerminalStore = create((set, get) => ({
  // ── State ──────────────────────────────────────────────────
  isOpen: false,
  isConnected: false,
  sessionId: null,
  lastSelection: null,
  commandHistory: [],

  // ── Actions ────────────────────────────────────────────────

  togglePanel: () => {
    const { isOpen } = get();
    if (isOpen) {
      get().closePanel();
    } else {
      get().openPanel();
    }
  },

  openPanel: async () => {
    set({ isOpen: true });
    if (!get().isConnected) {
      await get().createTerminal();
    }
  },

  closePanel: () => {
    set({ isOpen: false });
  },

  createTerminal: async () => {
    const aresDesktop = window.aresDesktop;
    if (!aresDesktop?.terminal) {
      console.error('Terminal API not available');
      return;
    }

    const result = await aresDesktop.terminal.create();
    set({ isConnected: true, sessionId: result.sessionId });

    aresDesktop.terminal.onExit(({ exitCode }) => {
      console.log(`Terminal exited with code ${exitCode}`);
      set({ isConnected: false, sessionId: null });
    });
  },

  writeToTerminal: (data) => {
    const aresDesktop = window.aresDesktop;
    aresDesktop?.logToFile(`Store: writeToTerminal: isConnected=${get().isConnected}, data=${JSON.stringify(data)}`);
    if (aresDesktop?.terminal && get().isConnected) {
      aresDesktop.terminal.write(data);
    }
  },

  resizeTerminal: (cols, rows) => {
    const aresDesktop = window.aresDesktop;
    if (aresDesktop?.terminal && get().isConnected) {
      aresDesktop.terminal.resize(cols, rows);
    }
  },

  setSelection: (text, startLine, endLine) => {
    set({ lastSelection: { text, startLine, endLine } });
  },

  clearSelection: () => {
    set({ lastSelection: null });
  },

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

  addCommandToHistory: (cmd) => {
    set((state) => ({
      commandHistory: [...state.commandHistory.slice(-99), cmd],
    }));
  },

  handleExecCommand: (command, cmdId, websocket) => {
    const store = get();
    if (!store.isConnected) {
      store.openPanel().then(() => {
        setTimeout(() => {
          store.writeToTerminal(command + '\r');
          store._collectOutput(command, cmdId, websocket);
        }, 500);
      });
      return;
    }

    store.writeToTerminal(command + '\r');
    store._collectOutput(command, cmdId, websocket);
  },

  _collectOutput: (command, cmdId, websocket) => {
    let output = '';
    const startTime = Date.now();
    const maxWait = 30000;
    let sent = false;
    let completionCheckCount = 0;

    const aresDesktop = window.aresDesktop;
    if (!aresDesktop?.terminal) return;

    const unsub = aresDesktop.terminal.onData((data) => {
      output += data;
    });

    const interval = setInterval(() => {
      const elapsed = Date.now() - startTime;
      if (sent) {
        clearInterval(interval);
        unsub();
        return;
      }

      if (elapsed >= maxWait) {
        sent = true;
        clearInterval(interval);
        unsub();
        if (websocket) {
          websocket.send(JSON.stringify({
            type: 'terminal:exec_result',
            cmd_id: cmdId,
            output: output.slice(-50000),
          }));
        }
        return;
      }

      if (output.length > 0) {
        completionCheckCount++;
        const trimmed = output.trimEnd();
        const lastLine = trimmed.split('\n').pop() || '';
        const isPrompt = lastLine.endsWith('>') ||
          lastLine.endsWith('$') ||
          lastLine.endsWith('#') ||
          lastLine.endsWith('%') ||
          lastLine.includes(':\\>') ||
          lastLine.includes(':\\$');
        if (isPrompt && completionCheckCount >= 5) {
          sent = true;
          clearInterval(interval);
          unsub();
          if (websocket) {
            websocket.send(JSON.stringify({
              type: 'terminal:exec_result',
              cmd_id: cmdId,
              output: output.slice(-50000),
            }));
          }
        }
      }
    }, 100);
  },

  killTerminal: () => {
    const aresDesktop = window.aresDesktop;
    if (aresDesktop?.terminal) {
      aresDesktop.terminal.kill();
    }
    set({ isConnected: false, sessionId: null });
  },
}));

export default useTerminalStore;
