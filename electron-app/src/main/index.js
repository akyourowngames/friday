const { app, BrowserWindow, ipcMain, nativeTheme, shell } = require("electron");
const path = require("path");
const { PythonManager } = require("./python-manager");
const TerminalManager = require("./terminal-manager");

const isDev = !app.isPackaged;
const pythonManager = new PythonManager();
const terminalManager = new TerminalManager();
let mainWindow = null;

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

ipcMain.handle("ares:terminal:create", () => {
  return terminalManager.create();
});

ipcMain.on("ares:terminal:write", (_, data) => {
  terminalManager.write(data);
});

ipcMain.on("ares:terminal:resize", (_, { cols, rows }) => {
  terminalManager.resize(cols, rows);
});

ipcMain.on("ares:terminal:kill", () => {
  terminalManager.kill();
});

ipcMain.handle("ares:terminal:isActive", () => {
  return terminalManager.isActive();
});

app.whenReady().then(async () => {
  await pythonManager.start();
  await createWindow();

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      createWindow();
    }
  });
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
