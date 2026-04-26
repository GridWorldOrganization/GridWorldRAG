// service-control.js — pure-Node helpers for querying / starting Windows
// services from the Electron main process. Kept dependency-free and
// side-effect-light so it can be unit-tested under node:test without
// spinning up an Electron context.
//
// Why this module exists
// ----------------------
// Mini-monitor's primary job is monitoring the daemon, NOT the API. If we
// only checked daemon state via the FastAPI /api/stats endpoint (PR #28),
// the mini-monitor would say "状態取得失敗" any time the API was down —
// which is precisely when the operator needs the mini-monitor most.
// Talking to the Windows Service Control Manager directly via `sc query`
// gives us a second, independent data stream.
//
// Security note: we never invoke `sc create` / `nssm install` from here.
// All install operations happen in the Inno Setup installer
// (installer/install-services.ps1). That installer is the only piece
// that needs admin. After install, this module's `startService()` works
// without UAC because the installer's PowerShell relaxes the service
// SDDL to grant SERVICE_START + SERVICE_QUERY_STATUS to the local group
// `WinServerRAG Operators`.
//
// Codex-flagged constraints honored here:
//   - sc.exe is invoked at its full path %SystemRoot%\System32\sc.exe
//     (avoids PATH-injection / sc.exe-on-path replacement).
//   - State is parsed from the numeric `STATE :  4  RUNNING` line
//     (4 = RUNNING). Localized text on JP Windows might not say "RUNNING".
//   - sc start error codes are mapped concretely (5/1056/1060/1068/1053).
//   - Pending states (2/3/5/6) are surfaced as a distinct kind, not
//     conflated with UNKNOWN.
"use strict";

const { execFile } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");

// ---------------------------------------------------------------------
// State machine (numeric, locale-independent)
// ---------------------------------------------------------------------
// Sourced from Microsoft SERVICE_STATUS dwCurrentState values:
//   1 SERVICE_STOPPED          ■ stopped
//   2 SERVICE_START_PENDING    ⏳ starting
//   3 SERVICE_STOP_PENDING     ⏳ stopping
//   4 SERVICE_RUNNING          ● running
//   5 SERVICE_CONTINUE_PENDING ⏳ resuming
//   6 SERVICE_PAUSE_PENDING    ⏳ pausing (rare for our daemon)
//   7 SERVICE_PAUSED           (rare; mini-monitor treats as RUNNING for UI purposes)
const STATE_NAMES = {
  1: "stopped",
  2: "start_pending",
  3: "stop_pending",
  4: "running",
  5: "continue_pending",
  6: "pause_pending",
  7: "paused",
};

const PENDING_STATES = new Set([2, 3, 5, 6]);


// ---------------------------------------------------------------------
// sc.exe absolute path
// ---------------------------------------------------------------------
function scExePath() {
  // %SystemRoot%\System32\sc.exe — avoids PATH lookup. Honors a different
  // SystemRoot if the OS is genuinely installed elsewhere (uncommon).
  const root = process.env.SystemRoot || "C:\\Windows";
  return path.join(root, "System32", "sc.exe");
}


// ---------------------------------------------------------------------
// sc query parsing
// ---------------------------------------------------------------------
// `sc query <name>` writes a multi-line block; we extract the first
// "STATE              : N  TEXT" line and return N. The numeric is
// locale-stable, the text is not.
//
// Returns one of:
//   { kind: "running" | "stopped" | "start_pending" | "stop_pending" |
//           "continue_pending" | "pause_pending" | "paused", code: N }
//   { kind: "not_installed", code: 1060 }
//   { kind: "unknown", code: <something>, raw: <stdout> }
function parseScQueryOutput(stdout, returncode) {
  if (returncode === 1060) {
    return { kind: "not_installed", code: 1060 };
  }
  if (returncode !== 0) {
    return { kind: "unknown", code: returncode, raw: String(stdout || "") };
  }
  const text = String(stdout || "");
  // STATE              : 4  RUNNING
  // Whitespace + ":" + whitespace + digit. Tolerates locale-varying text after.
  const m = text.match(/STATE\s*:\s*(\d+)/);
  if (!m) {
    return { kind: "unknown", code: returncode, raw: text };
  }
  const n = parseInt(m[1], 10);
  const name = STATE_NAMES[n];
  if (!name) {
    return { kind: "unknown", code: returncode, raw: text };
  }
  return { kind: name, code: n };
}


// ---------------------------------------------------------------------
// queryService — high-level: returns parsed state for a single service.
// Surfaces FileNotFoundError / TimeoutError as `unknown` so the mini-
// monitor never crashes from a missing sc.exe (Linux CI) or a hung SCM.
// ---------------------------------------------------------------------
function queryService(serviceName, opts = {}) {
  const exe = opts.scExe || scExePath();
  const timeoutMs = opts.timeoutMs || 3000;
  return new Promise((resolve) => {
    execFile(exe, ["query", serviceName], {
      timeout: timeoutMs,
      windowsHide: true,
    }, (err, stdout, stderr) => {
      // sc returns rc=1060 "service does not exist" via the err.code path.
      const rc = err && typeof err.code === "number" ? err.code : (err ? -1 : 0);
      // execFile timeouts surface err.killed = true; err.code may be undefined.
      if (err && err.killed) {
        resolve({ kind: "unknown", code: -1, raw: "(timeout)" });
        return;
      }
      // ENOENT (sc.exe missing) → unknown, not crash.
      if (err && err.code === "ENOENT") {
        resolve({ kind: "unknown", code: -1, raw: "(sc.exe not found)" });
        return;
      }
      resolve(parseScQueryOutput(stdout, rc));
    });
  });
}


// ---------------------------------------------------------------------
// sc start — exit code map
// ---------------------------------------------------------------------
// Codex-flagged: don't treat all non-zero as a generic failure. The
// operator's recovery action depends on which.
const START_ERROR_CLASSES = {
  0:    { kind: "started",        message: "started" },
  5:    { kind: "access_denied",  message: "アクセス拒否 (SDDL 確認)" },
  1056: { kind: "already_running", message: "既に実行中" },           // idempotent
  1060: { kind: "not_installed",   message: "未インストール" },
  1068: { kind: "dependency_failed", message: "依存サービス (API) 起動失敗" },
  1053: { kind: "no_response",     message: "応答タイムアウト" },
};

function classifyStartExit(code) {
  if (code in START_ERROR_CLASSES) return START_ERROR_CLASSES[code];
  return { kind: "unknown_error", message: `不明なエラー (exit ${code})`, code };
}


// ---------------------------------------------------------------------
// startService — kick off `sc start`, do NOT poll here. Caller polls
// queryService() to observe the START_PENDING → RUNNING transition.
// ---------------------------------------------------------------------
function startService(serviceName, opts = {}) {
  const exe = opts.scExe || scExePath();
  const timeoutMs = opts.timeoutMs || 5000;
  return new Promise((resolve) => {
    execFile(exe, ["start", serviceName], {
      timeout: timeoutMs,
      windowsHide: true,
    }, (err, stdout) => {
      const rc = err && typeof err.code === "number" ? err.code : (err ? -1 : 0);
      if (err && err.killed) {
        resolve({ kind: "no_response", message: "起動コマンドがタイムアウト", code: -1 });
        return;
      }
      if (err && err.code === "ENOENT") {
        resolve({ kind: "unknown_error", message: "sc.exe not found", code: -1 });
        return;
      }
      resolve(classifyStartExit(rc));
    });
  });
}


// ---------------------------------------------------------------------
// Dev-mode detection
// ---------------------------------------------------------------------
// Used to differentiate two "service not installed" cases:
//   1) Operator hasn't run the installer (production deploy, but missing)
//   2) Operator is a developer working from the repo (no installer ever)
//
// Heuristic: the mini-monitor app dir is a sibling of `.venv\Scripts\python.exe`
// in the repo. We walk up at most 2 directories from `__dirname` looking for
// `.venv\Scripts\python.exe`. If found AND service is `not_installed`, it's
// dev mode and the warning dialog tells the operator to run
// `python -m src.rag_daemon` from a venv-activated terminal.
function detectDevMode(startDir) {
  let dir = startDir || __dirname;
  for (let depth = 0; depth < 3; depth++) {
    const venvPython = path.join(dir, ".venv", "Scripts", "python.exe");
    try {
      if (fs.existsSync(venvPython)) {
        return { isDev: true, venvPython, repoRoot: dir };
      }
    } catch {
      // ignore
    }
    const parent = path.dirname(dir);
    if (parent === dir) break;
    dir = parent;
  }
  return { isDev: false };
}


module.exports = {
  STATE_NAMES,
  PENDING_STATES,
  scExePath,
  parseScQueryOutput,
  queryService,
  classifyStartExit,
  startService,
  detectDevMode,
};
