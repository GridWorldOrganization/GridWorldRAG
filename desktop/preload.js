// Exposes a narrow IPC surface to the renderer.
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("winsrv", {
  openWebUi: () => ipcRenderer.send("open-web-ui"),
  getApiUrl: () => ipcRenderer.invoke("get-api-url"),
  getAlwaysOnTop: () => ipcRenderer.invoke("get-always-on-top"),
  setAlwaysOnTop: (on) => ipcRenderer.send("set-always-on-top", !!on),
});
