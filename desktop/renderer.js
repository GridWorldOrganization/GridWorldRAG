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
  try {
    const r = await fetch(API + path, { signal: ctrl.signal });
    if (!r.ok) throw new Error(r.status);
    return await r.json();
  } finally {
    clearTimeout(t);
  }
}

let consecutiveFails = 0;
let lastFilesDoneSum = 0;
let _inflight = false;  // prevent overlapping fetches if a tick takes longer than 250ms
let _lastWdetailHash = null;  // gate #wdetail innerHTML writes so we don't flicker

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
  // kind: 'idle' | 'ok' | 'active' | 'err'
  const el = $("indicator");
  el.classList.remove("ok", "active", "err");
  if (kind !== "idle") el.classList.add(kind);
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
  try {
    const [s, w] = await Promise.all([
      fetchJSON("/api/stats").catch(() => null),
      fetchJSON("/api/workers").catch(() => null),
    ]);
    if (!s || !w) { consecutiveFails++; if (consecutiveFails >= 2) showErr(); return; }
    consecutiveFails = 0;

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

    const anyErr = allWorkers.some(x => x.state === "error") || s.pg_ok === false;
    if (daemonLooksDead) setIndicator("err");
    else if (anyErr)                 setIndicator("err");
    else if (activeWorkers.length > 0) setIndicator("active");
    else if (s.pg_ok)                setIndicator("ok");
    else                             setIndicator("idle");

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
      label.textContent = `${done.toLocaleString()} / ${total.toLocaleString()} files (${pct.toFixed(1)}%)`;
      agg.classList.add("active");
    } else {
      fill.style.width = "0";
      label.textContent = activeWorkers.length ? "リスト取得中…" : "アイドル";
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
  } catch (_) {
    consecutiveFails++;
    if (consecutiveFails >= 2) showErr();
  } finally {
    _inflight = false;
  }
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
}

window.addEventListener("DOMContentLoaded", async () => {
  try { API = await window.winsrv.getApiUrl(); } catch {}
  $("api-url").textContent = API.replace(/^https?:\/\//, "");
  $("open-btn").addEventListener("click", () => window.winsrv.openWebUi());
  tick();
  setInterval(tick, 250);
});
