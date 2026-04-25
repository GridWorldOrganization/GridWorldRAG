// WinServerRAG Mini Monitor — Electron main process.
// A tiny frameless-ish always-on-top window that shows health metrics
// and has one button to open the full web UI in the default browser.

const { app, BrowserWindow, shell, ipcMain, Menu } = require("electron");
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
  // Hidden accelerators: menu is not visible but accelerators still fire.
  // Lets the user pop DevTools / reload / force-refresh when diagnosing the UI.
  const menu = Menu.buildFromTemplate([{
    label: "Debug", visible: false, submenu: [
      { label: "Toggle DevTools", accelerator: "CmdOrCtrl+Shift+I",
        click: () => { try { win.webContents.toggleDevTools({ mode: "detach" }); } catch {} } },
      { label: "Reload",          accelerator: "CmdOrCtrl+R",
        click: () => { try { win.webContents.reload(); } catch {} } },
      { label: "Force Reload",    accelerator: "CmdOrCtrl+Shift+R",
        click: () => { try { win.webContents.reloadIgnoringCache(); } catch {} } },
    ],
  }]);
  Menu.setApplicationMenu(menu);
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
// Cache-buster so Chrome doesn't serve a stale index.html/app.js from a
// previous version of WinServerRAG.
ipcMain.on("open-web-ui", () => {
  const cb = Date.now().toString(36);
  shell.openExternal(`${API_URL}/?v=${cb}`);
});

// IPC: give the renderer the API URL so it can fetch /api/stats, /api/workers.
ipcMain.handle("get-api-url", () => API_URL);

// IPC: toggle always-on-top for the mini window.
ipcMain.handle("get-always-on-top", () => {
  return win ? win.isAlwaysOnTop() : false;
});
ipcMain.on("set-always-on-top", (_evt, on) => {
  if (win) win.setAlwaysOnTop(!!on);
});
