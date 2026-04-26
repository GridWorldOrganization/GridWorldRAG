// WinServerRAG Mini Monitor + Setup Wizard — Electron main process.
//
// A single Electron app.asar serves two UIs depending on how it was
// launched:
//   - WinServerRAG Mini.exe    → Mini Monitor (always-on-top dashboard)
//   - WinServerRAG Setup.exe   → Setup Wizard (step-by-step config)
//   - either + --mode=wizard   → Setup Wizard (override)
//
// The Setup.exe is just a renamed copy of Mini.exe (Inno Setup [Files]
// DestName trick) so we don't bundle a second 200MB Electron runtime.
// We detect the launcher name from process.execPath and dispatch.

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
        "WinServerRAG — Fatal error",
        String((err && err.stack) || err)
      );
    }
  } catch {}
  app.exit(1);
});

// v1.3.2: guard module requires. PR #34 fixed the asar packaging miss
// that caused this to throw, but a top-level require() is a footgun:
// any future bundle regression lands the user back in the orphan-process
// state. Catch the error, defer the user-facing dialog until app is
// ready, and exit cleanly so no helpers leak.
let sc = null;
let configCheck = null;
let wizardIpc = null;
let scLoadErr = null;
try {
  sc = require("./service-control.js");
  configCheck = require("./config-check.js");
  wizardIpc = require("./wizard-ipc.js");
} catch (err) {
  scLoadErr = err;
  console.error("[main] failed to load helper module:", err);
}

const API_URL = process.env.WINSERVERRAG_API_URL || "http://127.0.0.1:17600";
const DAEMON_SERVICE = "WinServerRAG-Daemon";
const API_SERVICE    = "WinServerRAG-API";


// ---------------------------------------------------------------------
// Mode dispatch — Mini Monitor vs Setup Wizard
// ---------------------------------------------------------------------
function detectMode() {
  // Explicit override wins.
  if (process.argv.includes("--mode=wizard")) return "wizard";
  if (process.argv.includes("--mode=mini"))   return "mini";
  // Otherwise infer from the launcher name (Inno copies the binary as
  // "WinServerRAG Setup.exe" alongside "WinServerRAG Mini.exe").
  const exe = path.basename(process.execPath || "").toLowerCase();
  if (exe.includes("setup")) return "wizard";
  return "mini";
}

const MODE = detectMode();

// Single-instance lock: prevent two Mini Monitors. Wizard is allowed
// to coexist with Mini (different UI). The lock key includes the mode.
const lockKey = `winserverrag-${MODE}`;
const gotLock = app.requestSingleInstanceLock({ key: lockKey });
if (!gotLock) {
  console.log(`[main] another ${MODE} instance is already running, exiting.`);
  app.quit();
} else {
  app.on("second-instance", () => {
    if (win) {
      if (win.isMinimized()) win.restore();
      win.focus();
    }
  });
}


let win;

function createMiniWindow() {
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
  // v1.3.2: catch renderer-process crashes. Without this, a fatal JS
  // error in renderer.js leaves a blank always-on-top window with the
  // main process still alive — user has no way to recover except Task
  // Manager. uncaughtException (set above) only covers the main
  // process; renderer crashes are a separate event.
  win.webContents.on("render-process-gone", (_e, details) => {
    console.error("[main] renderer gone:", details);
    try {
      dialog.showErrorBox(
        "WinServerRAG Mini — Renderer crashed",
        `Reason: ${details.reason}\nExit code: ${details.exitCode}\n\n` +
        "The mini-monitor will exit. Re-launch from the Start Menu."
      );
    } catch {}
    app.exit(3);
  });
  win.on("unresponsive", () => {
    console.warn("[main] renderer unresponsive");
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

function createWizardWindow() {
  win = new BrowserWindow({
    width: 720,
    height: 560,
    minWidth: 640,
    minHeight: 480,
    resizable: true,
    maximizable: false,
    fullscreenable: false,
    title: "WinServerRAG Setup Wizard",
    backgroundColor: "#0f1115",
    webPreferences: {
      preload: path.join(__dirname, "preload-wizard.js"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  win.webContents.on("render-process-gone", (_e, details) => {
    console.error("[wizard] renderer gone:", details);
    try {
      dialog.showErrorBox(
        "WinServerRAG Setup — Renderer crashed",
        `Reason: ${details.reason}\nExit code: ${details.exitCode}`
      );
    } catch {}
    app.exit(3);
  });
  win.setMenuBarVisibility(false);
  const menu = Menu.buildFromTemplate([{
    label: "Debug", visible: false, submenu: [
      { label: "Toggle DevTools", accelerator: "CmdOrCtrl+Shift+I",
        click: () => { try { win.webContents.toggleDevTools({ mode: "detach" }); } catch {} } },
      { label: "Reload",          accelerator: "CmdOrCtrl+R",
        click: () => { try { win.webContents.reload(); } catch {} } },
    ],
  }]);
  Menu.setApplicationMenu(menu);
  win.loadFile("wizard.html");
}


app.whenReady().then(() => {
  if (scLoadErr) {
    dialog.showErrorBox(
      "WinServerRAG — Bundle error",
      "A required helper module is missing from the app bundle.\n\n" +
        "This is an installer packaging bug. Please reinstall WinServerRAG.\n\n" +
        String(scLoadErr)
    );
    app.exit(2);
    return;
  }
  console.log(`[main] mode=${MODE} execPath=${process.execPath}`);
  if (MODE === "wizard") {
    wizardIpc.register({ ipcMain, app, dialog, sc, configCheck, services: { api: API_SERVICE, daemon: DAEMON_SERVICE } });
    createWizardWindow();
  } else {
    createMiniWindow();
  }
});

app.on("window-all-closed", () => {
  app.quit();
});


// =====================================================================
// Mini Monitor IPC handlers
// =====================================================================
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
  if (!sc) return { kind: "unknown", code: -1, raw: "service-control unavailable" };
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
ipcMain.handle("daemon-start", async () => {
  if (!sc) return { kind: "unknown_error", message: "service-control unavailable" };
  return sc.startService(DAEMON_SERVICE);
});

// v1.3.2: config-check probe. Renderer calls this on the same 5s cadence
// as daemon-status. Returns { kind: "ok" | "missing_file" | "incomplete",
// missingKeys, placeholderKeys, path }. Renderer renders a yellow warning
// row + "Setup Wizard を実行" button when not "ok".
ipcMain.handle("config-check", async () => {
  if (!configCheck) return { kind: "missing_file", path: "(configCheck unavailable)" };
  return configCheck.checkConfig();
});

// v1.3.2: launch the Setup Wizard from Mini Monitor. The wizard exe is
// a renamed copy of this same Electron binary at "{app}\mini\WinServerRAG
// Setup.exe". We compute its path from execPath so the same code works
// regardless of install location.
ipcMain.handle("launch-wizard", async () => {
  try {
    const dir = path.dirname(process.execPath);
    const wizardExe = path.join(dir, "WinServerRAG Setup.exe");
    if (require("fs").existsSync(wizardExe)) {
      shell.openPath(wizardExe);
      return { ok: true, path: wizardExe };
    }
    // Fallback: spawn ourselves with --mode=wizard. Useful in dev where
    // the renamed exe doesn't exist (npm run pack only produces Mini.exe).
    const { spawn } = require("node:child_process");
    spawn(process.execPath, ["--mode=wizard"], { detached: true, stdio: "ignore" }).unref();
    return { ok: true, path: process.execPath, fallback: "self+arg" };
  } catch (err) {
    return { ok: false, error: String(err) };
  }
});
