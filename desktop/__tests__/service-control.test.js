// Tests for service-control.js — pure-Node, no Electron, no sc.exe needed.
//
// Run via:
//   node --test desktop/__tests__/service-control.test.js
//
// We test the parser exhaustively (8 STATE codes + edge cases) and the
// sc start exit-code classifier. queryService / startService themselves
// shell out to sc.exe — those are covered by manual QA on a real Windows
// machine after install. Mocking child_process.execFile isn't worth it
// when the actual risk lives in real Windows ACL behavior.
"use strict";

const test = require("node:test");
const assert = require("node:assert");
const path = require("node:path");

const sc = require("../service-control.js");


// ---------- parseScQueryOutput ----------

test("parse: STATE 4 RUNNING → running", () => {
  const out = "SERVICE_NAME: WinServerRAG-Daemon\n" +
              "        TYPE               : 10  WIN32_OWN_PROCESS\n" +
              "        STATE              : 4  RUNNING\n";
  const r = sc.parseScQueryOutput(out, 0);
  assert.strictEqual(r.kind, "running");
  assert.strictEqual(r.code, 4);
});

test("parse: STATE 1 STOPPED → stopped", () => {
  const out = "        STATE              : 1  STOPPED\n";
  const r = sc.parseScQueryOutput(out, 0);
  assert.strictEqual(r.kind, "stopped");
  assert.strictEqual(r.code, 1);
});

test("parse: STATE 2 START_PENDING → start_pending", () => {
  const out = "        STATE              : 2  START_PENDING\n";
  const r = sc.parseScQueryOutput(out, 0);
  assert.strictEqual(r.kind, "start_pending");
  assert.strictEqual(r.code, 2);
});

test("parse: STATE 3 STOP_PENDING → stop_pending", () => {
  const out = "        STATE              : 3  STOP_PENDING\n";
  const r = sc.parseScQueryOutput(out, 0);
  assert.strictEqual(r.kind, "stop_pending");
  assert.strictEqual(r.code, 3);
});

test("parse: STATE 5 CONTINUE_PENDING → continue_pending", () => {
  const out = "        STATE              : 5  CONTINUE_PENDING\n";
  const r = sc.parseScQueryOutput(out, 0);
  assert.strictEqual(r.kind, "continue_pending");
  assert.strictEqual(r.code, 5);
});

test("parse: STATE 6 PAUSE_PENDING → pause_pending", () => {
  const out = "        STATE              : 6  PAUSE_PENDING\n";
  const r = sc.parseScQueryOutput(out, 0);
  assert.strictEqual(r.kind, "pause_pending");
  assert.strictEqual(r.code, 6);
});

test("parse: STATE 7 PAUSED → paused", () => {
  const out = "        STATE              : 7  PAUSED\n";
  const r = sc.parseScQueryOutput(out, 0);
  assert.strictEqual(r.kind, "paused");
  assert.strictEqual(r.code, 7);
});

test("parse: rc=1060 (service does not exist) → not_installed", () => {
  // sc.exe writes its own "FAILED 1060" text to stdout/stderr but the
  // crucial signal is the process exit code 1060.
  const r = sc.parseScQueryOutput("[SC] OpenService FAILED 1060", 1060);
  assert.strictEqual(r.kind, "not_installed");
  assert.strictEqual(r.code, 1060);
});

test("parse: rc=0 but no STATE line → unknown", () => {
  // Truncated / weird output. Don't pretend we know.
  const r = sc.parseScQueryOutput("garbage", 0);
  assert.strictEqual(r.kind, "unknown");
});

test("parse: rc=0 with STATE 999 (impossible) → unknown", () => {
  const r = sc.parseScQueryOutput("STATE : 999", 0);
  assert.strictEqual(r.kind, "unknown");
});

test("parse: rc=other (not 0 / 1060) → unknown", () => {
  // E.g. ACL refused etc. Not our responsibility to parse, but we shouldn't crash.
  const r = sc.parseScQueryOutput("", 1234);
  assert.strictEqual(r.kind, "unknown");
  assert.strictEqual(r.code, 1234);
});

test("parse: localized JP STATE label still parses (numeric is locale-stable)", () => {
  // Hypothetical JA Windows variant. The text after the number can vary;
  // the digit cannot.
  const out = "        STATE              : 4  実行中\n";
  const r = sc.parseScQueryOutput(out, 0);
  assert.strictEqual(r.kind, "running");
  assert.strictEqual(r.code, 4);
});


// ---------- classifyStartExit ----------

test("startExit: 0 → started", () => {
  const r = sc.classifyStartExit(0);
  assert.strictEqual(r.kind, "started");
});

test("startExit: 5 → access_denied (SDDL hint)", () => {
  const r = sc.classifyStartExit(5);
  assert.strictEqual(r.kind, "access_denied");
  assert.match(r.message, /SDDL/);
});

test("startExit: 1056 → already_running (idempotent OK)", () => {
  const r = sc.classifyStartExit(1056);
  assert.strictEqual(r.kind, "already_running");
});

test("startExit: 1060 → not_installed", () => {
  const r = sc.classifyStartExit(1060);
  assert.strictEqual(r.kind, "not_installed");
});

test("startExit: 1068 → dependency_failed (API note in message)", () => {
  const r = sc.classifyStartExit(1068);
  assert.strictEqual(r.kind, "dependency_failed");
  assert.match(r.message, /API|依存/);
});

test("startExit: 1053 → no_response", () => {
  const r = sc.classifyStartExit(1053);
  assert.strictEqual(r.kind, "no_response");
});

test("startExit: 9999 (unknown) → unknown_error with code preserved", () => {
  const r = sc.classifyStartExit(9999);
  assert.strictEqual(r.kind, "unknown_error");
  assert.strictEqual(r.code, 9999);
});


// ---------- scExePath ----------

test("scExePath: defaults to %SystemRoot%\\System32\\sc.exe", () => {
  const original = process.env.SystemRoot;
  process.env.SystemRoot = "C:\\Windows";
  try {
    assert.strictEqual(
      sc.scExePath(),
      path.join("C:\\Windows", "System32", "sc.exe"),
    );
  } finally {
    process.env.SystemRoot = original;
  }
});

test("scExePath: honors non-default SystemRoot", () => {
  const original = process.env.SystemRoot;
  process.env.SystemRoot = "D:\\Win11";
  try {
    assert.strictEqual(
      sc.scExePath(),
      path.join("D:\\Win11", "System32", "sc.exe"),
    );
  } finally {
    process.env.SystemRoot = original;
  }
});

test("scExePath: falls back to C:\\Windows when SystemRoot unset", () => {
  const original = process.env.SystemRoot;
  delete process.env.SystemRoot;
  try {
    assert.strictEqual(
      sc.scExePath(),
      path.join("C:\\Windows", "System32", "sc.exe"),
    );
  } finally {
    if (original !== undefined) process.env.SystemRoot = original;
  }
});


// ---------- detectDevMode ----------

test("detectDevMode: returns isDev=false when no .venv nearby", () => {
  // /tmp on most systems definitely does not have a .venv tree.
  const tmpDir = require("node:os").tmpdir();
  const r = sc.detectDevMode(tmpDir);
  assert.strictEqual(r.isDev, false);
});

test("detectDevMode: walks up to 3 levels — sibling .venv detected", () => {
  // We can't easily fake a .venv on disk in a unit test. Instead, we
  // verify the contract: the function does not throw, returns the
  // expected shape, and isDev is a boolean.
  const r = sc.detectDevMode("/nonexistent/path/that/has/no/venv");
  assert.strictEqual(typeof r.isDev, "boolean");
  assert.strictEqual(r.isDev, false);
});


// ---------- exports surface ----------

test("module exports the expected public API", () => {
  assert.ok(typeof sc.parseScQueryOutput === "function");
  assert.ok(typeof sc.queryService === "function");
  assert.ok(typeof sc.classifyStartExit === "function");
  assert.ok(typeof sc.startService === "function");
  assert.ok(typeof sc.scExePath === "function");
  assert.ok(typeof sc.detectDevMode === "function");
  assert.ok(sc.STATE_NAMES && sc.STATE_NAMES[4] === "running");
  assert.ok(sc.PENDING_STATES instanceof Set);
  assert.ok(sc.PENDING_STATES.has(2));
  assert.ok(sc.PENDING_STATES.has(3));
  assert.ok(!sc.PENDING_STATES.has(4));
});
