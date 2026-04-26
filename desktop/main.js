// WinServerRAG Mini Monitor — Electron main process.
// A tiny frameless-ish always-on-top window that shows health metrics
// and has one button to open the full web UI in the default browser.

const { app, BrowserWindow, shell, ipcMain, Menu, dialog } = require("electron");
const path = require("path");

// v1.3.2: catch unhandled exceptions in the main process. Without this,
// a failure during init (e.g. require() of a missing module) shows the
// default Electron error dialog and leaves the GPU helper / utility
// child processes orphaned in the background after the user closes it.
// Forcing app.exit() from the handler kills the whole process tree.
process.on("uncaughtException", (err) => {
  console.error("[main] uncaughtException:", err);
  try {
    if (app.isReady()) {
      dialog.showErrorBox(
        "WinServerRAG Mini — Fatal error",
        String((err && err.stack) || err)
      );
    }
  } catch {}
  app.exit(1);
});

// v1.3.2: guard the service-control.js require. PR #34 fixed the asar
// packaging miss that caused this to throw, but a top-level require()
// is a footgun: any future bundle regression lands the user back in
// the orphan-process state. Catch the error, defer the user-facing
// dialog until app is ready, and exit cleanly so no helpers leak.
let sc = null;
let scLoadErr = null;
try {
  sc = require("./service-control.js");
} catch (err) {
  scLoadErr = err;
  console.error("[main] failed to load service-control.js:", err);
}

const API_URL = process.env.WINSERVERRAG_API_URL || "http://127.0.0.1:17600";

// Service names — hardcoded on purpose. The installer registers exactly
// these (see installer/install-services.ps1). v1.3 doesn't need a
// manifest file because `sc query <name>` and `sc start <name>` only
// take a name, not a path. Codex review flagged path-resolution as
// overbuilt; we deferred it.
const DAEMON_SERVICE = "WinServerRAG-Daemon";
const API_SERVICE    = "WinServerRAG-API";

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

app.whenReady().then(() => {
  if (scLoadErr) {
    dialog.showErrorBox(
      "WinServerRAG Mini — Bundle error",
      "service-control.js is missing from the app bundle.\n\n" +
        "This is an installer packaging bug. Please reinstall WinServerRAG.\n\n" +
        String(scLoadErr)
    );
    app.exit(2);
    return;
  }
  createWindow();
});

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

// IPC: daemon status — the renderer polls this every 5s. Returns the
// numeric-parsed SCM state for WinServerRAG-Daemon. Independent of the
// FastAPI lifecycle (the whole point of v1.3).
//
// Shape: { kind: "running" | "stopped" | ... | "not_installed" | "unknown",
//          code: <numeric>, devMode: { isDev: bool, ...} }
ipcMain.handle("daemon-status", async () => {
  const status = await sc.queryService(DAEMON_SERVICE);
  // Augment with dev-mode detection so the renderer can decide between
  // "✕ 未インストール" (production deploy missing the installer) vs
  // "💻 dev mode (venv で起動してください)" (developer running from repo).
  const dev = sc.detectDevMode(__dirname);
  return { ...status, devMode: dev };
});

// IPC: start the daemon. Triggered by the renderer's ▶ button when the
// service is in STOPPED state. Works without UAC because the installer
// (installer/install-services.ps1) granted SERVICE_START to the
// WinServerRAG Operators local group at install time.
//
// Returns the classified exit:
//   { kind: "started" | "already_running" | "access_denied" | "not_installed"
//          | "dependency_failed" | "no_response" | "unknown_error",
//     message: <user-facing string>, code: <numeric> }
//
// The renderer should poll daemon-status afterwards to observe the
// START_PENDING → RUNNING transition (no need to block here).
ipcMain.handle("daemon-start", async () => {
  return sc.startService(DAEMON_SERVICE);
});
