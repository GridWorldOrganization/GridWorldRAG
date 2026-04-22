// Exposes a narrow IPC surface to the renderer.
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("winsrv", {
  openWebUi: () => ipcRenderer.send("open-web-ui"),
  getApiUrl: () => ipcRenderer.invoke("get-api-url"),
});
