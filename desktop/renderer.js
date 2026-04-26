// Mini monitor — polls control API every 250ms.

let API = "http://127.0.0.1:17600";

const $ = (id) => document.getElementById(id);

function fmtBytes(n) {
  if (n == null) return "-";
  const u = ["B","KB","MB","GB","TB"];
  let i = 0, v = Number(n);
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(v >= 10 ? 0 : 1)} ${u[i]}`;
}

async function fetchJSON(path, timeoutMs = 1500) {
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), timeoutMs);
  const slot = path === "/api/stats" ? "statsHttp" : path === "/api/workers" ? "workersHttp" : null;
  try {
    const r = await fetch(API + path, { signal: ctrl.signal });
    if (slot) DBG[slot] = String(r.status);
    if (!r.ok) throw new Error(r.status);
    return await r.json();
  } catch (e) {
    if (slot) DBG[slot] = "ERR " + (e && e.name === "AbortError" ? "timeout" : (e && e.message) || "?");
    throw e;
  } finally {
    clearTimeout(t);
  }
}

let consecutiveFails = 0;
let lastFilesDoneSum = 0;
let _inflight = false;  // prevent overlapping fetches if a tick takes longer than 250ms
let _lastWdetailHash = null;  // gate #wdetail innerHTML writes so we don't flicker

// Debug telemetry — populated every tick, rendered only when the debug
// overlay is open (so rendering cost is zero during normal operation).
const DBG = {
  lastTickAt: null,
  lastOkAt: null,
  lastFailAt: null,
  lastFailReason: null,
  statsHttp: null,    // "200" | "ERR timeout" | "ERR 500" | null
  workersHttp: null,
  statsRaw: null,
  workersRaw: null,
  indicator: "idle",
  daemonDead: false,
  activeCount: 0,
  staleCount: 0,
  workerHb: [],       // [{id, state, age_sec, stale}]
};

// A worker row is considered stale (its owning daemon died / crashed) if
// its heartbeat hasn't advanced within this window. Normal sources of gap:
//   - Drive list_files_in_drive with many pages (can be 60+ sec silently)
//   - Sheets API backoff retries (Semaphore / exponential wait)
//   - PDF extraction timeout (60s internal) followed by upsert
// Choose 120s — comfortably above all of those while still catching a
// dead daemon within a couple of minutes.
const WORKER_STALE_MS = 120000;

function isWorkerStale(w, now) {
  if (!w || !w.heartbeat_at) return false;
  const hb = new Date(w.heartbeat_at).getTime();
  return (now - hb) > WORKER_STALE_MS;
}

function escHtmlAttr(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function setIndicator(kind) {
  // kind: 'idle' | 'ok' | 'active' | 'err' | 'paused'
  // 'paused' overrides 'active' — when the operator manually paused the
  // service, in-flight workers may still be finishing, but the dominant
  // user-visible state is "stopped on purpose".
  const el = $("indicator");
  el.classList.remove("ok", "active", "err", "paused");
  if (kind !== "idle") el.classList.add(kind);
  DBG.indicator = kind;
}

// --- Pause/resume button state machine ---------------------------------
// `null` = unknown (waiting for first /api/stats response). `true` = paused.
// We track this so the button stays in sync with the API and so a click
// can be optimistically reflected before the next 250ms poll lands.
let _pausedState = null;
let _pauseInflight = false;

// --- Daemon state cache (v1.3) ----------------------------------------
// Populated by a separate 5s polling loop that calls window.winsrv.daemonStatus()
// (Electron main → sc query). Kept independent from /api/stats so that
// "API down + Daemon up" is still visible. Shape mirrors service-control.js:
//   { kind, code, devMode: { isDev, venvPython?, repoRoot? } }
let _daemonState = null;
// Tri-state: null = first poll hasn't completed, "..." = inflight start,
// "ok" = idle, "err" = last start failed (sticky until next poll succeeds).
let _daemonStartFlight = null;

function setPauseUI(paused) {
  const btn = $("pause-btn");
  if (!btn) return;
  // The button label depends on a 2D state: (daemon SCM state, API paused).
  // Most states are handled here based on the daemon kind we last saw.
  // The API-derived `paused` flag is only meaningful when the daemon
  // is actually running.
  const d = _daemonState && _daemonState.kind;
  if (d === "running" || d === "paused") {
    if (paused) {
      btn.textContent = "▶";
      btn.title = "サービスを再開する";
      btn.classList.add("paused");
    } else {
      btn.textContent = "⏸";
      btn.title = "サービスを一時停止する (新規タスク取得を止める。進行中タスクは完走)";
      btn.classList.remove("paused");
    }
    btn.disabled = _pauseInflight;
    return;
  }
  if (d === "stopped") {
    btn.textContent = "▶";
    btn.title = "Daemon サービスを起動する (UAC 不要)";
    btn.classList.add("paused");
    btn.disabled = !!_daemonStartFlight;
    return;
  }
  if (d === "start_pending" || d === "stop_pending" ||
      d === "continue_pending" || d === "pause_pending") {
    btn.textContent = "⏳";
    btn.title = "サービス遷移中...";
    btn.disabled = true;
    return;
  }
  if (d === "not_installed") {
    const isDev = _daemonState.devMode && _daemonState.devMode.isDev;
    btn.textContent = isDev ? "💻" : "⊘";
    btn.title = isDev ? "dev mode 検出 — クリックで起動方法を表示"
                      : "Daemon が未インストール — クリックでインストーラー案内";
    btn.disabled = false;
    return;
  }
  // unknown / null
  btn.textContent = "?";
  btn.title = "Daemon 状態取得中...";
  btn.disabled = true;
}

async function togglePause() {
  // Dispatch on daemon state — the button's meaning depends on it.
  // Codex review made this explicit: a single "pause" handler can't
  // safely paper over "service is stopped" vs "service is running".
  const d = _daemonState && _daemonState.kind;

  // 1) Daemon not registered. Show a help dialog instead of doing
  //    anything system-mutating. Mini-monitor never installs.
  if (d === "not_installed") {
    const isDev = _daemonState.devMode && _daemonState.devMode.isDev;
    if (isDev) showDevModeDialog(_daemonState.devMode);
    else       showNotInstalledDialog();
    return;
  }

  // 2) Daemon registered but stopped → start it (no UAC, SDDL relaxed).
  if (d === "stopped") {
    if (_daemonStartFlight) return;
    _daemonStartFlight = "starting";
    setPauseUI(_pausedState);
    try {
      const r = await window.winsrv.daemonStart();
      if (r.kind === "started" || r.kind === "already_running") {
        // Optimistic: surface PENDING immediately. Next pollDaemon()
        // will pick up the real RUNNING transition (≤5s lag).
        _daemonState = { kind: "start_pending", code: 2, devMode: _daemonState.devMode };
        renderDaemonRow(_daemonState);
      } else {
        // Hard fail (access_denied / not_installed / dependency_failed
        // / no_response / unknown_error). Show in debug overlay; the
        // daemon row will reflect reality on the next poll.
        DBG.lastFailReason = `daemon-start: ${r.kind} — ${r.message}`;
        DBG.lastFailAt = new Date();
        if (isDebugOpen()) renderDebug();
      }
    } catch (e) {
      DBG.lastFailReason = `daemon-start IPC: ${e && e.message || e}`;
      DBG.lastFailAt = new Date();
      if (isDebugOpen()) renderDebug();
    } finally {
      _daemonStartFlight = null;
      setPauseUI(_pausedState);
    }
    return;
  }

  // 3) PENDING — button is disabled, but defensive no-op anyway.
  if (d === "start_pending" || d === "stop_pending" ||
      d === "continue_pending" || d === "pause_pending") {
    return;
  }

  // 4) Default path: daemon is RUNNING (or PAUSED, treated as running).
  //    Standard pause/resume API call. _pausedState may still be null
  //    on the first ever tick — bail in that case.
  if (_pauseInflight || _pausedState == null) return;
  const target = !_pausedState;
  const apiPath = target ? "/api/daemon/pause" : "/api/daemon/resume";
  _pauseInflight = true;
  setPauseUI(_pausedState);
  try {
    const r = await fetch(API + apiPath, { method: "POST" });
    if (!r.ok) throw new Error("HTTP " + r.status);
    const state = await r.json();
    _pausedState = !!state.paused;
    setPauseUI(_pausedState);
  } catch (e) {
    DBG.lastFailReason = `pause-toggle: ${e && e.message || e}`;
    DBG.lastFailAt = new Date();
    if (isDebugOpen()) renderDebug();
  } finally {
    _pauseInflight = false;
    setPauseUI(_pausedState);
  }
}

// --- Daemon SCM polling ------------------------------------------------
// Independent loop, 5s cadence. Runs whether or not /api/stats responds
// — that's the v1.3 promise to the operator.
async function pollDaemon() {
  try {
    const status = await window.winsrv.daemonStatus();
    _daemonState = status;
    renderDaemonRow(status);
    setPauseUI(_pausedState);
  } catch (e) {
    // Electron IPC genuinely shouldn't fail unless the main process is
    // gone. Surface in debug overlay; leave _daemonState alone.
    DBG.lastFailReason = `daemon-status IPC: ${e && e.message || e}`;
    DBG.lastFailAt = new Date();
    if (isDebugOpen()) renderDebug();
  }
}

// Render the daemon row from a service-control.js result. Independent of
// /api/stats — see comment in tick().
function renderDaemonRow(state) {
  const el = $("service-status");
  if (!el) return;
  switch (state.kind) {
    case "running":
    case "paused":  // treat SCM-level PAUSED same as RUNNING for UI
      el.textContent = "● 実行中";
      el.className = "v ok";
      break;
    case "stopped":
      el.textContent = "■ 停止中 (登録済)";
      el.className = "v muted";
      break;
    case "start_pending":
      el.textContent = "⏳ 起動中...";
      el.className = "v warn";
      break;
    case "stop_pending":
      el.textContent = "⏳ 停止中...";
      el.className = "v warn";
      break;
    case "continue_pending":
    case "pause_pending":
      el.textContent = "⏳ 遷移中...";
      el.className = "v warn";
      break;
    case "not_installed":
      if (state.devMode && state.devMode.isDev) {
        el.textContent = "💻 dev mode (venv)";
        el.className = "v muted";
      } else {
        el.textContent = "✕ 未インストール";
        el.className = "v err";
      }
      break;
    case "unknown":
    default:
      el.textContent = "? 状態取得失敗";
      el.className = "v warn";
  }
}

function showNotInstalledDialog() {
  alert(
    "Daemon サービスが未インストールです。\n\n" +
    "本機能を使うには、管理者権限で\n" +
    "  WinServerRAG-Setup-x.y.z.exe\n" +
    "を実行してインストーラーを完了してください。\n\n" +
    "https://github.com/GridWorldOrganization/GridWorldRAG/releases"
  );
}

function showDevModeDialog(dev) {
  const cmd = (dev.repoRoot || "<repo>") +
              "\\.venv\\Scripts\\python.exe -m src.rag_daemon";
  // Use prompt so the user can copy the command (alert doesn't allow copy
  // in Electron without extra plumbing). Pre-fill the command as the
  // default value; the operator presses Ctrl+C inside the prompt and
  // dismisses with Esc.
  prompt(
    "dev mode 検出: NSSM サービスは未登録です。\n" +
    "venv で Daemon を起動してください (別ターミナルで実行):\n\n" +
    "↓ コマンド (Ctrl+C でコピー、Esc で閉じる)",
    cmd
  );
}

function flashTick() {
  const t = $("progress-tick");
  t.classList.remove("on");
  // force reflow so animation replays
  void t.offsetWidth;
  t.classList.add("on");
}

async function tick() {
  if (_inflight) return;
  _inflight = true;
  DBG.lastTickAt = new Date();
  try {
    const [s, w] = await Promise.all([
      fetchJSON("/api/stats").catch((e) => { DBG.lastFailReason = String(e && e.message || e); return null; }),
      fetchJSON("/api/workers").catch((e) => { DBG.lastFailReason = String(e && e.message || e); return null; }),
    ]);
    if (!s || !w) {
      consecutiveFails++;
      DBG.lastFailAt = new Date();
      if (consecutiveFails >= 2) showErr();
      if (isDebugOpen()) renderDebug();
      return;
    }
    consecutiveFails = 0;
    DBG.lastOkAt = new Date();
    DBG.statsRaw = s;
    DBG.workersRaw = w;

    const now = Date.now();
    const allWorkers = w.workers || [];

    // --- Fresh = heartbeat is recent. Stale = daemon probably died mid-task.
    // Only fresh workers contribute to "active" animation / aggregate progress.
    const activeStates = new Set(["building", "syncing", "listing", "claiming"]);
    const activeWorkers = allWorkers.filter(x =>
      activeStates.has(x.state) && !isWorkerStale(x, now));
    const staleActiveCount = allWorkers.filter(x =>
      activeStates.has(x.state) && isWorkerStale(x, now)).length;

    // Daemon-liveness heuristic: the daemon is presumed dead if every worker
    // row (whatever its state) is stale — the heartbeats stopped arriving.
    const daemonLooksDead = allWorkers.length > 0 && allWorkers.every(x => isWorkerStale(x, now));
    DBG.daemonDead = daemonLooksDead;
    DBG.activeCount = activeWorkers.length;
    DBG.staleCount = staleActiveCount;
    DBG.workerHb = allWorkers.map(x => {
      const hb = x.heartbeat_at ? new Date(x.heartbeat_at).getTime() : 0;
      return {
        id: x.worker_id, state: x.state,
        age_sec: hb ? Math.round((now - hb) / 1000) : null,
        stale: isWorkerStale(x, now),
      };
    });

    // Reflect manual pause state from /api/stats. When paused, the button
    // shows ▶ and the indicator goes gray — overriding "active" even if
    // workers are still finishing in-flight tasks (spec: "in-flight tasks
    // complete" means we promised to drain, not to hide the pause state).
    const stillFlightingInflight = activeWorkers.length > 0;  // for status text
    if (_pausedState !== !!s.paused) {
      _pausedState = !!s.paused;
      setPauseUI(_pausedState);
    }

    const anyErr = allWorkers.some(x => x.state === "error") || s.pg_ok === false;
    // Indicator priority: err > paused > active > ok > idle
    if (daemonLooksDead)                  setIndicator("err");
    else if (anyErr)                       setIndicator("err");
    else if (s.paused)                     setIndicator("paused");
    else if (activeWorkers.length > 0)     setIndicator("active");
    else if (s.pg_ok)                      setIndicator("ok");
    else                                   setIndicator("idle");

    // --- top aggregate progress (sum PER DRIVE, not per worker).
    // 4 workers on one drive all report total_files = the drive's total, so
    // naive sum over workers triple/quadruple-counts total. Group by drive:
    // done = sum of workers' files_done; total = that drive's total (any
    // worker on the drive reports it).
    const perDrive = new Map();
    for (const x of activeWorkers) {
      const did = x.drive_id || "(none)";
      const e = perDrive.get(did) || { done: 0, total: 0 };
      e.done += x.files_done || 0;
      if ((x.total_files || 0) > e.total) e.total = x.total_files || 0;
      perDrive.set(did, e);
    }
    let done = 0, total = 0;
    for (const e of perDrive.values()) { done += e.done; total += e.total; }

    const agg = $("progress-agg");
    const fill = $("progress-fill");
    const label = $("progress-text");
    if (total > 0) {
      const pct = (done / total) * 100;
      fill.style.width = pct.toFixed(1) + "%";
      // Spec (README:149) requires "停止中（手動停止）" when paused. Keep
      // the bar visible so the user sees in-flight work draining, but
      // augment the text so it's clear the daemon is stopped on purpose.
      const suffix = s.paused ? " — 停止中（手動停止）" : "";
      label.textContent = `${done.toLocaleString()} / ${total.toLocaleString()} files (${pct.toFixed(1)}%)${suffix}`;
      // Don't shimmer when paused — the bar should look dormant even if
      // a worker is still finishing the current file.
      if (s.paused) agg.classList.remove("active");
      else agg.classList.add("active");
    } else {
      fill.style.width = "0";
      if (s.paused) {
        label.textContent = "停止中（手動停止）";
      } else {
        label.textContent = activeWorkers.length ? "リスト取得中…" : "アイドル";
      }
      agg.classList.remove("active");
    }
    // Blink the tick whenever files_done advances — gives a visible heartbeat
    // even when the percentage bar moves slowly.
    if (done > lastFilesDoneSum) flashTick();
    lastFilesDoneSum = done;

    // --- global progress: cumulative indexed vs estimated total across all
    // ENABLED drives. Lets the user see "I've built 274 of ~28,143 files
    // total" regardless of which drive is currently active.
    const gFill = $("progress-global-fill");
    const gText = $("progress-global-text");
    const gDone = Number(s.total_files) || 0;
    const gTotal = Number(s.enabled_files_estimate) || 0;
    if (gFill && gText) {
      if (gTotal > 0) {
        const gPct = Math.min(100, (gDone / gTotal) * 100);
        gFill.style.width = gPct.toFixed(1) + "%";
        gText.textContent = `${gDone.toLocaleString()} / ~${gTotal.toLocaleString()} (${gPct.toFixed(1)}%)`;
      } else {
        gFill.style.width = "0";
        gText.textContent = gDone ? `${gDone.toLocaleString()} / (推定取得中)` : "—";
      }
    }

    // --- rows
    // Daemon row — DAEMON-ONLY status, independent of /api/stats.
    // Renders from _daemonState (Electron `sc query` poll, 5s). When the
    // poll hasn't completed yet, _daemonState is null and we leave the
    // row alone (will be filled on the next pollDaemon tick). Once we
    // have a value, this row is authoritative and never gets cleared by
    // showErr() — that's the v1.3 promise: "API down ≠ daemon unknown".
    if (_daemonState !== null) renderDaemonRow(_daemonState);

    // Build row — explicit text label for indexing state. /api/stats only.
    //   error/dead daemon → 障害
    //   paused=true       → 停止中（手動停止）
    //   activeWorkers > 0 → 実行中 (N workers)
    //   else              → アイドル
    const buildEl = $("build-status");
    if (daemonLooksDead || anyErr) {
      buildEl.textContent = "✕ 障害 (詳細はログ)";
      buildEl.className = "v err";
    } else if (s.paused) {
      buildEl.textContent = "⏸ 停止中（手動停止）";
      buildEl.className = "v muted";
    } else if (activeWorkers.length > 0) {
      buildEl.textContent = `⚙ 実行中 (${activeWorkers.length} workers)`;
      buildEl.className = "v ok";
    } else {
      buildEl.textContent = "💤 アイドル";
      buildEl.className = "v";
    }

    const pgVal = s.pg_ok ? "PG" : "PG ×";
    const drvVal = s.drive_ok ? "Drive" : "Drive ×";
    const connsEl = $("conns");
    connsEl.textContent = `${pgVal} / ${drvVal}`;
    connsEl.className = "v " + (s.pg_ok && s.drive_ok ? "ok" : "err");

    $("fds").textContent = `${s.enabled_fds ?? 0}/${s.total_fds ?? 0}`;
    $("files").textContent = (s.total_files ?? 0).toLocaleString();
    $("chunks").textContent = (s.total_chunks ?? 0).toLocaleString();
    $("dbsize").textContent = fmtBytes(s.db_size_bytes);
    const summary =
      daemonLooksDead ? `デーモン停止中 (stale ${allWorkers.length})`
      : staleActiveCount
        ? `${activeWorkers.length} + ${staleActiveCount} stale / ${w.live} (target ${w.target})`
        : `${activeWorkers.length} / ${w.live} (target ${w.target})`;
    $("wsummary").textContent = summary;

    // Always show `target` slots so the user sees "this is what you asked
    // for". Extant workers fill the first slots (sorted by worker_id);
    // remaining slots show as placeholders.
    const sortedWorkers = [...allWorkers].sort((a, b) => a.worker_id - b.worker_id);
    const slots = Math.max(w.target || 0, sortedWorkers.length, 1);
    const parts = [];
    for (let i = 0; i < slots; i++) {
      const x = sortedWorkers[i];
      if (!x) {
        const tip = escHtmlAttr(`Worker slot ${i + 1}\nstate: (未起動 — target ${w.target})`);
        parts.push(
          `<span class="w idle" title="${tip}">#${i + 1} (待機)</span>`
        );
        continue;
      }
      const stale = isWorkerStale(x, now);
      const cls = stale ? "stale" : x.state;
      const label = stale ? "stale" : x.state;
      const tipLines = [
        `Worker #${x.worker_id}`,
        `state: ${label}`,
        x.phase        ? `phase: ${x.phase}`        : null,
        x.drive_name   ? `drive: ${x.drive_name}`   : "drive: (none)",
        x.drive_id     ? `drive_id: ${x.drive_id}`  : null,
        x.current_file ? `file: ${x.current_file}`  : null,
        x.total_files  ? `progress: ${x.files_done}/${x.total_files}` : null,
        x.last_error   ? `error: ${String(x.last_error).slice(0,200)}` : null,
      ].filter(Boolean);
      const tip = escHtmlAttr(tipLines.join("\n"));
      parts.push(`<span class="w ${cls}" title="${tip}">#${x.worker_id} ${label}</span>`);
    }
    // Only rewrite #wdetail when the rendered HTML actually changed. Without
    // this, a 250ms poll interval writes innerHTML 4x/sec and the layout
    // briefly "blinks" each time the browser reparses + repaints the row.
    const wHtml = parts.join("");
    if (wHtml !== _lastWdetailHash) {
      _lastWdetailHash = wHtml;
      $("wdetail").innerHTML = wHtml;
    }
    if (isDebugOpen()) renderDebug();
  } catch (e) {
    consecutiveFails++;
    DBG.lastFailAt = new Date();
    DBG.lastFailReason = String(e && e.message || e);
    if (consecutiveFails >= 2) showErr();
    if (isDebugOpen()) renderDebug();
  } finally {
    _inflight = false;
  }
}

// -------------------- Debug overlay --------------------
function isDebugOpen() {
  const p = document.getElementById("debug-panel");
  return !!(p && p.classList.contains("open"));
}
function fmtTs(d) {
  if (!d) return "-";
  const pad = n => String(n).padStart(2, "0");
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}.${String(d.getMilliseconds()).padStart(3,"0")}`;
}
function ageSec(d) {
  if (!d) return "-";
  return ((Date.now() - d.getTime()) / 1000).toFixed(1) + "s ago";
}
function httpBadge(v) {
  if (v == null) return "-";
  const cls = v.startsWith("ERR") || /^[45]/.test(v) ? "derr"
           : v === "200" ? "dok" : "dwarn";
  return `<span class="${cls}">${v}</span>`;
}
function renderDebug() {
  const set = (id, html) => { const el = document.getElementById(id); if (el) el.innerHTML = html; };
  set("dbg-api", API);
  set("dbg-last-tick", fmtTs(DBG.lastTickAt));
  set("dbg-last-ok",   DBG.lastOkAt   ? `${fmtTs(DBG.lastOkAt)} <span class="dk">(${ageSec(DBG.lastOkAt)})</span>` : "-");
  set("dbg-last-fail", DBG.lastFailAt
    ? `<span class="derr">${fmtTs(DBG.lastFailAt)}</span> <span class="dk">(${ageSec(DBG.lastFailAt)})</span> ${DBG.lastFailReason ? '- ' + DBG.lastFailReason : ''}`
    : "-");
  const failCls = consecutiveFails >= 2 ? "derr" : consecutiveFails > 0 ? "dwarn" : "dok";
  set("dbg-fails", `<span class="${failCls}">${consecutiveFails}</span>`);
  set("dbg-stats-http", httpBadge(DBG.statsHttp));
  set("dbg-workers-http", httpBadge(DBG.workersHttp));
  const indCls = DBG.indicator === "err" ? "derr" : DBG.indicator === "active" ? "dwarn" : DBG.indicator === "ok" ? "dok" : "dk";
  set("dbg-indicator", `<span class="${indCls}">${DBG.indicator}</span>`);
  set("dbg-dead", DBG.daemonDead ? '<span class="derr">yes</span>' : '<span class="dok">no</span>');
  set("dbg-active-stale", `${DBG.activeCount} active / ${DBG.staleCount} stale`);
  const stats = DBG.statsRaw ? JSON.stringify(DBG.statsRaw, null, 2) : "(none)";
  const workers = DBG.workersRaw ? JSON.stringify(DBG.workersRaw, null, 2) : "(none)";
  document.getElementById("dbg-stats-raw").textContent = stats;
  document.getElementById("dbg-workers-raw").textContent = workers;
  const hb = DBG.workerHb.length
    ? DBG.workerHb.map(x => `#${x.id} ${x.state.padEnd(10)} ${x.age_sec==null?'(no hb)':x.age_sec+'s'}${x.stale?' STALE':''}`).join("\n")
    : "(no workers)";
  document.getElementById("dbg-hb").textContent = hb;
}
function toggleDebug(force) {
  const p = document.getElementById("debug-panel");
  if (!p) return;
  const want = force == null ? !p.classList.contains("open") : !!force;
  p.classList.toggle("open", want);
  if (want) renderDebug();
}

function showErr() {
  setIndicator("err");
  const connsEl = $("conns");
  connsEl.textContent = "API 接続失敗";
  connsEl.className = "v err";
  $("wsummary").textContent = "-";
  $("wdetail").innerHTML = "";
  _lastWdetailHash = "";
  $("progress-agg").classList.remove("active");
  $("progress-fill").style.width = "0";
  $("progress-text").textContent = "—";
  const gFill = $("progress-global-fill");
  if (gFill) gFill.style.width = "0";
  const gText = $("progress-global-text");
  if (gText) gText.textContent = "—";
  // Clear metric rows too — otherwise stale values remain visible next
  // to the "API 接続失敗" badge and look like live data.
  $("fds").textContent = "-/-";
  $("files").textContent = "-";
  $("chunks").textContent = "-";
  $("dbsize").textContent = "-";
  // v1.3: Daemon row is independent of /api/stats — populated by the
  // SCM poll (winsrv.daemonStatus). DO NOT clear it here. The whole
  // point of v1.3 is "API down ≠ daemon unknown". Build row is API-only,
  // so we mark it explicitly.
  const buildEl = $("build-status");
  buildEl.textContent = "? (API 不通)";
  buildEl.className = "v err";
  lastFilesDoneSum = 0;
}

window.addEventListener("DOMContentLoaded", async () => {
  try { API = await window.winsrv.getApiUrl(); } catch {}
  $("api-url").textContent = API.replace(/^https?:\/\//, "");
  $("open-btn").addEventListener("click", () => window.winsrv.openWebUi());

  // Pause/resume toggle. Initial label is set to ⏸ in HTML; the first
  // /api/stats response will flip it to ▶ if the service is actually paused.
  const pauseBtn = $("pause-btn");
  if (pauseBtn) {
    pauseBtn.addEventListener("click", () => togglePause());
  }

  // Always-on-top checkbox: reflects current BrowserWindow state on load,
  // flips it via IPC on change. Uses localStorage as an extra memory so the
  // user's preference survives an Electron restart even before main.js
  // asks for the persisted value.
  const aot = $("aot-toggle");
  if (aot) {
    try {
      const current = await window.winsrv.getAlwaysOnTop();
      const saved = localStorage.getItem("aot");
      // If we have a saved user preference, honor it over the window default.
      const desired = saved === null ? !!current : saved === "1";
      aot.checked = desired;
      if (desired !== current) window.winsrv.setAlwaysOnTop(desired);
    } catch {}
    aot.addEventListener("change", () => {
      window.winsrv.setAlwaysOnTop(aot.checked);
      try { localStorage.setItem("aot", aot.checked ? "1" : "0"); } catch {}
    });
  }

  // --- Debug overlay wiring
  const dbgToggle = document.getElementById("debug-toggle");
  const dbgClose = document.getElementById("debug-close");
  if (dbgToggle) dbgToggle.addEventListener("click", () => toggleDebug());
  if (dbgClose) dbgClose.addEventListener("click", () => toggleDebug(false));
  // Ctrl+Shift+D toggles, Esc closes. Ctrl+Shift+I opens native DevTools
  // (handled in main.js via a menu accelerator).
  window.addEventListener("keydown", (e) => {
    if (e.ctrlKey && e.shiftKey && (e.key === "D" || e.key === "d")) {
      e.preventDefault();
      toggleDebug();
    } else if (e.key === "Escape" && isDebugOpen()) {
      toggleDebug(false);
    }
  });

  tick();
  setInterval(tick, 250);

  // v1.3: separate Daemon SCM polling loop, 5s cadence. Independent of
  // /api/stats. First poll immediate so the daemon row populates as
  // soon as possible; subsequent polls every 5s.
  pollDaemon();
  setInterval(pollDaemon, 5000);
});
