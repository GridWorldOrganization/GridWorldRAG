"""モニター画面のレンダリング。

monitor.sh から呼び出され、進捗ファイルを読み取って表示テキストを生成する。
引数: worker_count progress_dir elapsed_str
"""

import json
import os
import re
import sys


def render(worker_count, progress_dir, elapsed):
    """全ワーカーの進捗を読み取り、表示行のリストを返す。"""
    lines = []
    total_proc = 0
    total_skip = 0
    total_chunks = 0
    total_errors = 0

    # ヘッダ
    lines.append(f"GridWorldRAG build index {elapsed}")

    # ワーカー行
    for idx in range(worker_count):
        f = os.path.join(progress_dir, f"worker_{idx}.json")
        wid = idx + 1
        if not os.path.exists(f):
            lines.append(f"W{wid}[....................] 停止中")
            continue
        try:
            d = json.load(open(f))
        except (json.JSONDecodeError, IOError):
            lines.append(f"W{wid}[....................] 読込中")
            continue

        status = d.get("status", "run")
        proc = d.get("processed", 0)
        skip = d.get("skipped", 0)
        errs = d.get("errors", 0)
        chunks = d.get("chunks", 0)
        total_proc += proc
        total_skip += skip
        total_chunks += chunks
        total_errors += errs

        if status == "done":
            lines.append(f"W{wid}[####################] done {proc} tasks")
        elif status == "loading":
            lines.append(f"W{wid}[....................] 起動中")
        elif status == "ready":
            lines.append(f"W{wid}[....................] 待機中")
        else:
            tp = d.get("drive_type", "共有")
            nm = d.get("drive", "")
            pt = d.get("part", "")
            cur = d.get("current", 0)
            task_size = d.get("total", 1)
            drive_total = d.get("drive_total", task_size)
            label = f"{tp} {nm}({pt})" if pt else f"{tp} {nm}"
            pct = cur * 100 // task_size if task_size > 0 else 0
            filled = pct // 5
            bar = "#" * filled + "." * (20 - filled)
            lines.append(f"W{wid}[{bar}]{pct:3d}% {label} {cur}/{task_size}/{drive_total}")

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

    # サマリ行
    total_current = total_proc + total_skip + total_errors
    total_detail = total_proc + total_skip
    lines.append(
        f"workers:{worker_count} drives:{drives_done}/{total_drive_count}"
        f" tasks:{total_tasks_done}/{total_tasks}"
        f" scanned:{total_current} detail:{total_proc}/{total_detail}"
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
