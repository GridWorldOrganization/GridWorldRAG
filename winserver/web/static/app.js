// WinServerRAG — Web monitor frontend.
//
// Design notes:
//   - All DOM handlers are bound inside the init IIFE to guard against
//     `TypeError: Cannot read properties of null` when the HTML schema
//     and JS schema drift. `?.addEventListener` double-guards.
//   - Every polling fn has a per-fn `_inflight` guard and a `console.error`
//     on failure, so a slow/dead API doesn't cascade into duplicate in-flight
//     fetches and debug info is always in DevTools.
//   - Table re-renders are skipped when the payload hasn't changed (cheap
//     JSON.stringify hash) so text selection / focus aren't clobbered
//     every tick.

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => [...document.querySelectorAll(sel)];

// ---------------- utilities ----------------
async function getJSON(url, opts = {}) {
  const timeoutMs = opts.timeoutMs ?? 4000;
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const res = await fetch(url, { ...opts, signal: ctrl.signal });
    if (!res.ok) throw new Error(`${res.status} ${url}`);
    return await res.json();
  } finally {
    clearTimeout(t);
  }
}
async function postJSON(url, body) {
  return getJSON(url, {
    method: "POST",
    headers: body ? { "content-type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
}
async function del(url) { return getJSON(url, { method: "DELETE" }); }

// in-flight gate for polling fns
function withInflight(fn) {
  return async function guarded(...args) {
    if (fn._inflight) return;
    fn._inflight = true;
    try { return await fn(...args); }
    finally { fn._inflight = false; }
  };
}

function fmtBytes(n) {
  if (n == null) return "-";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0, v = Number(n);
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(v >= 10 ? 0 : 1)} ${units[i]}`;
}
function fmtTs(s) {
  if (!s) return "-";
  try {
    const d = new Date(s);
    return d.toLocaleString("ja-JP", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
  } catch { return s; }
}
function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;",
  }[c]));
}

// Cheap content-equality hash for diff-gated render.
function hashOf(payload) {
  try { return JSON.stringify(payload); } catch { return String(Math.random()); }
}

// ---------------- tabs ----------------
function setupTabs() {
  $$(".tab-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const tab = btn.getAttribute("data-tab");
      $$(".tab-btn").forEach((b) => b.classList.toggle("active", b === btn));
      $$(".tab-pane").forEach((p) =>
        p.classList.toggle("active", p.getAttribute("data-tab") === tab));
    });
  });
}

// ---------------- stats ----------------
let _lastPgOk = true;    // module-scoped so updateGlobalIndicator() can read

const refreshStats = withInflight(async function _refreshStatsImpl() {
  try {
    const s = await getJSON(`/api/stats`);
    _lastPgOk = !!s.pg_ok;
    const pg = $("#stat-pg");
    if (pg) {
      pg.textContent = "PG " + (s.pg_ok ? "OK" : "NG");
      pg.className = "badge " + (s.pg_ok ? "ok" : "err");
    }
    const drv = $("#stat-drive");
    if (drv) {
      drv.textContent = "Drive " + (s.drive_ok ? "OK" : "NG");
      drv.className = "badge " + (s.drive_ok ? "ok" : "err");
    }
    const set = (id, text) => { const el = $(id); if (el) el.textContent = text; };
    set("#stat-fds",    `FDs: ${s.enabled_fds ?? 0}/${s.total_fds ?? 0}`);
    set("#stat-files",  `Files: ${(s.total_files ?? 0).toLocaleString()}`);
    set("#stat-chunks", `Chunks: ${(s.total_chunks ?? 0).toLocaleString()}`);
    set("#stat-dbsize", `DB: ${fmtBytes(s.db_size_bytes)}`);
  } catch (e) {
    console.error("refreshStats failed:", e);
    _lastPgOk = false;
    const pg = $("#stat-pg");
    if (pg) { pg.textContent = "PG NG"; pg.className = "badge err"; }
  }
});

// ---------------- workers ----------------
const WEB_WORKER_STALE_MS = 120_000;

function isWorkerStale(w, now) {
  if (!w || !w.heartbeat_at) return false;
  return (now - new Date(w.heartbeat_at).getTime()) > WEB_WORKER_STALE_MS;
}

let _lastWorkersHash = null;

const refreshWorkers = withInflight(async function _refreshWorkersImpl() {
  let data;
  try {
    data = await getJSON(`/api/workers`);
  } catch (e) {
    console.error("refreshWorkers failed:", e);
    const body = $("#workers-body");
    if (body) body.innerHTML =
      `<div class="worker-placeholder err">ワーカー情報取得失敗: ${escapeHtml(e.message)}</div>`;
    return;
  }
  // Normalize to defend against mid-restart partial payloads.
  if (!data || !Array.isArray(data.workers)) {
    data = { target: 0, live: 0, workers: [] };
  }
  const setTarget = $("#scale-target"); if (setTarget) setTarget.textContent = data.target;
  const setLive   = $("#scale-live");   if (setLive)   setLive.textContent   = data.live;
  // Skip re-render when nothing changed (preserves text selection / focus).
  const h = hashOf(data);
  if (h !== _lastWorkersHash) {
    _lastWorkersHash = h;
    renderWorkers(data);
  }
  updateGlobalIndicator(data);  // indicator depends on heartbeat freshness → always
});

function renderWorkers(data) {
  const body = $("#workers-body");
  if (!body) return;
  const target = Number(data.target) || 0;
  const byId = new Map(data.workers.map((w) => [w.worker_id, w]));
  const ids = data.workers.map((w) => w.worker_id).sort((a, b) => a - b);
  const slots = Math.max(target, ids.length);
  const html = [];
  for (let i = 0; i < slots; i++) {
    const w = ids[i] != null ? byId.get(ids[i]) : null;
    html.push(renderWorkerCard(i + 1, w));
  }
  body.innerHTML = html.join("");
}

function updateGlobalIndicator(workersPayload) {
  const el = $("#global-indicator");
  if (!el) return;
  const now = Date.now();
  const all = (workersPayload && workersPayload.workers) || [];
  const activeStates = new Set(["building", "syncing", "listing", "claiming"]);
  const freshActive = all.some((w) => activeStates.has(w.state) && !isWorkerStale(w, now));
  const allStale = all.length > 0 && all.every((w) => isWorkerStale(w, now));
  el.classList.remove("ok", "active", "err");
  if (allStale || !_lastPgOk) el.classList.add("err");
  else if (freshActive)       el.classList.add("active");
  else if (_lastPgOk)         el.classList.add("ok");
}

function renderWorkerCard(slotNum, w) {
  if (!w) {
    return `
      <div class="worker-card idle">
        <div class="worker-id">#${slotNum}</div>
        <div class="worker-drive muted">—</div>
        <div class="worker-state muted">(未起動)</div>
      </div>`;
  }
  const state = (w.state || "idle").toLowerCase();
  const done = Number(w.files_done) || 0;
  const total = Number(w.total_files) || 0;
  const pct = total > 0 ? Math.round((done / total) * 100) : 0;
  const progressBar = total > 0
    ? `<div class="progress"><div class="progress-fill" style="width:${pct}%"></div></div>
       <div class="progress-text">${done}/${total} files (${pct}%)</div>`
    : "";
  return `
    <div class="worker-card ${state}">
      <div class="worker-header">
        <span class="worker-id">#${w.worker_id}</span>
        <span class="wstate ${state}">${state}</span>
      </div>
      <div class="worker-drive">${w.drive_name ? '▶ ' + escapeHtml(w.drive_name) : '<span class="muted">(担当ドライブなし)</span>'}</div>
      ${w.phase ? `<div class="worker-phase muted">${escapeHtml(w.phase)}</div>` : ""}
      <div class="worker-file">${w.current_file ? escapeHtml(w.current_file) : ""}</div>
      ${progressBar}
      ${w.last_error ? `<div class="worker-err">${escapeHtml(String(w.last_error).slice(0, 200))}</div>` : ""}
    </div>`;
}

// ---------------- FD tables ----------------
let _lastFdsHash = null;

const refreshFDs = withInflight(async function _refreshFDsImpl() {
  let rows;
  try {
    rows = await getJSON(`/api/fds`);
  } catch (e) {
    console.error("refreshFDs failed:", e);
    const body = $("#fds-build-body");
    if (body) body.innerHTML =
      `<tr><td colspan="8" class="empty">読み込み失敗: ${escapeHtml(e.message)}</td></tr>`;
    return;
  }
  if (!Array.isArray(rows)) rows = [];
  const h = hashOf(rows);
  if (h === _lastFdsHash) return;
  _lastFdsHash = h;
  renderBuildTable(rows);
});

function renderBuildTable(rows) {
  const tbody = $("#fds-build-body");
  if (!tbody) return;
  if (!rows.length) {
    tbody.innerHTML = `<tr><td colspan="8" class="empty">
      共有ドライブが登録されていません。上の「共有ドライブを取得」を押してください。
    </td></tr>`;
    return;
  }
  tbody.innerHTML = rows.map((r) => {
    const state = (r.state || "idle").toLowerCase();
    const enabledClass = r.enabled ? "enabled" : "";
    const runningClass = r.running ? "running" : "";
    return `
      <tr class="row-toggle ${runningClass} ${enabledClass}" tabindex="0"
          data-id="${escapeHtml(r.drive_id)}" data-enabled="${r.enabled ? 1 : 0}">
        <td class="col-toggle"><input type="checkbox" class="toggle" ${r.enabled ? "checked" : ""} data-action="toggle-build" aria-label="ビルド ON/OFF"></td>
        <td title="${escapeHtml(r.drive_id)}">${escapeHtml(r.name || "(no name)")}</td>
        <td><span class="state ${state}">${state}${r.running ? " ●" : ""}</span></td>
        <td>${(Number(r.file_count) || 0).toLocaleString()}</td>
        <td>${(Number(r.chunk_count) || 0).toLocaleString()}</td>
        <td>${fmtTs(r.last_sync_at)}</td>
        <td>${fmtTs(r.last_build_at)}</td>
        <td class="actions">
          <button data-action="rebuild" class="destructive" title="全データを捨てて 0 から作り直します。時間がかかります。">再構築（初期化）</button>
          <span class="action-sep"></span>
          <button data-action="delete" class="danger" title="このドライブを登録から外し DB も削除します">削除</button>
        </td>
      </tr>
    `;
  }).join("");
  attachBuildRowHandlers();
}

function attachBuildRowHandlers() {
  $$("#fds-build-body tr.row-toggle").forEach((tr) => {
    const id = tr.getAttribute("data-id");
    const toggleRowState = async () => {
      const on = tr.getAttribute("data-enabled") !== "1";
      await toggleBuild(id, on);
    };

    // mouse click (but not on actions / toggle)
    tr.addEventListener("click", async (ev) => {
      if (ev.target.closest(".actions")) return;
      if (ev.target.classList.contains("toggle")) return;
      await toggleRowState();
    });
    // keyboard: Enter / Space
    tr.addEventListener("keydown", async (ev) => {
      if (ev.key === "Enter" || ev.key === " ") {
        if (ev.target.closest(".actions")) return;
        if (ev.target.classList.contains("toggle")) return;
        ev.preventDefault();
        await toggleRowState();
      }
    });

    const toggle = tr.querySelector("input.toggle");
    if (toggle) {
      toggle.addEventListener("click", async (ev) => {
        ev.stopPropagation();
        await toggleBuild(id, toggle.checked);
      });
    }

    tr.querySelectorAll(".actions [data-action]").forEach((el) => {
      el.addEventListener("click", async (ev) => {
        ev.stopPropagation();
        const act = el.getAttribute("data-action");
        const name = tr.querySelector("td:nth-child(2)").textContent.trim();
        try {
          if (act === "rebuild") {
            if (!confirmRebuild(name)) return;
            await postJSON(`/api/fds/${encodeURIComponent(id)}/rebuild`);
          } else if (act === "delete") {
            if (!confirm(`「${name}」を登録から外し、DB データも削除します。よろしいですか？`)) return;
            await del(`/api/fds/${encodeURIComponent(id)}`);
          }
          _lastFdsHash = null; // force re-render even if cached
          await refreshFDs();
          await refreshStats();
        } catch (e) {
          alert(`失敗: ${e.message}`);
          _lastFdsHash = null;
          await refreshFDs();
        }
      });
    });
  });
}

async function toggleBuild(id, on) {
  try {
    await postJSON(`/api/fds/${encodeURIComponent(id)}/${on ? "enable" : "disable"}`);
  } catch (e) {
    console.error("toggleBuild failed:", e);
    alert(`失敗: ${e.message}`);
  }
  _lastFdsHash = null;
  await refreshFDs();
  await refreshStats();
}

function confirmRebuild(name) {
  const msg =
    `【再構築（初期化）】\n\n` +
    `共有ドライブ:\n  「${name}」\n\n` +
    `既存のチャンクデータは全て破棄し、Google Drive から 0 から再取得します。\n` +
    `完了まで時間がかかります（ファイル数により数分〜数時間）。\n\n` +
    `本当に実行しますか？`;
  return confirm(msg);
}

// --- MCP global search scope ---
let _lastScopeHash = null;

const refreshMcpScope = withInflight(async function _refreshMcpScopeImpl() {
  const tbody = $("#mcp-scope-body");
  if (!tbody) return;
  let rows;
  try {
    rows = await getJSON(`/api/fds`);
  } catch (e) {
    console.error("refreshMcpScope failed:", e);
    tbody.innerHTML = `<tr><td colspan="6" class="empty">取得失敗: ${escapeHtml(e.message)}</td></tr>`;
    return;
  }
  if (!Array.isArray(rows)) rows = [];
  if (!rows.length) {
    tbody.innerHTML = `<tr><td colspan="6" class="empty">ドライブが未登録</td></tr>`;
    return;
  }
  const h = hashOf(rows);
  if (h === _lastScopeHash) return;
  _lastScopeHash = h;
  tbody.innerHTML = rows.map((r) => {
    const state = (r.state || "idle").toLowerCase();
    const built = r.last_build_at != null || (r.chunk_count ?? 0) > 0;
    const hint = built ? "" : "（未ビルド）";
    const cls = r.search_enabled ? "search-on" : "";
    return `
      <tr class="row-search ${cls}" tabindex="0"
          data-id="${escapeHtml(r.drive_id)}" data-search="${r.search_enabled ? 1 : 0}">
        <td class="col-toggle"><input type="checkbox" class="toggle search-toggle" ${r.search_enabled ? "checked" : ""} aria-label="検索スコープ ON/OFF"></td>
        <td title="${escapeHtml(r.drive_id)}">
          ${escapeHtml(r.name || "(no name)")}
          ${hint ? `<span class="muted small"> ${hint}</span>` : ""}
        </td>
        <td><span class="state ${state}">${state}</span></td>
        <td>${(Number(r.file_count) || 0).toLocaleString()}</td>
        <td>${(Number(r.chunk_count) || 0).toLocaleString()}</td>
        <td>${fmtTs(r.last_build_at)}</td>
      </tr>
    `;
  }).join("");
  attachScopeHandlers();
});

function attachScopeHandlers() {
  $$("#mcp-scope-body tr.row-search").forEach((tr) => {
    const id = tr.getAttribute("data-id");
    const toggle = tr.querySelector("input.search-toggle");
    const flip = async () => {
      const on = tr.getAttribute("data-search") !== "1";
      await toggleScope(id, on);
    };
    tr.addEventListener("click", async (ev) => {
      if (ev.target === toggle) return;
      await flip();
    });
    tr.addEventListener("keydown", async (ev) => {
      if (ev.key === "Enter" || ev.key === " ") {
        if (ev.target === toggle) return;
        ev.preventDefault();
        await flip();
      }
    });
    toggle.addEventListener("click", async (ev) => {
      ev.stopPropagation();
      await toggleScope(id, toggle.checked);
    });
  });
}

async function toggleScope(driveId, on) {
  try {
    await postJSON(`/api/fds/${encodeURIComponent(driveId)}/${on ? "search-enable" : "search-disable"}`);
  } catch (e) {
    console.error("toggleScope failed:", e);
    alert(`失敗: ${e.message}`);
  }
  _lastScopeHash = null;
  await refreshMcpScope();
}

// --- MCP query log ---
let _lastQueryLogHash = null;

const refreshMcpQueryLog = withInflight(async function _refreshMcpQueryLogImpl() {
  const el = $("#mcp-query-log");
  if (!el) return;
  let rows;
  try {
    rows = await getJSON(`/api/mcp/query-log?limit=50`);
  } catch (e) {
    console.error("refreshMcpQueryLog failed:", e);
    el.innerHTML = `<span class="err">取得失敗: ${escapeHtml(e.message)}</span>`;
    return;
  }
  if (!Array.isArray(rows)) rows = [];
  if (!rows.length) {
    el.innerHTML = '<span class="muted">まだクエリはありません</span>';
    return;
  }
  const h = hashOf(rows);
  if (h === _lastQueryLogHash) return;
  _lastQueryLogHash = h;
  el.innerHTML = rows.map((r) => {
    const ts = r.ts ? new Date(r.ts).toLocaleString("ja-JP", { hour12: false }) : "";
    const latency = r.latency_ms != null ? `${r.latency_ms}ms` : "-";
    const q = r.query ? escapeHtml(r.query) : `<span class="muted">(${escapeHtml(r.tool_name || "?")})</span>`;
    const err = r.error ? ` <span class="evt-err">× ${escapeHtml(String(r.error).slice(0, 80))}</span>` : "";
    return `<span class="evt">
      <span class="ts">${ts}</span>
      <span class="lvl info">${escapeHtml(r.tool_name || "?")}</span>
      <span class="fd">[${escapeHtml(r.username || "?")}]</span>
      <span class="msg">${q}</span>
      <span class="muted">${r.returned_count ?? "-"} hits • ${latency}${err}</span>
    </span>`;
  }).join("");
});

// ---------------- Eval score ----------------
const refreshEval = withInflight(async function _refreshEvalImpl() {
  const scoreEl = $("#eval-score");
  const detailEl = $("#eval-detail");
  const noteEl = $("#eval-note");
  if (!scoreEl) return;
  try {
    const r = await getJSON(`/api/eval`);
    if (r.score == null) {
      scoreEl.textContent = "—";
      if (noteEl) noteEl.textContent = r.note || "";
      if (detailEl) detailEl.textContent = "";
    } else {
      const pct = Math.round(r.score * 100);
      scoreEl.textContent = `${pct}%`;
      scoreEl.className = pct >= 80 ? "ok" : pct >= 50 ? "warn" : "err";
      if (detailEl) detailEl.textContent = `(${r.passed}/${r.total} passed${r.reranked ? ", reranked" : ""})`;
      if (noteEl) noteEl.textContent = "";
    }
  } catch (e) {
    console.error("refreshEval failed:", e);
    scoreEl.textContent = "?";
  }
});

// ---------------- MCP users ----------------
let _lastUsersHash = null;

const refreshMcpUsers = withInflight(async function _refreshMcpUsersImpl() {
  const tbody = $("#mcp-users-body");
  if (!tbody) return;
  let users;
  try {
    users = await getJSON(`/api/mcp/users`);
  } catch (e) {
    console.error("refreshMcpUsers failed:", e);
    tbody.innerHTML = `<tr><td colspan="4" class="empty">取得失敗: ${escapeHtml(e.message)}</td></tr>`;
    return;
  }
  if (!Array.isArray(users)) users = [];
  if (!users.length) {
    tbody.innerHTML = `<tr><td colspan="4" class="empty">ユーザー未登録</td></tr>`;
    _lastUsersHash = null;
    return;
  }
  const h = hashOf(users);
  if (h === _lastUsersHash) return;
  _lastUsersHash = h;
  tbody.innerHTML = users.map((u) => `
    <tr data-username="${escapeHtml(u.username)}">
      <td><strong>${escapeHtml(u.username)}</strong></td>
      <td>${fmtTs(u.created_at)}</td>
      <td>${fmtTs(u.updated_at)}</td>
      <td class="actions">
        <button data-action="change-pw">パスワード変更</button>
        <button data-action="delete-user" class="danger">削除</button>
      </td>
    </tr>
  `).join("");
  attachUserRowHandlers();
});

function attachUserRowHandlers() {
  $$("#mcp-users-body tr[data-username]").forEach((tr) => {
    const username = tr.getAttribute("data-username");
    tr.querySelectorAll("[data-action]").forEach((btn) => {
      btn.addEventListener("click", async (ev) => {
        ev.stopPropagation();
        const act = btn.getAttribute("data-action");
        try {
          if (act === "change-pw") {
            const pw = prompt(`「${username}」の新しいパスワード (4 文字以上)`);
            if (pw == null || pw === "") return;
            const res = await fetch(`/api/mcp/users/${encodeURIComponent(username)}/password`, {
              method: "PUT",
              headers: { "content-type": "application/json" },
              body: JSON.stringify({ password: pw }),
            });
            if (!res.ok) {
              const txt = await res.text().catch(() => "");
              throw new Error(`${res.status} ${txt}`);
            }
            alert(`${username} のパスワードを変更しました`);
          } else if (act === "delete-user") {
            if (!confirm(`MCP ユーザー「${username}」を削除しますか？`)) return;
            await del(`/api/mcp/users/${encodeURIComponent(username)}`);
          }
          _lastUsersHash = null;
          await refreshMcpUsers();
        } catch (e) {
          console.error("user action failed:", e);
          alert(`失敗: ${e.message}`);
        }
      });
    });
  });
}

// ---------------- events stream ----------------
const eventsEl = () => $("#events");

function appendEvent(e) {
  const el = eventsEl();
  if (!el) return;
  const ts = e.ts ? new Date(e.ts).toLocaleTimeString("ja-JP", { hour12: false }) : "";
  const fd = e.drive_id ? e.drive_id.substring(0, 8) : "*";
  const div = document.createElement("span");
  div.className = "evt";
  div.innerHTML = `
    <span class="ts">${ts}</span>
    <span class="lvl ${escapeHtml(e.level || "info")}">${escapeHtml(e.level || "?")}</span>
    <span class="fd">[${escapeHtml(fd)}]</span>
    <span class="ev">${escapeHtml(e.event || "")}</span>
    <span class="msg">${escapeHtml(e.message || "")}</span>
  `;
  el.appendChild(div);
  while (el.children.length > 500) el.removeChild(el.firstChild);
  el.scrollTop = el.scrollHeight;
}

async function loadInitialEvents() {
  try {
    const evs = await getJSON(`/api/events?limit=200`);
    const el = eventsEl();
    if (el) el.innerHTML = "";
    for (const e of evs) appendEvent(e);
  } catch (e) {
    console.warn("loadInitialEvents failed:", e);
  }
}

// WebSocket with exponential backoff + de-dup via single-instance guard.
let _ws = null;
let _wsBackoffMs = 2000;   // starts at 2s, grows to 60s on repeated failure
const _WS_MAX_BACKOFF_MS = 60_000;

function connectWS() {
  if (_ws && (_ws.readyState === 0 || _ws.readyState === 1)) return;  // connecting/open
  const proto = location.protocol === "https:" ? "wss" : "ws";
  try {
    _ws = new WebSocket(`${proto}://${location.host}/ws/events`);
  } catch (e) {
    console.warn("ws ctor failed:", e);
    scheduleWsReconnect();
    return;
  }
  _ws.onopen = () => { _wsBackoffMs = 2000; };  // reset backoff on success
  _ws.onmessage = (m) => {
    try { appendEvent(JSON.parse(m.data)); } catch (e) { console.warn("ws msg parse:", e); }
  };
  _ws.onclose = () => scheduleWsReconnect();
  _ws.onerror = () => { try { _ws.close(); } catch {} };
}

function scheduleWsReconnect() {
  setTimeout(connectWS, _wsBackoffMs);
  _wsBackoffMs = Math.min(_wsBackoffMs * 2, _WS_MAX_BACKOFF_MS);
}

// ---------------- auto-refresh interval (slider) ----------------
const REFRESH_STORAGE_KEY = "winserverrag.refreshIntervalMs";
const DEFAULT_REFRESH_MS = 2000;
const REFRESH_MIN_MS = 100;
const REFRESH_MAX_MS = 20000;
let _refreshTimers = [];

function formatInterval(ms) {
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

function clearRefreshTimers() {
  for (const t of _refreshTimers) clearInterval(t);
  _refreshTimers = [];
}

function startRefreshTimers(ms) {
  clearRefreshTimers();
  _refreshTimers.push(setInterval(() => refreshFDs().catch(() => {}),     ms));
  _refreshTimers.push(setInterval(() => refreshStats().catch(() => {}),   ms));
  _refreshTimers.push(setInterval(() => refreshWorkers().catch(() => {}), ms));
}

function applyInterval(ms) {
  ms = Math.max(REFRESH_MIN_MS, Math.min(REFRESH_MAX_MS, Math.round(ms / 100) * 100));
  const disp = $("#interval-display"); if (disp) disp.textContent = formatInterval(ms);
  const sl = $("#interval-slider"); if (sl) sl.value = ms;
  try { localStorage.setItem(REFRESH_STORAGE_KEY, String(ms)); } catch {}
  startRefreshTimers(ms);
}

function setupIntervalSlider() {
  const initial = (() => {
    try {
      const v = parseInt(localStorage.getItem(REFRESH_STORAGE_KEY) || "", 10);
      if (Number.isFinite(v) && v >= REFRESH_MIN_MS && v <= REFRESH_MAX_MS) return v;
    } catch {}
    return DEFAULT_REFRESH_MS;
  })();
  applyInterval(initial);
  $("#interval-slider")?.addEventListener("input", (ev) => {
    applyInterval(parseInt(ev.target.value, 10));
  });
}

// ---------------- init (single IIFE — all DOM binding happens here) ----------------
(async function init() {
  try {
    setupTabs();
    setupIntervalSlider();

    // Top-bar buttons (guarded against missing elements)
    $("#btn-refresh")?.addEventListener("click", async () => {
      _lastFdsHash = null; _lastWorkersHash = null;
      await Promise.allSettled([refreshFDs(), refreshStats(), refreshWorkers()]);
    });
    $("#btn-discover")?.addEventListener("click", async () => {
      const btn = $("#btn-discover");
      if (btn) btn.disabled = true;
      try {
        await getJSON(`/api/drives/available`);
        _lastFdsHash = null;
        await refreshFDs();
        await refreshStats();
      } catch (e) {
        console.error("discover failed:", e);
        alert(`取得失敗: ${e.message}`);
      } finally {
        if (btn) btn.disabled = false;
      }
    });
    $("#btn-add-mcp-user")?.addEventListener("click", async () => {
      const username = prompt("新規 MCP ユーザー名 (英数 . _ -、最大 64 文字)");
      if (!username) return;
      const pw = prompt(`「${username}」のパスワード (4 文字以上)`);
      if (!pw) return;
      try {
        await postJSON(`/api/mcp/users`, { username, password: pw });
        _lastUsersHash = null;
        await refreshMcpUsers();
      } catch (e) {
        console.error("add user failed:", e);
        alert(`追加失敗: ${e.message}`);
      }
    });

    // Refresh MCP widgets when switching to the MCP tab
    $$(".tab-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        if (btn.getAttribute("data-tab") === "mcp") {
          refreshMcpUsers().catch(() => {});
          refreshMcpScope().catch(() => {});
          refreshMcpQueryLog().catch(() => {});
        }
      });
    });

    // Kick off initial refreshes in parallel (none block the others).
    loadInitialEvents().catch((e) => console.warn("loadInitialEvents:", e));
    refreshFDs().catch(() => {});
    refreshStats().catch(() => {});
    refreshWorkers().catch(() => {});
    refreshMcpUsers().catch(() => {});
    refreshMcpScope().catch(() => {});
    refreshMcpQueryLog().catch(() => {});
    refreshEval().catch(() => {});
    connectWS();
    // Periodic refresh timers are started inside setupIntervalSlider().
  } catch (e) {
    // Last-resort: surface init errors so user sees them (otherwise silent).
    console.error("init failed:", e);
    document.body.insertAdjacentHTML("afterbegin",
      `<div style="background:#33181c;color:#ff6b6b;padding:10px;font-family:monospace;">
        init failed: ${escapeHtml(e && e.message ? e.message : String(e))}
      </div>`);
  }
})();
