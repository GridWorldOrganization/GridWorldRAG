"""モニター画面のレンダリング。

monitor.sh から呼び出され、進捗ファイルを読み取って表示テキストを生成する。
引数: worker_count progress_dir elapsed_str
"""

import json
import os
import re
import sys
import time as _time

from src.config import WorkerStatus


def render(worker_count, progress_dir, elapsed):
    """全ワーカーの進捗を読み取り、表示行のリストを返す。"""
    lines = []
    total_proc = 0
    total_skip = 0
    total_chunks = 0
    total_errors = 0

    # ヘッダ
    lines.append(f"GridWorldRAG build index workers:{worker_count} time={elapsed}")

    # ワーカー行
    for idx in range(1, worker_count + 1):
        f = os.path.join(progress_dir, f"worker_{idx}.json")
        if not os.path.exists(f):
            lines.append(f"W{idx}[....................] 停止中")
            continue
        try:
            d = json.load(open(f))
        except (json.JSONDecodeError, IOError):
            lines.append(f"W{idx}[....................] 読込中")
            continue
        wid = d.get("worker_id", idx)

        status = d.get("status", "run")
        proc = d.get("processed", 0)
        skip = d.get("skipped", 0)
        errs = d.get("errors", 0)
        chunks = d.get("chunks", 0)
        total_proc += proc
        total_skip += skip
        total_chunks += chunks
        total_errors += errs

        if status == WorkerStatus.DONE:
            lines.append(f"W{wid}[####################] 完了")
        elif status == WorkerStatus.LOADING:
            lines.append(f"W{wid}[....................] 起動中")
        elif status == WorkerStatus.RATE_LIMITED:
            lines.append(f"W{wid}[....................] レート制限待ち")
        elif status == WorkerStatus.RATE_LIMITED_RUNNING:
            cur = d.get("current", 0)
            task_size = d.get("total", 1)
            pct = cur * 100 // task_size if task_size > 0 else 0
            tp = d.get("drive_type", "共有")
            nm = d.get("drive", "")
            pt = d.get("part", "")
            label = f"{tp} {nm}({pt})" if pt else f"{tp} {nm}"
            filled = pct // 5
            bar = "#" * filled + "." * (20 - filled)
            lines.append(f"W{wid}[{bar}]{pct:3d}%(レート制限待ち) {label} {cur}/{task_size}")
        elif status == WorkerStatus.READY:
            lines.append(f"W{wid}[....................] 待機中")
        else:
            tp = d.get("drive_type", "共有")
            nm = d.get("drive", "")
            pt = d.get("part", "")
            cur = d.get("current", 0)
            task_size = d.get("total", 1)
            drive_total = d.get("drive_total", task_size)
            resume_skip = d.get("resume_skip_count", 0)
            label = f"{tp} {nm}({pt})" if pt else f"{tp} {nm}"
            pct = cur * 100 // task_size if task_size > 0 else 0
            filled = pct // 5
            bar = "#" * filled + "." * (20 - filled)
            # 更新停滞検出（30秒以上進捗更新なし → API待ち表示）
            _updated_at = d.get("updated_at", 0)
            _stalled_sec = int(_time.time() - _updated_at) if _updated_at else 0
            _stalled = f"(レート制限待ち{_stalled_sec}s) " if _stalled_sec >= 30 else ""
            _skip = f"skipping({resume_skip}) " if resume_skip > 0 else ""
            lines.append(f"W{wid}[{bar}]{pct:3d}% {_stalled}{_skip}{label} {cur}/{task_size}/{drive_total}")

    lines.append("---")

    # 完了タスク数・ドライブ数を集計
    all_completed = []
    total_tasks_done = 0
    total_tasks = 0
    for i in range(worker_count):
        wf = os.path.join(progress_dir, f"worker_{i}.json")
        if os.path.exists(wf):
            try:
                wd = json.load(open(wf))
                total_tasks_done += wd.get("drives_done", 0)
                total_tasks = max(total_tasks, wd.get("drives_total", 0))
                all_completed.extend(wd.get("completed_tasks", []))
            except (json.JSONDecodeError, IOError):
                pass

    # ドライブ完了判定（全パート完了で +1）
    drive_parts = {}
    drive_total_parts = {}
    for ct in all_completed:
        m = re.match(r"^(.+)\((\d+)/(\d+)\)$", ct)
        if m:
            name = m.group(1)
            drive_parts.setdefault(name, set()).add(int(m.group(2)))
            drive_total_parts[name] = int(m.group(3))

    drives_done = sum(
        1 for name, parts in drive_parts.items()
        if len(parts) >= drive_total_parts.get(name, 999)
    )

    # 総ドライブ数（完了+処理中）
    all_drive_names = set()
    for ct in all_completed:
        m = re.match(r"^(.+)\(\d+/\d+\)$", ct)
        if m:
            all_drive_names.add(m.group(1))
    for i in range(worker_count):
        wf = os.path.join(progress_dir, f"worker_{i}.json")
        if os.path.exists(wf):
            try:
                wd = json.load(open(wf))
                if wd.get("drive", ""):
                    all_drive_names.add(wd["drive"])
            except (json.JSONDecodeError, IOError):
                pass
    total_drive_count = len(all_drive_names) if all_drive_names else "?"

    # 全アイテム数を pickle から取得
    total_items = "?"
    task_data_pkl = "/tmp/gridworldrag_taskdata.pkl"
    if os.path.exists(task_data_pkl):
        try:
            import pickle
            with open(task_data_pkl, "rb") as pf:
                bundle = pickle.load(pf)
            total_items = bundle.get("total_items", "?")
        except Exception:
            pass

    # 全体進捗バー（アイテム数ベース）
    total_current = total_proc + total_skip + total_errors
    if isinstance(total_items, int) and total_items > 0:
        overall_pct = total_current * 100 // total_items
        overall_filled = overall_pct * 40 // 100
        overall_bar = "#" * overall_filled + "." * (40 - overall_filled)
        lines.append(f"[{overall_bar}]{overall_pct:3d}% {total_current}/{total_items} items")
    else:
        lines.append(f"[........................................] ?% {total_current}/? items")

    # サマリ行
    total_detail = total_proc + total_skip
    lines.append(
        f"drives:{drives_done}/{total_drive_count}"
        f"(tasks:{total_tasks_done}/{total_tasks})"
        f" item-scanned:{total_current}/{total_items}"
        f" detail:{total_proc}/{total_detail}"
        f" chunks:{total_chunks} errors:{total_errors}"
    )
    lines.append("Ctrl+C to stop | log: tail -f /tmp/gridworldrag_build.log")

    return lines


if __name__ == "__main__":
    worker_count = int(sys.argv[1])
    progress_dir = sys.argv[2]
    elapsed = sys.argv[3]
    for line in render(worker_count, progress_dir, elapsed):
        print(line)
