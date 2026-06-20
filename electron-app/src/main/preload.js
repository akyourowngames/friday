const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("aresDesktop", {
  getServerUrl: () => ipcRenderer.invoke("ares:get-server-url"),
  restartServer: () => ipcRenderer.invoke("ares:restart-server"),
  getAppVersion: () => ipcRenderer.invoke("ares:get-app-version"),
  platform: process.platform,
  logToFile: (msg) => ipcRenderer.send("ares:log-to-file", msg),

  // ── Terminal API ──────────────────────────────────────────
  terminal: {
    /** Create a new PTY session. Returns { sessionId }. */
    create: () => ipcRenderer.invoke("ares:terminal:create"),

    /** Write data (string) to the PTY. */
    write: (data) => ipcRenderer.send("ares:terminal:write", data),

    /** Resize the PTY to new dimensions. */
    resize: (cols, rows) => ipcRenderer.send("ares:terminal:resize", { cols, rows }),

    /** Kill the active PTY process. */
    kill: () => ipcRenderer.send("ares:terminal:kill"),

    /** Check if a PTY is active. Returns boolean. */
    isActive: () => ipcRenderer.invoke("ares:terminal:isActive"),

    /** Subscribe to PTY output data (string chunks). */
    onData: (callback) => {
      const handler = (_, data) => callback(data);
      ipcRenderer.on("terminal:data", handler);
      return () => ipcRenderer.removeListener("terminal:data", handler);
    },

    /** Subscribe to PTY exit event. */
    onExit: (callback) => {
      const handler = (_, info) => callback(info);
      ipcRenderer.on("terminal:exit", handler);
      return () => ipcRenderer.removeListener("terminal:exit", handler);
    },

    /** Subscribe to PTY creation event. */
    onCreate: (callback) => {
      const handler = (_, info) => callback(info);
      ipcRenderer.on("terminal:create", handler);
      return () => ipcRenderer.removeListener("terminal:create", handler);
    },

    /** Remove all terminal IPC listeners. */
    removeAllListeners: () => {
      ipcRenderer.removeAllListeners("terminal:data");
      ipcRenderer.removeAllListeners("terminal:exit");
      ipcRenderer.removeAllListeners("terminal:create");
    },
  },
});
