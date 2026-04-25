"""Live parallelism monitor: per-worker state + per-process CPU + chunk delta."""
from __future__ import annotations

import sys, time, datetime as dt
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src import db

DRIVE_ID = "0AO_74QBzpdLrUk9PVA"
SCHEMA   = f'"fd_{DRIVE_ID}"'


def _chunk_stats(conn, cur):
    try:
        cur.execute(f"""SELECT COUNT(DISTINCT split_part(split_part(drive_file_id,'_chunk_',1),'_sheet_',1)) AS f,
                               COUNT(*) AS c FROM {SCHEMA}.documents""")
        r = cur.fetchone()
        return r["f"], r["c"]
    except Exception:
        conn.rollback()
        return 0, 0


def _cpu_snap():
    """Return {pid: (user_ms, kernel_ms, threads, rss_mb)} via powershell cimi."""
    import subprocess
    ps = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command",
         "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
         "Where-Object { $_.CommandLine -like '*rag_daemon*' } | "
         "ForEach-Object { \"$($_.ProcessId)|$($_.UserModeTime)|$($_.KernelModeTime)|"
         "$($_.ThreadCount)|$($_.WorkingSetSize)|$($_.CommandLine.Substring(0,30))\" }"],
        capture_output=True, text=True, timeout=10,
    )
    out = {}
    for line in ps.stdout.strip().splitlines():
        parts = line.split("|")
        if len(parts) < 6: continue
        pid = int(parts[0])
        out[pid] = {
            "user_100ns":   int(parts[1]),   # 100ns units
            "kernel_100ns": int(parts[2]),
            "threads":      int(parts[3]),
            "rss_mb":       int(parts[4]) / 1024 / 1024,
            "tag":          parts[5],
        }
    return out


def main():
    conn = db.connect()
    cur = conn.cursor()
    prev_cpu = _cpu_snap()
    prev_t   = time.time()

    # baseline chunks
    pf, pc = _chunk_stats(conn, cur)
    print(f"[{dt.datetime.now().strftime('%H:%M:%S')}] baseline: {pf} files, {pc} chunks")
    print()

    for i in range(24):    # 24 samples x 5s = 2 min
        time.sleep(5)
        now = time.time()
        cur_cpu = _cpu_snap()
        f, c = _chunk_stats(conn, cur)

        cur.execute("SELECT worker_id, state, phase, files_done, total_files, current_file, heartbeat_at FROM daemon_workers ORDER BY worker_id")
        workers = cur.fetchall()

        cur.execute(f"SELECT state, rotate_token, total_files_listed FROM fd_registry WHERE drive_id='{DRIVE_ID}'")
        fd = cur.fetchone()

        dt_s = now - prev_t
        print(f"[{dt.datetime.now().strftime('%H:%M:%S')}]   dt={dt_s:.1f}s  chunks: {c} ({'+' if c>=pc else ''}{c-pc})  files: {f} (+{f-pf})  fd_state={fd['state']} rotate={fd['rotate_token']} listed={fd['total_files_listed']}")

        total_done = 0
        for w in workers:
            total_done += w["files_done"] or 0
            cfile = (w["current_file"] or "")[:35]
            state = w["state"] or "-"
            phase = w["phase"] or "-"
            print(f"         w#{w['worker_id']}: {state:<10} {phase:<20} {w['files_done']:>3}/{w['total_files'] or 0:<3}  {cfile}")
        print(f"         Σ files_done = {total_done}")

        for pid, m in cur_cpu.items():
            prev = prev_cpu.get(pid)
            if prev:
                du = (m["user_100ns"]   - prev["user_100ns"])   / 10_000_000  # -> seconds
                dk = (m["kernel_100ns"] - prev["kernel_100ns"]) / 10_000_000
                cpu_pct_user   = (du / dt_s) * 100 if dt_s > 0 else 0
                cpu_pct_kernel = (dk / dt_s) * 100 if dt_s > 0 else 0
                print(f"         pid {pid:>6} threads={m['threads']:>3} rss={m['rss_mb']:.0f}MB  user={cpu_pct_user:.0f}%  kernel={cpu_pct_kernel:.0f}%  (sum={cpu_pct_user+cpu_pct_kernel:.0f}%)")
        print()
        pf, pc = f, c
        prev_cpu = cur_cpu
        prev_t   = now
    conn.close()


if __name__ == "__main__":
    main()
