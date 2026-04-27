// wizard-ipc.js — IPC handlers for the Setup Wizard.
//
// Registered from main.js via wizardIpc.register({ ipcMain, app, dialog,
// sc, configCheck, services }). Each handler is a thin wrapper around
// child processes (winserverrag-dbinit.exe, service-control.js) plus
// config-check.js for read/write.
//
// Why this lives in its own module: main.js was already 200+ lines for
// the Mini Monitor; the wizard adds another 200+ for handlers. Splitting
// keeps each file focused and lets each be unit-tested independently.
"use strict";

const path = require("node:path");
const fs = require("node:fs");
const { spawn, execFile } = require("node:child_process");

let _ctx = null; // captured ipcMain, dialog, etc.

// ---------------------------------------------------------------------
// Locate the bundled service exes. The wizard runs from
// {app}\mini\WinServerRAG Setup.exe; the api/daemon/dbinit/backup
// onedir bundles are at {app}\bin\winserverrag-{name}\<name>.exe.
// app root = wizard exec path → up 1 dir (mini\) → up 1 dir (= {app}).
// ---------------------------------------------------------------------
function appRoot() {
  if (process.env.WINSERVERRAG_APP_ROOT) {
    return process.env.WINSERVERRAG_APP_ROOT;
  }
  const exec = process.execPath; // ...\mini\WinServerRAG Setup.exe
  return path.dirname(path.dirname(exec));
}

function bundledExe(name) {
  return path.join(appRoot(), "bin", `winserverrag-${name}`, `winserverrag-${name}.exe`);
}


// ---------------------------------------------------------------------
// Step 2: prerequisites — PG service running? pgvector available?
// ---------------------------------------------------------------------
function probeService(name) {
  return new Promise((resolve) => {
    const exe = path.join(process.env.SystemRoot || "C:\\Windows", "System32", "sc.exe");
    execFile(exe, ["query", name], { timeout: 3000, windowsHide: true },
      (err, stdout) => {
        if (err && err.code === 1060) return resolve({ exists: false, running: false });
        if (err && typeof err.code === "number") {
          // sc returns 1060 for non-existent. Some environments surface it
          // as err.code; others put it in stdout. Treat anything else as "unknown".
          if (err.code === 1060) return resolve({ exists: false, running: false });
        }
        const m = String(stdout || "").match(/STATE\s*:\s*(\d+)/);
        if (!m) return resolve({ exists: false, running: false });
        const code = parseInt(m[1], 10);
        resolve({ exists: true, running: code === 4, stateCode: code });
      });
  });
}

async function prereqCheck() {
  const pg17 = await probeService("postgresql-x64-17");
  const pg16 = pg17.exists ? null : await probeService("postgresql-x64-16");
  const pg15 = (pg17.exists || (pg16 && pg16.exists)) ? null : await probeService("postgresql-x64-15");
  const winsrvApi = await probeService(_ctx.services.api);
  const winsrvDaemon = await probeService(_ctx.services.daemon);
  // Pick the first PG that exists; if none, surface "missing".
  const pg = pg17.exists ? { ...pg17, version: 17 }
           : (pg16 && pg16.exists) ? { ...pg16, version: 16 }
           : (pg15 && pg15.exists) ? { ...pg15, version: 15 }
           : { exists: false, version: null };
  return {
    postgres: pg,
    winserverrag: {
      apiRegistered:    winsrvApi.exists,
      daemonRegistered: winsrvDaemon.exists,
    },
  };
}


// ---------------------------------------------------------------------
// Step 3: PG connection test. We invoke the daemon's bundled python
// runtime via winserverrag-dbinit.exe with --check-only to exercise the
// real config.py code path. That guarantees the test matches what the
// service will actually try at startup.
//
// For now, simpler approach: spawn psql via choco/pg's bin folder.
// Falls back to "no test, just save" if psql isn't on PATH.
// ---------------------------------------------------------------------
// v1.3.6: TCP-only port reachability test (no psql subprocess).
//
// History of failed approaches:
//   PR #47: hardcode PGDATABASE='postgres'  → fixed "DB doesn't exist"
//   PR #53: surface stderr / err.code        → diagnostic only, no fix
//   PR #55: stdin.end() + 15s timeout        → still hangs at 15s SIGTERM
//
// Root cause we couldn't shake: psql.exe launched from Electron's main
// process consistently hangs until SIGTERM. Same binary, same env, same
// command works in <1s from a normal console node REPL. The exact
// mechanism (Job Object inheritance? AppContainer? GUI-vs-console
// stdio?) was never pinned down.
//
// New approach: skip psql entirely. Use Node's net.connect to verify the
// PG port is reachable. That answers the "is PG listening at this
// host:port?" question, which is the most common operator mistake at
// this step (wrong port, PG not running, firewall blocking). Auth + DB
// existence are deferred to Step 6 (db_init), which spawns
// winserverrag-dbinit.exe — that goes through psycopg + libpq, not psql,
// so it works fine.
//
// What we still don't catch here:
//   - Wrong password         → caught by db_init step
//   - Missing target DB      → db_init creates it (the form hint already
//                              tells the operator this)
//   - Wrong PG user          → caught by db_init
function testPgConnection(params) {
  const net = require("node:net");
  const { host, port } = params || {};
  let h = host || "localhost";
  // Force IPv4 loopback to avoid any IPv6/IPv4 mismatch with PG's
  // listen_addresses on Windows.
  if (h === "localhost") h = "127.0.0.1";
  const p = parseInt(port || "5432", 10);
  return new Promise((resolve) => {
    const t0 = Date.now();
    const socket = new net.Socket();
    let settled = false;
    const finish = (result) => {
      if (settled) return;
      settled = true;
      try { socket.destroy(); } catch {}
      resolve(result);
    };
    socket.setTimeout(5000);
    socket.once("connect", () => {
      const elapsed = Date.now() - t0;
      finish({
        ok: true,
        stdout: `TCP接続成功 (${h}:${p}, ${elapsed}ms)`,
        note: "ポート到達のみ確認。認証・DB 存在チェックは Step 6 (db_init) で行います。",
      });
    });
    socket.once("error", (err) => {
      finish({
        ok: false,
        error: `TCP接続失敗 (${h}:${p}): ${err.code || err.message || err}`,
      });
    });
    socket.once("timeout", () => {
      finish({
        ok: false,
        error: `TCP接続タイムアウト (${h}:${p}, 5s)。PostgreSQL サービスが起動しているか確認してください。`,
      });
    });
    try { socket.connect(p, h); }
    catch (e) { finish({ ok: false, error: `connect error: ${e.message || e}` }); }
  });
}


// ---------------------------------------------------------------------
// Step 4: Google OAuth — file picker for credentials.json + copy to
// CONFIG_DIR. The actual OAuth flow runs separately when the user starts
// the API service for the first time (winserverrag-api spawns the
// browser auth flow on demand). The wizard just stages credentials.json.
// ---------------------------------------------------------------------
async function pickCredentials() {
  const r = await _ctx.dialog.showOpenDialog({
    title: "credentials.json を選択",
    filters: [{ name: "OAuth credentials JSON", extensions: ["json"] }],
    properties: ["openFile"],
  });
  if (r.canceled || !r.filePaths.length) return { canceled: true };
  return { canceled: false, path: r.filePaths[0] };
}

// v1.3.2 fix B6: sanity-check the source path before copying. The
// renderer SHOULD only ever pass a path returned by pickCredentials
// (which is the Electron file dialog), but contextIsolation is
// defense-in-depth, not a guarantee — a future preload regression or
// devtools console call could pass any path. Reject anything that
// isn't a JSON file under a reasonable size.
const MAX_CREDENTIALS_BYTES = 32 * 1024; // 32KB; real OAuth client.json is ~500B

function copyCredentials(srcPath) {
  return new Promise((resolve) => {
    try {
      if (typeof srcPath !== "string" || srcPath.length === 0) {
        return resolve({ ok: false, error: "invalid path" });
      }
      const ext = path.extname(srcPath).toLowerCase();
      if (ext !== ".json") {
        return resolve({ ok: false, error: `expected a .json file, got ${ext || "(no ext)"}` });
      }
      const stat = fs.statSync(srcPath);
      if (!stat.isFile()) {
        return resolve({ ok: false, error: "source is not a regular file" });
      }
      if (stat.size > MAX_CREDENTIALS_BYTES) {
        return resolve({ ok: false, error: `file too large (${stat.size} bytes, max ${MAX_CREDENTIALS_BYTES})` });
      }
      // Light schema check: real Google OAuth credentials.json has
      // an "installed" or "web" top-level key with "client_id" inside.
      const text = fs.readFileSync(srcPath, "utf-8");
      let obj;
      try { obj = JSON.parse(text); } catch {
        return resolve({ ok: false, error: "file is not valid JSON" });
      }
      const root = obj && (obj.installed || obj.web);
      if (!root || typeof root.client_id !== "string") {
        return resolve({ ok: false, error: "JSON does not look like a Google OAuth credentials file (missing installed/web → client_id)" });
      }
      const dst = path.join(_ctx.configCheck.configDir(), "credentials.json");
      fs.mkdirSync(path.dirname(dst), { recursive: true });
      fs.copyFileSync(srcPath, dst);
      // v1.3.3: also surface the parsed client_id / client_secret so the
      // renderer can auto-fill the OAuth form fields. Saves the operator
      // from copy-pasting the same values from credentials.json into the
      // form (and prevents copy-paste mismatches that bite later when
      // the daemon tries to do OAuth).
      resolve({
        ok: true,
        dest: dst,
        clientId: root.client_id,
        clientSecret: typeof root.client_secret === "string" ? root.client_secret : "",
      });
    } catch (err) {
      resolve({ ok: false, error: String(err) });
    }
  });
}


// ---------------------------------------------------------------------
// Step 6: write config.v2.env atomically
// ---------------------------------------------------------------------
function writeConfig(values) {
  try {
    const r = _ctx.configCheck.writeConfig(values);
    return Promise.resolve({ ok: true, ...r });
  } catch (err) {
    return Promise.resolve({ ok: false, error: String(err) });
  }
}


// ---------------------------------------------------------------------
// Step 7: run winserverrag-dbinit.exe, stream stdout/stderr to the
// renderer line-by-line. Resolve when the child exits.
// ---------------------------------------------------------------------
function runDbInit(sender) {
  return new Promise((resolve) => {
    const exe = bundledExe("dbinit");
    if (!fs.existsSync(exe)) {
      resolve({ ok: false, error: `dbinit exe not found at ${exe}` });
      return;
    }
    const child = spawn(exe, [], { windowsHide: true });
    let buf = "";
    const onChunk = (chunk) => {
      buf += chunk.toString("utf-8");
      let nl;
      while ((nl = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, nl).replace(/\r$/, "");
        buf = buf.slice(nl + 1);
        try { sender.send("wizard:db-init-log", line); } catch {}
      }
    };
    child.stdout.on("data", onChunk);
    child.stderr.on("data", onChunk);
    child.on("close", (code) => {
      if (buf) { try { sender.send("wizard:db-init-log", buf); } catch {} }
      resolve({ ok: code === 0, exitCode: code });
    });
    child.on("error", (err) => resolve({ ok: false, error: String(err) }));
  });
}


// ---------------------------------------------------------------------
// Step 8: start services. Order: API first (daemon depends on it).
// ---------------------------------------------------------------------
async function startServices() {
  if (!_ctx.sc) return { ok: false, error: "service-control unavailable" };
  const apiResult = await _ctx.sc.startService(_ctx.services.api);
  if (apiResult.kind !== "started" && apiResult.kind !== "already_running") {
    return { ok: false, stage: "api", result: apiResult };
  }
  // Brief wait so the API can bind its port before the daemon checks
  // for it (DependOnService alone doesn't wait for app readiness).
  await new Promise((r) => setTimeout(r, 1500));
  const daemonResult = await _ctx.sc.startService(_ctx.services.daemon);
  if (daemonResult.kind !== "started" && daemonResult.kind !== "already_running") {
    return { ok: false, stage: "daemon", result: daemonResult, apiResult };
  }
  return { ok: true, apiResult, daemonResult };
}

async function serviceStatus() {
  if (!_ctx.sc) return { api: { kind: "unknown" }, daemon: { kind: "unknown" } };
  const api = await _ctx.sc.queryService(_ctx.services.api);
  const daemon = await _ctx.sc.queryService(_ctx.services.daemon);
  return { api, daemon };
}


// ---------------------------------------------------------------------
// Registration
// ---------------------------------------------------------------------
// v1.3.4: idempotent. main.js now calls register() unconditionally so
// the Mini Monitor process can host wizard:* IPC channels (the wizard
// is opened as a child modal of Mini, not a separate Setup.exe
// process). Calling ipcMain.handle twice on the same channel throws,
// so guard with a one-shot flag — subsequent calls just refresh _ctx.
let _registered = false;
function register(ctx) {
  _ctx = ctx;
  if (_registered) return;
  _registered = true;
  const { ipcMain, app } = ctx;
  const { shell } = require("electron");

  // v1.3.2 fix B4: include the parsed values so the wizard can
  // pre-fill its forms on re-run. checkConfig() alone returns only
  // status — without values, every re-run resets to defaults and
  // the operator types their PG password again. Returns:
  //   { kind, missingKeys?, placeholderKeys?, path, values }
  // where values is the parsed {key: value} dict (empty {} if no
  // file). missingKeys / placeholderKeys are still populated by
  // validateConfig for the CTA logic.
  ipcMain.handle("wizard:read-config",   () => {
    const status = ctx.configCheck.checkConfig();
    const parsed = ctx.configCheck.readConfig();
    return { ...status, values: (parsed && parsed.values) || {} };
  });
  ipcMain.handle("wizard:config-path",   () => ctx.configCheck.configPath());
  ipcMain.handle("wizard:prereq-check",  () => prereqCheck());
  ipcMain.handle("wizard:pg-test",       (_e, params) => testPgConnection(params));
  ipcMain.handle("wizard:pick-credentials", () => pickCredentials());
  ipcMain.handle("wizard:copy-credentials", (_e, src) => copyCredentials(src));
  ipcMain.handle("wizard:write-config",  (_e, values) => writeConfig(values));
  ipcMain.handle("wizard:db-init",       (e) => runDbInit(e.sender));
  ipcMain.handle("wizard:start-services", () => startServices());
  ipcMain.handle("wizard:service-status", () => serviceStatus());
  ipcMain.handle("wizard:open-web-console", () => {
    shell.openExternal("http://127.0.0.1:17600/");
    return { ok: true };
  });
  ipcMain.handle("wizard:launch-mini", (e) => {
    try {
      // v1.3.4: when the wizard is a child modal of Mini (current
      // launch-wizard flow), Mini is already alive as our parent
      // window. Focus it instead of spawning another instance.
      const { BrowserWindow } = require("electron");
      const w = BrowserWindow.fromWebContents(e.sender);
      const parent = w && w.getParentWindow();
      if (parent && !parent.isDestroyed()) {
        parent.focus();
        return { ok: true, focused: true };
      }
      // Standalone Setup.exe — spawn Mini.exe.
      const dir = path.dirname(process.execPath);
      const miniExe = path.join(dir, "WinServerRAG Mini.exe");
      if (fs.existsSync(miniExe)) {
        shell.openPath(miniExe);
      } else {
        // dev fallback: spawn ourselves with --mode=mini
        spawn(process.execPath, ["--mode=mini"], { detached: true, stdio: "ignore" }).unref();
      }
      return { ok: true, spawned: true };
    } catch (err) {
      return { ok: false, error: String(err) };
    }
  });
  // v1.3.4: close only the originating wizard window. When the wizard
  // is a child modal of Mini, calling app.quit() would kill Mini too.
  // Closing the specific BrowserWindow leaves Mini alive; for the
  // standalone Setup.exe case (no parent), the closed event triggers
  // window-all-closed → app.quit() naturally.
  ipcMain.handle("wizard:close", (e) => {
    try {
      const { BrowserWindow } = require("electron");
      const w = BrowserWindow.fromWebContents(e.sender);
      if (w) w.close();
    } catch {}
    return { ok: true };
  });
}

module.exports = { register };
