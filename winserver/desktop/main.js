// WinServerRAG Mini Monitor — Electron main process.
// A tiny frameless-ish always-on-top window that shows health metrics
// and has one button to open the full web UI in the default browser.

const { app, BrowserWindow, shell, ipcMain } = require("electron");
const path = require("path");

const API_URL = process.env.WINSERVERRAG_API_URL || "http://127.0.0.1:17600";

let win;

function createWindow() {
  win = new BrowserWindow({
    width: 360,
    height: 400,
    minWidth: 320,
    minHeight: 320,
    maxWidth: 560,
    maxHeight: 700,
    resizable: true,
    maximizable: false,
    fullscreenable: false,
    alwaysOnTop: true,
    frame: true,
    title: "WinServerRAG Mini",
    backgroundColor: "#0f1115",
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  win.setMenuBarVisibility(false);
  // Belt & suspenders: intercept maximize/fullscreen events in case the OS
  // window-controls are used to bypass the flags above.
  win.on("maximize", () => { try { win.unmaximize(); } catch {} });
  win.on("enter-full-screen", () => { try { win.setFullScreen(false); } catch {} });
  win.loadFile("index.html");
}

app.whenReady().then(createWindow);

app.on("window-all-closed", () => {
  app.quit();
});

// IPC: open full web UI in default browser.
ipcMain.on("open-web-ui", () => {
  shell.openExternal(API_URL + "/");
});

// IPC: give the renderer the API URL so it can fetch /api/stats, /api/workers.
ipcMain.handle("get-api-url", () => API_URL);
