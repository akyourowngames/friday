const { app, BrowserWindow, ipcMain, nativeTheme, shell } = require("electron");
const path = require("path");
const { PythonManager } = require("./python-manager");

const isDev = !app.isPackaged;
const pythonManager = new PythonManager();
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
    backgroundColor: "#0b0b0c",
    titleBarStyle: "hidden",
    titleBarOverlay: {
      color: "#0b0b0c",
      symbolColor: "#fafafa",
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

  mainWindow.once("ready-to-show", () => {
    mainWindow.show();
  });

  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: "deny" };
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
