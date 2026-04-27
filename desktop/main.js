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

// v1.3.2 fix B1: split userData by mode so Mini and Wizard don't share
// the single-instance lock.
//
// Why: requestSingleInstanceLock(additionalData?) — the argument is NOT
// a lock key, it's data passed to the second-instance event handler.
// The actual lock is per-app, derived from app.name × userData dir.
// Mini.exe and Setup.exe (a renamed copy of Mini.exe) share both, so
// they share the lock. Result: clicking the Mini Monitor's "Setup
// Wizard を起動" CTA spawns Setup.exe, which fails the lock check
// while Mini holds it, and silently quits. Wizard never opens.
//
// Fix: change userData per mode BEFORE requestSingleInstanceLock. Each
// mode now has its own SingletonLock file, so they coexist.
if (MODE === "wizard") {
  const wizardData = path.join(app.getPath("appData"), "WinServerRAG-Wizard");
  app.setPath("userData", wizardData);
}

const gotLock = app.requestSingleInstanceLock();
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
        "The mini-monitor will exit. Re-launch from the Start Menu shortcut\n" +
        '"WinServerRAG Mini Monitor".'
      );
    } catch {}
    app.exit(3);
  });
  win.on("unresponsive", () => {
    console.warn("[main] renderer unresponsive");
  });
  // v1.3.3: replace the default Chromium context menu with a minimal
  // clipboard-only menu. The default has Reload / Save / Print /
  // Inspect / etc. which look unprofessional. But we DO want the
  // operator to be able to Cut/Copy/Paste in input fields — PR #51
  // suppressed the menu entirely which broke that. v1.3.4 puts Cut/
  // Copy/Paste/Select All back, omits the rest.
  win.webContents.on("context-menu", (event, params) => {
    const m = Menu.buildFromTemplate([
      { role: "cut",       enabled: params.editFlags.canCut },
      { role: "copy",      enabled: params.editFlags.canCopy },
      { role: "paste",     enabled: params.editFlags.canPaste },
      { type: "separator" },
      { role: "selectAll", enabled: params.editFlags.canSelectAll },
    ]);
    m.popup();
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
  // v1.3.3: minimal clipboard-only context menu. See Mini window
  // for rationale — Wizard renders form inputs so Cut/Copy/Paste must
  // be available. v1.3.4 puts them back, omits Reload/Save/Inspect/etc.
  win.webContents.on("context-menu", (event, params) => {
    const m = Menu.buildFromTemplate([
      { role: "cut",       enabled: params.editFlags.canCut },
      { role: "copy",      enabled: params.editFlags.canCopy },
      { role: "paste",     enabled: params.editFlags.canPaste },
      { type: "separator" },
      { role: "selectAll", enabled: params.editFlags.canSelectAll },
    ]);
    m.popup();
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
  // v1.3.4: register the wizard IPC handlers regardless of mode. Mini's
  // launch-wizard CTA used to spawn Setup.exe as a separate process; v1.3.4
  // opens the wizard as a CHILD MODAL of the Mini window instead. That
  // requires Mini's main process to handle the wizard:* IPC channels too.
  // wizard-ipc.js is idempotent on re-call so this is safe.
  wizardIpc.register({ ipcMain, app, dialog, sc, configCheck, services: { api: API_SERVICE, daemon: DAEMON_SERVICE } });
  if (MODE === "wizard") {
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

// v1.3.4: launch the Setup Wizard as a CHILD MODAL of the Mini Monitor
// window. Previously this spawned Setup.exe as a separate process. Modal
// behavior matches operator's mental model — Mini Monitor is the home
// surface, the Wizard is a transient configuration dialog that should
// disable Mini until completed/cancelled.
//
// Setup.exe (a renamed copy of this binary) still works as a standalone
// Start Menu launcher for the case where Mini isn't running yet (first-
// time setup).
let _wizardChild = null;
ipcMain.handle("launch-wizard", async () => {
  if (_wizardChild && !_wizardChild.isDestroyed()) {
    _wizardChild.focus();
    return { ok: true, focused: true };
  }
  try {
    _wizardChild = new BrowserWindow({
      width: 720,
      height: 560,
      minWidth: 640,
      minHeight: 480,
      resizable: true,
      maximizable: false,
      fullscreenable: false,
      title: "WinServerRAG Setup Wizard",
      backgroundColor: "#0f1115",
      parent: win,
      modal: true,
      show: false,
      webPreferences: {
        preload: path.join(__dirname, "preload-wizard.js"),
        contextIsolation: true,
        nodeIntegration: false,
      },
    });
    _wizardChild.setMenuBarVisibility(false);
    // Same clipboard-only context menu as the parent (PR #52).
    _wizardChild.webContents.on("context-menu", (event, params) => {
      const m = Menu.buildFromTemplate([
        { role: "cut",       enabled: params.editFlags.canCut },
        { role: "copy",      enabled: params.editFlags.canCopy },
        { role: "paste",     enabled: params.editFlags.canPaste },
        { type: "separator" },
        { role: "selectAll", enabled: params.editFlags.canSelectAll },
      ]);
      m.popup();
    });
    _wizardChild.webContents.on("render-process-gone", (_e, details) => {
      console.error("[wizard child] renderer gone:", details);
      try {
        dialog.showErrorBox(
          "WinServerRAG Setup — Renderer crashed",
          `Reason: ${details.reason}\nExit code: ${details.exitCode}`
        );
      } catch {}
      try { _wizardChild.destroy(); } catch {}
      _wizardChild = null;
    });
    _wizardChild.on("closed", () => { _wizardChild = null; });
    _wizardChild.once("ready-to-show", () => _wizardChild.show());
    await _wizardChild.loadFile("wizard.html");
    return { ok: true, modal: true };
  } catch (err) {
    return { ok: false, error: String(err) };
  }
});
