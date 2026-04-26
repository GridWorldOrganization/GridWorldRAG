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
function testPgConnection(params) {
  const { host, port, user, password, database } = params || {};
  return new Promise((resolve) => {
    // Build a temporary env so we don't leak the password into argv.
    const env = {
      ...process.env,
      PGHOST: host || "localhost",
      PGPORT: port || "5432",
      PGUSER: user || "postgres",
      PGPASSWORD: password || "",
      PGDATABASE: database || "postgres",
    };
    // psql is usually at C:\Program Files\PostgreSQL\<ver>\bin\psql.exe.
    const candidates = [
      "C:\\Program Files\\PostgreSQL\\17\\bin\\psql.exe",
      "C:\\Program Files\\PostgreSQL\\16\\bin\\psql.exe",
      "C:\\Program Files\\PostgreSQL\\15\\bin\\psql.exe",
      "psql.exe", // PATH lookup as last resort
    ];
    const psql = candidates.find((p) => p === "psql.exe" || fs.existsSync(p)) || candidates[candidates.length - 1];
    execFile(psql, ["-c", "SELECT 1"], { env, timeout: 5000, windowsHide: true },
      (err, stdout, stderr) => {
        if (err) {
          resolve({
            ok: false,
            error: String((err && err.message) || stderr || err).slice(0, 500),
          });
          return;
        }
        resolve({ ok: true, stdout: String(stdout).slice(0, 200) });
      });
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

function copyCredentials(srcPath) {
  return new Promise((resolve) => {
    try {
      const dst = path.join(_ctx.configCheck.configDir(), "credentials.json");
      fs.mkdirSync(path.dirname(dst), { recursive: true });
      fs.copyFileSync(srcPath, dst);
      resolve({ ok: true, dest: dst });
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
function register(ctx) {
  _ctx = ctx;
  const { ipcMain, app } = ctx;
  const { shell } = require("electron");

  ipcMain.handle("wizard:read-config",   () => ctx.configCheck.checkConfig());
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
  ipcMain.handle("wizard:launch-mini", () => {
    try {
      const dir = path.dirname(process.execPath);
      const miniExe = path.join(dir, "WinServerRAG Mini.exe");
      if (fs.existsSync(miniExe)) {
        shell.openPath(miniExe);
      } else {
        // dev fallback: spawn ourselves with --mode=mini
        spawn(process.execPath, ["--mode=mini"], { detached: true, stdio: "ignore" }).unref();
      }
      return { ok: true };
    } catch (err) {
      return { ok: false, error: String(err) };
    }
  });
  ipcMain.handle("wizard:close", () => { app.quit(); return { ok: true }; });
}

module.exports = { register };
