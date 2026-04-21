const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => [...document.querySelectorAll(sel)];
const API = "";

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
async function refreshStats() {
  try {
    const s = await getJSON(`${API}/api/stats`);
    const pg = $("#stat-pg");
    pg.textContent = "PG " + (s.pg_ok ? "OK" : "NG");
    pg.className = "badge " + (s.pg_ok ? "ok" : "err");
    const drv = $("#stat-drive");
    drv.textContent = "Drive " + (s.drive_ok ? "OK" : "NG");
    drv.className = "badge " + (s.drive_ok ? "ok" : "err");
    $("#stat-fds").textContent    = `FDs: ${s.enabled_fds ?? 0}/${s.total_fds ?? 0}`;
    $("#stat-files").textContent  = `Files: ${(s.total_files ?? 0).toLocaleString()}`;
    $("#stat-chunks").textContent = `Chunks: ${(s.total_chunks ?? 0).toLocaleString()}`;
    $("#stat-dbsize").textContent = `DB: ${fmtBytes(s.db_size_bytes)}`;
  } catch (e) {
    $("#stat-pg").textContent = "PG NG";
    $("#stat-pg").className = "badge err";
  }
}

// ---------------- workers ----------------
let _lastTarget = null;

async function refreshWorkers() {
  try {
    const data = await getJSON(`${API}/api/workers`);
    _lastTarget = data.target;
    $("#scale-target").textContent = data.target;
    $("#scale-live").textContent = data.live;
    renderWorkers(data);
  } catch (e) {
    $("#workers-body").innerHTML =
      `<div class="worker-placeholder err">ワーカー情報取得失敗: ${escapeHtml(e.message)}</div>`;
  }
}

function renderWorkers(data) {
  const target = data.target;
  const byId = new Map(data.workers.map((w) => [w.worker_id, w]));
  const ids = data.workers.map((w) => w.worker_id).sort((a, b) => a - b);
  // Show at least `target` slots, even if some haven't heartbeat yet.
  const slots = Math.max(target, ids.length);
  const html = [];
  for (let i = 0; i < slots; i++) {
    const w = ids[i] != null ? byId.get(ids[i]) : null;
    html.push(renderWorkerCard(i + 1, w));
  }
  $("#workers-body").innerHTML = html.join("");
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
  const pct = w.total_files > 0
    ? Math.round((w.files_done / w.total_files) * 100)
    : 0;
  const progressBar = w.total_files > 0
    ? `<div class="progress"><div class="progress-fill" style="width:${pct}%"></div></div>
       <div class="progress-text">${w.files_done}/${w.total_files} files (${pct}%)</div>`
    : "";
  // Drive name is the prominent identifier of "what this worker is doing"
  // — promote it to the top of the card, big and clearly legible.
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
      ${w.last_error ? `<div class="worker-err">${escapeHtml(w.last_error.slice(0, 200))}</div>` : ""}
    </div>`;
}

async function scaleBy(delta) {
  if (_lastTarget == null) return;
  const next = Math.max(1, Math.min(10, _lastTarget + delta));
  if (next === _lastTarget) return;
  try {
    await postJSON(`${API}/api/workers/scale`, { n: next });
  } catch (e) {
    alert(`scale failed: ${e.message}`);
  }
  await refreshWorkers();
}

// ---------------- FD tables (build + mcp) ----------------
async function refreshFDs() {
  let rows;
  try {
    rows = await getJSON(`${API}/api/fds`);
  } catch (e) {
    $("#fds-build-body").innerHTML =
      `<tr><td colspan="8" class="empty">読み込み失敗: ${escapeHtml(e.message)}</td></tr>`;
    $("#fds-mcp-body").innerHTML =
      `<tr><td colspan="6" class="empty">読み込み失敗: ${escapeHtml(e.message)}</td></tr>`;
    return;
  }
  renderBuildTable(rows);
  renderMcpTable(rows);
}

// --- BUILD table
function renderBuildTable(rows) {
  const tbody = $("#fds-build-body");
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
      <tr class="row-toggle ${runningClass} ${enabledClass}" data-id="${r.drive_id}" data-enabled="${r.enabled ? 1 : 0}">
        <td class="col-toggle"><input type="checkbox" class="toggle" ${r.enabled ? "checked" : ""} data-action="toggle-build"></td>
        <td title="${escapeHtml(r.drive_id)}">${escapeHtml(r.name || "(no name)")}</td>
        <td><span class="state ${state}">${state}${r.running ? " ●" : ""}</span></td>
        <td>${(r.file_count ?? 0).toLocaleString()}</td>
        <td>${(r.chunk_count ?? 0).toLocaleString()}</td>
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

    tr.addEventListener("click", async (ev) => {
      if (ev.target.closest(".actions")) return;
      if (ev.target.classList.contains("toggle")) return;
      const on = tr.getAttribute("data-enabled") !== "1";
      await toggleBuild(id, on);
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
          if (act === "sync") {
            await postJSON(`${API}/api/fds/${id}/sync-now`);
          } else if (act === "rebuild") {
            if (!confirmRebuild(name)) return;
            await postJSON(`${API}/api/fds/${id}/rebuild`);
          } else if (act === "delete") {
            if (!confirm(`「${name}」を登録から外し、DB データも削除します。よろしいですか？`)) return;
            await del(`${API}/api/fds/${id}`);
          }
          await refreshFDs();
          await refreshStats();
        } catch (e) {
          alert(`失敗: ${e.message}`);
          await refreshFDs();
        }
      });
    });
  });
}

async function toggleBuild(id, on) {
  try {
    await postJSON(`${API}/api/fds/${id}/${on ? "enable" : "disable"}`);
  } catch (e) {
    alert(`失敗: ${e.message}`);
  }
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

// --- MCP table
function renderMcpTable(rows) {
  const tbody = $("#fds-mcp-body");
  if (!rows.length) {
    tbody.innerHTML = `<tr><td colspan="6" class="empty">ドライブが未登録です。</td></tr>`;
    return;
  }
  tbody.innerHTML = rows.map((r) => {
    const state = (r.state || "idle").toLowerCase();
    const built = r.last_build_at != null || (r.chunk_count ?? 0) > 0;
    const disabledHint = built ? "" : "（未ビルド — 検索対象にしても結果なし）";
    const searchClass = r.search_enabled ? "search-on" : "";
    return `
      <tr class="row-search ${searchClass}" data-id="${r.drive_id}" data-search="${r.search_enabled ? 1 : 0}">
        <td class="col-toggle"><input type="checkbox" class="toggle search-toggle" ${r.search_enabled ? "checked" : ""}></td>
        <td title="${escapeHtml(r.drive_id)}">
          ${escapeHtml(r.name || "(no name)")}
          ${disabledHint ? `<span class="muted small"> ${disabledHint}</span>` : ""}
        </td>
        <td><span class="state ${state}">${state}</span></td>
        <td>${(r.file_count ?? 0).toLocaleString()}</td>
        <td>${(r.chunk_count ?? 0).toLocaleString()}</td>
        <td>${fmtTs(r.last_build_at)}</td>
      </tr>
    `;
  }).join("");
  attachMcpRowHandlers();
}

function attachMcpRowHandlers() {
  $$("#fds-mcp-body tr.row-search").forEach((tr) => {
    const id = tr.getAttribute("data-id");
    const toggle = tr.querySelector("input.search-toggle");

    tr.addEventListener("click", async (ev) => {
      if (ev.target === toggle) return;
      const on = tr.getAttribute("data-search") !== "1";
      await toggleSearch(id, on);
    });
    toggle.addEventListener("click", async (ev) => {
      ev.stopPropagation();
      await toggleSearch(id, toggle.checked);
    });
  });
}

async function toggleSearch(id, on) {
  try {
    await postJSON(`${API}/api/fds/${id}/${on ? "search-enable" : "search-disable"}`);
  } catch (e) {
    alert(`失敗: ${e.message}`);
  }
  await refreshFDs();
}

// ---------------- MCP users ----------------
async function refreshMcpUsers() {
  const tbody = $("#mcp-users-body");
  try {
    const users = await getJSON(`${API}/api/mcp/users`);
    if (!users.length) {
      tbody.innerHTML = `<tr><td colspan="4" class="empty">ユーザー未登録</td></tr>`;
      return;
    }
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
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="4" class="empty">取得失敗: ${escapeHtml(e.message)}</td></tr>`;
  }
}

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
            await fetch(`${API}/api/mcp/users/${encodeURIComponent(username)}/password`, {
              method: "PUT",
              headers: { "content-type": "application/json" },
              body: JSON.stringify({ password: pw }),
            }).then((r) => {
              if (!r.ok) return r.text().then((t) => { throw new Error(`${r.status} ${t}`); });
            });
            alert(`${username} のパスワードを変更しました`);
          } else if (act === "delete-user") {
            if (!confirm(`MCP ユーザー「${username}」を削除しますか？`)) return;
            await del(`${API}/api/mcp/users/${encodeURIComponent(username)}`);
          }
          await refreshMcpUsers();
        } catch (e) {
          alert(`失敗: ${e.message}`);
        }
      });
    });
  });
}

$("#btn-add-mcp-user").addEventListener("click", async () => {
  const username = prompt("新規 MCP ユーザー名 (英数 . _ -、最大 64 文字)");
  if (!username) return;
  const pw = prompt(`「${username}」のパスワード (4 文字以上)`);
  if (!pw) return;
  try {
    await postJSON(`${API}/api/mcp/users`, { username, password: pw });
    await refreshMcpUsers();
  } catch (e) {
    alert(`追加失敗: ${e.message}`);
  }
});

// ---------------- top controls ----------------
$("#btn-refresh").addEventListener("click", async () => {
  await Promise.all([refreshFDs(), refreshStats(), refreshWorkers()]);
});
$("#btn-discover").addEventListener("click", async () => {
  $("#btn-discover").disabled = true;
  try {
    await getJSON(`${API}/api/drives/available`);
    await refreshFDs();
    await refreshStats();
  } catch (e) {
    alert(`取得失敗: ${e.message}`);
  } finally {
    $("#btn-discover").disabled = false;
  }
});
$("#btn-scale-up").addEventListener("click", () => scaleBy(+1));
$("#btn-scale-down").addEventListener("click", () => scaleBy(-1));

// ---------------- events stream ----------------
const eventsEl = $("#events");
function appendEvent(e) {
  const ts = e.ts ? new Date(e.ts).toLocaleTimeString("ja-JP", { hour12: false }) : "";
  const fd = e.drive_id ? e.drive_id.substring(0, 8) : "*";
  const div = document.createElement("span");
  div.className = "evt";
  div.innerHTML = `
    <span class="ts">${ts}</span>
    <span class="lvl ${e.level}">${e.level}</span>
    <span class="fd">[${fd}]</span>
    <span class="ev">${escapeHtml(e.event)}</span>
    <span class="msg">${escapeHtml(e.message || "")}</span>
  `;
  eventsEl.appendChild(div);
  while (eventsEl.children.length > 500) eventsEl.removeChild(eventsEl.firstChild);
  eventsEl.scrollTop = eventsEl.scrollHeight;
}
async function loadInitialEvents() {
  try {
    const evs = await getJSON(`${API}/api/events?limit=200`);
    eventsEl.innerHTML = "";
    for (const e of evs) appendEvent(e);
  } catch {}
}
function connectWS() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/events`);
  ws.onmessage = (m) => { try { appendEvent(JSON.parse(m.data)); } catch {} };
  ws.onclose = () => setTimeout(connectWS, 3000);
  ws.onerror = () => ws.close();
}

// ---------------- init ----------------
(async function init() {
  setupTabs();
  // refresh MCP users when switching to the MCP tab
  $$(".tab-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      if (btn.getAttribute("data-tab") === "mcp") refreshMcpUsers();
    });
  });
  // Kick off refreshes in parallel — don't let a slow one block the others.
  loadInitialEvents().catch(() => {});
  refreshFDs().catch(() => {});
  refreshStats().catch(() => {});
  refreshWorkers().catch(() => {});
  refreshMcpUsers().catch(() => {});
  connectWS();
  setInterval(() => refreshFDs().catch(() => {}), 5000);
  setInterval(() => refreshStats().catch(() => {}), 5000);
  setInterval(() => refreshWorkers().catch(() => {}), 2000);
})();
