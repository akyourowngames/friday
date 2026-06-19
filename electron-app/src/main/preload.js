const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("aresDesktop", {
  getServerUrl: () => ipcRenderer.invoke("ares:get-server-url"),
  restartServer: () => ipcRenderer.invoke("ares:restart-server"),
  getAppVersion: () => ipcRenderer.invoke("ares:get-app-version"),
  platform: process.platform
});
