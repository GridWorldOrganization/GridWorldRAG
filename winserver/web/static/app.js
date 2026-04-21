const $ = (sel) => document.querySelector(sel);
const API = "";  // same origin

async function getJSON(url, opts = {}) {
  const res = await fetch(url, opts);
  if (!res.ok) throw new Error(`${res.status} ${url}`);
  return res.json();
}
async function postJSON(url) {
  return getJSON(url, { method: "POST" });
}
async function del(url) {
  return getJSON(url, { method: "DELETE" });
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

async function refreshFDs() {
  const tbody = $("#fds-body");
  try {
    const rows = await getJSON(`${API}/api/fds`);
    if (!rows.length) {
      tbody.innerHTML = `<tr><td colspan="8" class="empty">
        共有ドライブが登録されていません。右上の「共有ドライブを取得」を押してください。
      </td></tr>`;
      return;
    }
    tbody.innerHTML = rows.map((r) => renderRow(r)).join("");
    attachRowHandlers();
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="8" class="empty">読み込み失敗: ${e.message}</td></tr>`;
  }
}

function renderRow(r) {
  const state = (r.state || "idle").toLowerCase();
  const running = r.running ? "running" : "";
  return `
    <tr class="${running}" data-id="${r.drive_id}">
      <td><input type="checkbox" class="toggle" ${r.enabled ? "checked" : ""} data-action="toggle"></td>
      <td title="${escapeHtml(r.drive_id)}">${escapeHtml(r.name || "(no name)")}</td>
      <td><span class="state ${state}">${state}${r.running ? " ●" : ""}</span></td>
      <td>${(r.file_count ?? 0).toLocaleString()}</td>
      <td>${(r.chunk_count ?? 0).toLocaleString()}</td>
      <td>${fmtTs(r.last_sync_at)}</td>
      <td>${fmtTs(r.last_build_at)}</td>
      <td class="actions">
        <button data-action="sync">同期</button>
        <button data-action="rebuild">再構築</button>
        <button data-action="delete" class="danger">削除</button>
      </td>
    </tr>
  `;
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;",
  }[c]));
}

function attachRowHandlers() {
  document.querySelectorAll("#fds-body tr").forEach((tr) => {
    const id = tr.getAttribute("data-id");
    tr.querySelectorAll("[data-action]").forEach((el) => {
      el.addEventListener("click", async (ev) => {
        const act = el.getAttribute("data-action");
        try {
          if (act === "toggle") {
            const on = el.checked;
            await postJSON(`${API}/api/fds/${id}/${on ? "enable" : "disable"}`);
          } else if (act === "sync") {
            await postJSON(`${API}/api/fds/${id}/sync-now`);
          } else if (act === "rebuild") {
            if (!confirm("全量再構築しますか？既存データは破棄されます。")) return;
            await postJSON(`${API}/api/fds/${id}/rebuild`);
          } else if (act === "delete") {
            if (!confirm("このドライブを削除しますか？DB データも削除されます。")) return;
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

$("#btn-refresh").addEventListener("click", async () => {
  await Promise.all([refreshFDs(), refreshStats()]);
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

// --- Events stream (WS if available, poll otherwise) ---
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
  ws.onmessage = (m) => {
    try { appendEvent(JSON.parse(m.data)); } catch {}
  };
  ws.onclose = () => setTimeout(connectWS, 3000);
  ws.onerror = () => ws.close();
}

(async function init() {
  await loadInitialEvents();
  await refreshFDs();
  await refreshStats();
  connectWS();
  setInterval(refreshFDs, 5000);
  setInterval(refreshStats, 5000);
})();
