// Exposes a narrow IPC surface to the renderer.
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("winsrv", {
  openWebUi: () => ipcRenderer.send("open-web-ui"),
  getApiUrl: () => ipcRenderer.invoke("get-api-url"),
  getAlwaysOnTop: () => ipcRenderer.invoke("get-always-on-top"),
  setAlwaysOnTop: (on) => ipcRenderer.send("set-always-on-top", !!on),
  // v1.3 daemon control — independent of the FastAPI lifecycle.
  // daemonStatus() polls SCM directly so the daemon row updates even
  // when the API is down (precisely when the operator wants honesty).
  daemonStatus: () => ipcRenderer.invoke("daemon-status"),
  daemonStart:  () => ipcRenderer.invoke("daemon-start"),
});
