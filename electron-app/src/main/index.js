const { app, BrowserWindow, ipcMain, nativeTheme, shell } = require("electron");
const path = require("path");
const fs = require("fs");
const { PythonManager } = require("./python-manager");
const TerminalManager = require("./terminal-manager");

const isDev = !app.isPackaged;
const pythonManager = new PythonManager();
const terminalManager = new TerminalManager();
let mainWindow = null;

const debugLogPath = "c:\\Users\\anime\\ares\\terminal-debug.log";
// Clean log file on start
try {
  fs.writeFileSync(debugLogPath, `[${new Date().toISOString()}] Logger initialized\n`);
} catch (e) {
  // ignore
}

function logToFile(msg) {
  try {
    fs.appendFileSync(debugLogPath, `[${new Date().toISOString()}] ${msg}\n`);
  } catch (e) {
    // ignore
  }
}

function rendererEntry() {
  if (isDev) {
    return "http://127.0.0.1:5173";
  }
  return `file://${path.join(__dirname, "..", "..", "dist", "index.html")}`;
}

function createWindow() {
  nativeTheme.themeSource = "dark";

  mainWindow = new BrowserWindow({
    width: 1365,
    height: 768,
    minWidth: 980,
    minHeight: 620,
    backgroundColor: "#09090b",
    titleBarStyle: "hidden",
    titleBarOverlay: {
      color: "#09090b",
      symbolColor: "#e4e4e7",
      height: 36,
    },
    show: false,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false
    }
  });

  terminalManager.setWindow(mainWindow);

  mainWindow.once("ready-to-show", () => {
    mainWindow.show();
  });

  // Always allow Ctrl+Shift+I to open DevTools for debugging
  mainWindow.webContents.on("before-input-event", (event, input) => {
    if (input.control && input.shift && input.key.toLowerCase() === "i") {
      mainWindow.webContents.toggleDevTools();
      event.preventDefault();
    }
  });

  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: "deny" };
  });

  mainWindow.on("closed", () => {
    terminalManager.kill();
    terminalManager.setWindow(null);
    mainWindow = null;
  });

  return mainWindow.loadURL(rendererEntry());
}

ipcMain.handle("ares:get-server-url", async () => {
  return pythonManager.ready ? pythonManager.url : pythonManager.start();
});

ipcMain.handle("ares:restart-server", async () => {
  return pythonManager.restart();
});

ipcMain.handle("ares:get-app-version", () => app.getVersion());

// ── Terminal IPC handlers ──────────────────────────────────────

ipcMain.on("ares:log-to-file", (_, msg) => {
  logToFile(msg);
});

ipcMain.on("focus-fix", () => {
  if (mainWindow && !mainWindow.isDestroyed()) {
    logToFile("Main: focus-fix triggered, calling mainWindow.focus()");
    mainWindow.focus();
  }
});

ipcMain.handle("ares:terminal:create", () => {
  logToFile("Main: ares:terminal:create called");
  return terminalManager.create();
});

ipcMain.on("ares:terminal:write", (_, data) => {
  logToFile(`Main: ares:terminal:write received: ${JSON.stringify(data)}`);
  terminalManager.write(data);
});

ipcMain.on("ares:terminal:resize", (_, { cols, rows }) => {
  logToFile(`Main: ares:terminal:resize: cols=${cols}, rows=${rows}`);
  terminalManager.resize(cols, rows);
});

ipcMain.on("ares:terminal:kill", () => {
  logToFile("Main: ares:terminal:kill called");
  terminalManager.kill();
});

ipcMain.handle("ares:terminal:isActive", () => {
  return terminalManager.isActive();
});

app.whenReady().then(async () => {
  try {
    await pythonManager.start();
  } catch (error) {
    // Keep the UI reachable: the renderer can ask the manager to retry and
    // shows its existing reconnect state instead of Electron crashing here.
    console.error("Ares backend startup failed:", error);
  }
  await createWindow();

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      createWindow();
    }
  });
}).catch((error) => {
  console.error("Electron startup failed:", error);
  app.quit();
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") {
    app.quit();
  }
});

app.on("before-quit", async (event) => {
  if (pythonManager.process) {
    event.preventDefault();
    await pythonManager.stop();
    app.exit(0);
  }
});
