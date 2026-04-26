// config-check.js — read + validate WinServerRAG runtime config.
//
// Mini Monitor calls checkConfig() every 5s alongside the daemon SCM
// poll. If config is missing or incomplete, the renderer surfaces a
// "Run Setup Wizard" call-to-action over the daemon row instead of the
// usual "API 不通" red status (which isn't actionable).
//
// Setup Wizard calls readConfig() to pre-fill the form on resume + uses
// writeConfig() to persist the assembled values atomically.
//
// Pure Node (no Electron deps) so it can be unit-tested under node:test.
"use strict";

const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");

// ---------------------------------------------------------------------
// Where the runtime config lives.
// ---------------------------------------------------------------------
// Production: %ProgramData%\WinServerRAG\config\config.v2.env
// Dev runs (when launched from the repo) honor WINSERVERRAG_CONFIG_DIR
// just like src/config.py does.
function configDir() {
  const env = process.env.WINSERVERRAG_CONFIG_DIR;
  if (env) return env;
  // Default: ProgramData\WinServerRAG\config (Windows). On non-Windows
  // (developer mac/linux running tests) fall back to a safe stub.
  if (process.platform === "win32") {
    const programData = process.env.ProgramData || "C:\\ProgramData";
    return path.join(programData, "WinServerRAG", "config");
  }
  return path.join(os.tmpdir(), "WinServerRAG", "config");
}

function configPath() {
  // Prefer v2; fall back to legacy config.env if v2 doesn't exist (only
  // matters if someone restored a pre-v0.4 install).
  const dir = configDir();
  const v2 = path.join(dir, "config.v2.env");
  const v1 = path.join(dir, "config.env");
  if (fs.existsSync(v2)) return v2;
  if (fs.existsSync(v1)) return v1;
  return v2; // canonical, even if missing
}

// ---------------------------------------------------------------------
// Required keys for the daemon to actually start (mirrors src/config.py
// _REQUIRED). If any of these are missing OR present-but-blank, we
// flag the config as INCOMPLETE.
// ---------------------------------------------------------------------
const REQUIRED_KEYS = [
  "GOOGLE_EMAIL",
  "GOOGLE_OAUTH_CLIENT_ID",
  "GOOGLE_OAUTH_CLIENT_SECRET",
  "PGPASSWORD",
];

// Optional keys we surface to the wizard / Mini Monitor UI but don't
// require. Their defaults come from config.py (_env defaults).
const OPTIONAL_KEYS = [
  "PGHOST", "PGPORT", "PGUSER", "PGDATABASE",
  "GOOGLE_TOKEN_PATH",
  "EMBEDDING_MODEL", "EMBEDDING_DEVICE", "BATCH_SIZE",
  "DAEMON_WORKER_THREADS",
  "API_HOST", "API_PORT", "API_BEARER_TOKEN",
];

// Sentinel values that operators commonly leave in from the example
// file. We treat these as "not yet configured".
const PLACEHOLDER_VALUES = new Set([
  "change_me",
  "you@example.com",
  "xxxx.apps.googleusercontent.com",
  "xxxx",
]);


// ---------------------------------------------------------------------
// Parse a config.v2.env file into a plain object.
// Strips comments, blank lines; preserves last-write-wins on duplicates
// (matches src/config.py's `os.environ.setdefault` semantics — first
// occurrence wins. We mirror that here for round-trip fidelity.)
// ---------------------------------------------------------------------
function parseEnv(text) {
  const out = {};
  const lines = String(text || "").split(/\r?\n/);
  for (const raw of lines) {
    const line = raw.trim();
    if (!line || line.startsWith("#")) continue;
    const eq = line.indexOf("=");
    if (eq < 0) continue;
    const key = line.slice(0, eq).trim();
    const value = line.slice(eq + 1).trim();
    if (!(key in out)) out[key] = value;
  }
  return out;
}

function readConfig(opts = {}) {
  const p = opts.path || configPath();
  if (!fs.existsSync(p)) return null;
  const text = fs.readFileSync(p, { encoding: "utf-8" });
  return { path: p, values: parseEnv(text) };
}


// ---------------------------------------------------------------------
// Validate the parsed config against REQUIRED_KEYS + placeholder
// detection. Returns:
//   { kind: "missing_file" }     — file doesn't exist
//   { kind: "incomplete", missingKeys: [...], placeholderKeys: [...] }
//   { kind: "ok" }
// ---------------------------------------------------------------------
function validateConfig(parsed) {
  if (!parsed) return { kind: "missing_file" };
  const missing = [];
  const placeholder = [];
  for (const k of REQUIRED_KEYS) {
    const v = parsed.values[k];
    if (v === undefined || v === "") {
      missing.push(k);
    } else if (PLACEHOLDER_VALUES.has(v)) {
      placeholder.push(k);
    }
  }
  if (missing.length || placeholder.length) {
    return {
      kind: "incomplete",
      missingKeys: missing,
      placeholderKeys: placeholder,
    };
  }
  return { kind: "ok" };
}

function checkConfig(opts = {}) {
  const parsed = readConfig(opts);
  const status = validateConfig(parsed);
  return {
    ...status,
    path: (parsed && parsed.path) || configPath(),
  };
}


// ---------------------------------------------------------------------
// Atomic write — assemble the env content from a {key: value} map and
// write to disk via tmp + rename. Preserves a header comment block so
// the file is self-documenting even on first run.
// ---------------------------------------------------------------------
function buildEnvText(values, opts = {}) {
  const ts = opts.timestamp || new Date().toISOString();
  const header = [
    "# WinServerRAG runtime configuration",
    `# Written by Setup Wizard at ${ts}`,
    "# Edit by hand only if you know what you're doing — re-running the",
    "# Setup Wizard will overwrite this file (after backing it up to",
    "# config.v2.env.bak).",
    "",
  ];
  // Group keys for readability — Google, Postgres, indexing, API.
  const groups = [
    { name: "Google OAuth", keys: ["GOOGLE_EMAIL", "GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET", "GOOGLE_TOKEN_PATH"] },
    { name: "PostgreSQL",   keys: ["PGHOST", "PGPORT", "PGUSER", "PGPASSWORD", "PGDATABASE"] },
    { name: "Indexing",     keys: ["EMBEDDING_MODEL", "EMBEDDING_DEVICE", "BATCH_SIZE", "DAEMON_WORKER_THREADS", "INDEX_IMAGE_OCR"] },
    { name: "Control API",  keys: ["API_HOST", "API_PORT", "API_BEARER_TOKEN"] },
  ];
  const lines = [...header];
  const written = new Set();
  for (const g of groups) {
    lines.push(`# --- ${g.name} ---`);
    for (const k of g.keys) {
      if (k in values) {
        lines.push(`${k}=${values[k]}`);
        written.add(k);
      }
    }
    lines.push("");
  }
  // Any extra keys passed in that didn't match a group go at the end.
  const extras = Object.keys(values).filter((k) => !written.has(k));
  if (extras.length) {
    lines.push("# --- Other ---");
    for (const k of extras) lines.push(`${k}=${values[k]}`);
    lines.push("");
  }
  return lines.join("\n");
}

function writeConfig(values, opts = {}) {
  const dest = opts.path || configPath();
  const dir = path.dirname(dest);
  if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });
  // Backup existing file before overwriting.
  if (fs.existsSync(dest) && !opts.skipBackup) {
    const bak = dest + ".bak";
    try { fs.copyFileSync(dest, bak); } catch { /* best-effort */ }
  }
  const text = buildEnvText(values, opts);
  // Atomic write via tmp + rename so a power loss mid-write can't
  // corrupt the file the daemon reads.
  const tmp = dest + ".tmp";
  fs.writeFileSync(tmp, text, { encoding: "utf-8" });
  fs.renameSync(tmp, dest);
  return { path: dest, bytes: Buffer.byteLength(text, "utf-8") };
}


module.exports = {
  configDir,
  configPath,
  parseEnv,
  readConfig,
  validateConfig,
  checkConfig,
  buildEnvText,
  writeConfig,
  REQUIRED_KEYS,
  OPTIONAL_KEYS,
  PLACEHOLDER_VALUES,
};
