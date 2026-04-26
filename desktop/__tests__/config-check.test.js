// node:test for config-check.js — pure-JS, no Electron.
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const cc = require("../config-check.js");


// ---------------------------------------------------------------------
// parseEnv
// ---------------------------------------------------------------------
test("parseEnv: skips comments and blank lines", () => {
  const input = `
# this is a comment
GOOGLE_EMAIL=you@example.com

PGPASSWORD=secret
# another comment
`.trim();
  const r = cc.parseEnv(input);
  assert.equal(r.GOOGLE_EMAIL, "you@example.com");
  assert.equal(r.PGPASSWORD, "secret");
  assert.equal(Object.keys(r).length, 2);
});

test("parseEnv: handles values containing equals signs", () => {
  const r = cc.parseEnv("API_BEARER_TOKEN=abc=def=ghi");
  assert.equal(r.API_BEARER_TOKEN, "abc=def=ghi");
});

test("parseEnv: first occurrence wins (mirrors src/config.py setdefault)", () => {
  const r = cc.parseEnv("KEY=first\nKEY=second");
  assert.equal(r.KEY, "first");
});

test("parseEnv: trims whitespace around key + value", () => {
  const r = cc.parseEnv("  KEY  =  value with trailing  ");
  assert.equal(r.KEY, "value with trailing");
});

test("parseEnv: ignores lines without '='", () => {
  const r = cc.parseEnv("just a string\nKEY=value");
  assert.equal(r.KEY, "value");
  assert.equal(Object.keys(r).length, 1);
});


// ---------------------------------------------------------------------
// validateConfig
// ---------------------------------------------------------------------
test("validateConfig: missing_file when parsed is null", () => {
  const r = cc.validateConfig(null);
  assert.deepEqual(r, { kind: "missing_file" });
});

test("validateConfig: ok when all required keys are present + non-placeholder", () => {
  const parsed = {
    path: "/tmp/x",
    values: {
      GOOGLE_EMAIL: "real@example.com",
      GOOGLE_OAUTH_CLIENT_ID: "real-client-id",
      GOOGLE_OAUTH_CLIENT_SECRET: "real-secret",
      PGPASSWORD: "real-password",
    },
  };
  assert.deepEqual(cc.validateConfig(parsed), { kind: "ok" });
});

test("validateConfig: incomplete when a required key is missing", () => {
  const parsed = {
    path: "/tmp/x",
    values: {
      GOOGLE_EMAIL: "x@y.z",
      GOOGLE_OAUTH_CLIENT_ID: "id",
      // GOOGLE_OAUTH_CLIENT_SECRET missing
      PGPASSWORD: "pw",
    },
  };
  const r = cc.validateConfig(parsed);
  assert.equal(r.kind, "incomplete");
  assert.deepEqual(r.missingKeys, ["GOOGLE_OAUTH_CLIENT_SECRET"]);
  assert.deepEqual(r.placeholderKeys, []);
});

test("validateConfig: incomplete when a required key is blank string", () => {
  const parsed = {
    path: "/tmp/x",
    values: {
      GOOGLE_EMAIL: "a@b.c",
      GOOGLE_OAUTH_CLIENT_ID: "id",
      GOOGLE_OAUTH_CLIENT_SECRET: "sec",
      PGPASSWORD: "",
    },
  };
  const r = cc.validateConfig(parsed);
  assert.equal(r.kind, "incomplete");
  assert.deepEqual(r.missingKeys, ["PGPASSWORD"]);
});

test("validateConfig: incomplete when key has placeholder example value", () => {
  const parsed = {
    path: "/tmp/x",
    values: {
      GOOGLE_EMAIL: "you@example.com",
      GOOGLE_OAUTH_CLIENT_ID: "real-id",
      GOOGLE_OAUTH_CLIENT_SECRET: "real-secret",
      PGPASSWORD: "change_me",
    },
  };
  const r = cc.validateConfig(parsed);
  assert.equal(r.kind, "incomplete");
  assert.deepEqual(r.missingKeys, []);
  assert.deepEqual(r.placeholderKeys.sort(), ["GOOGLE_EMAIL", "PGPASSWORD"].sort());
});


// ---------------------------------------------------------------------
// buildEnvText / writeConfig (round-trip via tmp file)
// ---------------------------------------------------------------------
test("buildEnvText: includes header and groups by section", () => {
  const text = cc.buildEnvText({
    GOOGLE_EMAIL: "x@y.z",
    PGPASSWORD: "secret",
  }, { timestamp: "2026-01-01T00:00:00Z" });
  assert.match(text, /^# WinServerRAG runtime configuration/m);
  assert.match(text, /Written by Setup Wizard at 2026-01-01T00:00:00Z/);
  assert.match(text, /^# --- Google OAuth ---$/m);
  assert.match(text, /^GOOGLE_EMAIL=x@y\.z$/m);
  assert.match(text, /^# --- PostgreSQL ---$/m);
  assert.match(text, /^PGPASSWORD=secret$/m);
});

test("buildEnvText: extras (unknown keys) land in 'Other' section", () => {
  const text = cc.buildEnvText({ MY_CUSTOM_FLAG: "1" });
  assert.match(text, /^# --- Other ---$/m);
  assert.match(text, /^MY_CUSTOM_FLAG=1$/m);
});

test("writeConfig + readConfig round-trip", () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "wsrg-cfg-"));
  const dest = path.join(tmp, "config.v2.env");
  const values = {
    GOOGLE_EMAIL: "real@example.com",
    GOOGLE_OAUTH_CLIENT_ID: "id-123",
    GOOGLE_OAUTH_CLIENT_SECRET: "sec-456",
    PGHOST: "db.local",
    PGPORT: "5432",
    PGUSER: "postgres",
    PGPASSWORD: "p@ss!word",
    PGDATABASE: "winserverrag",
  };
  const wr = cc.writeConfig(values, { path: dest, skipBackup: true });
  assert.equal(wr.path, dest);
  assert.ok(fs.existsSync(dest));

  const re = cc.readConfig({ path: dest });
  assert.equal(re.values.GOOGLE_EMAIL, "real@example.com");
  assert.equal(re.values.PGPASSWORD, "p@ss!word"); // special chars survive
  assert.equal(re.values.PGHOST, "db.local");

  const status = cc.validateConfig(re);
  assert.equal(status.kind, "ok");

  fs.rmSync(tmp, { recursive: true, force: true });
});

test("writeConfig: backs up existing file to .bak", () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "wsrg-cfg-"));
  const dest = path.join(tmp, "config.v2.env");
  fs.writeFileSync(dest, "OLD=value\n", { encoding: "utf-8" });
  cc.writeConfig({ NEW: "value" }, { path: dest });
  const bak = dest + ".bak";
  assert.ok(fs.existsSync(bak), ".bak should exist");
  assert.equal(fs.readFileSync(bak, "utf-8"), "OLD=value\n");
  assert.match(fs.readFileSync(dest, "utf-8"), /^NEW=value$/m);
  fs.rmSync(tmp, { recursive: true, force: true });
});

test("writeConfig: atomic rename leaves no .tmp on success", () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "wsrg-cfg-"));
  const dest = path.join(tmp, "config.v2.env");
  cc.writeConfig({ K: "v" }, { path: dest, skipBackup: true });
  assert.ok(!fs.existsSync(dest + ".tmp"), "tmp should be renamed");
  assert.ok(fs.existsSync(dest));
  fs.rmSync(tmp, { recursive: true, force: true });
});

test("checkConfig: returns missing_file kind when path doesn't exist", () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "wsrg-cfg-"));
  const r = cc.checkConfig({ path: path.join(tmp, "nope.env") });
  assert.equal(r.kind, "missing_file");
  fs.rmSync(tmp, { recursive: true, force: true });
});


// ---------------------------------------------------------------------
// REQUIRED_KEYS / OPTIONAL_KEYS / PLACEHOLDER_VALUES exports
// ---------------------------------------------------------------------
test("REQUIRED_KEYS mirrors src/config.py", () => {
  // If src/config.py adds a required key, this should fail loudly
  // (forcing the dev to update the wizard's required fields).
  assert.deepEqual(
    cc.REQUIRED_KEYS,
    ["GOOGLE_EMAIL", "GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET", "PGPASSWORD"],
  );
});

test("PLACEHOLDER_VALUES recognizes example file's defaults", () => {
  for (const v of ["change_me", "you@example.com", "xxxx.apps.googleusercontent.com", "xxxx"]) {
    assert.ok(cc.PLACEHOLDER_VALUES.has(v), `${v} should be flagged as placeholder`);
  }
});
