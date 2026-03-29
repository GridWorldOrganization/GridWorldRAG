#!/bin/bash
# GridWorldRAG - リアルタイム進捗モニター（インプレース更新）
# 使い方: ./monitor.sh [BUILD_PID] [WORKER_COUNT]

BUILD_PID="${1:-}"
WORKER_COUNT="${2:-10}"
MONITOR_INTERVAL_MS="${3:-1000}"
# ミリ秒 → 秒（小数）に変換
SLEEP_SEC=$(echo "scale=3; $MONITOR_INTERVAL_MS / 1000" | bc)
PROGRESS_DIR="/tmp/gridworldrag_progress"
LOG_FILE="/tmp/gridworldrag_build.log"
START_TIME_FILE="/tmp/gridworldrag_start_time"

if [ -f "$START_TIME_FILE" ]; then
    START_EPOCH=$(cat "$START_TIME_FILE")
else
    START_EPOCH=$(date +%s)
fi

# 表示行数 = ヘッダ1 + ワーカー数 + 区切り1 + サマリ1 + フッタ1
TOTAL_LINES=$((WORKER_COUNT + 4))

# 初回は空行を出力して領域確保
for i in $(seq 1 $TOTAL_LINES); do echo ""; done

# 全表示を Python で一括生成（全角幅の問題を回避）
while true; do
    printf "\033[${TOTAL_LINES}A"

    NOW_EPOCH=$(date +%s)
    ELAPSED=$((NOW_EPOCH - START_EPOCH))
    ELAPSED_M=$((ELAPSED / 60))
    ELAPSED_S=$((ELAPSED % 60))
    ELAPSED_STR=$(printf "%d:%02d" "$ELAPSED_M" "$ELAPSED_S")
    COLS=$(tput cols 2>/dev/null || echo 80)

    # Python で全行を生成（1行 = COLS幅以内に切り詰め）
    python3 -c "
import json, os, unicodedata

cols = $COLS
worker_count = $WORKER_COUNT
progress_dir = '$PROGRESS_DIR'
elapsed = '$ELAPSED_STR'

def display_width(s):
    w = 0
    for c in s:
        if unicodedata.east_asian_width(c) in ('F','W'):
            w += 2
        else:
            w += 1
    return w

def truncate(s, max_w):
    w = 0
    out = ''
    for c in s:
        cw = 2 if unicodedata.east_asian_width(c) in ('F','W') else 1
        if w + cw > max_w:
            break
        out += c
        w += cw
    return out + ' ' * (max_w - w)

lines = []
total_proc = 0
total_skip = 0
total_chunks = 0
total_errors = 0

# ヘッダ
lines.append(f'GridWorldRAG build index {elapsed}')

# ワーカー行
for idx in range(worker_count):
    f = os.path.join(progress_dir, f'worker_{idx}.json')
    wid = idx + 1
    if not os.path.exists(f):
        lines.append(f'W{wid:<2}[....................] 停止中')
        continue
    try:
        d = json.load(open(f))
    except:
        lines.append(f'W{wid:<2}[....................] 読込中')
        continue

    status = d.get('status', 'run')
    proc = d.get('processed', 0)
    skip = d.get('skipped', 0)
    errs = d.get('errors', 0)
    chunks = d.get('chunks', 0)
    total_proc += proc
    total_skip += skip
    total_chunks += chunks
    total_errors += errs

    if status == 'done':
        lines.append(f'W{wid:<2}[####################] done {proc} tasks')
    elif status == 'loading':
        lines.append(f'W{wid:<2}[....................] 起動中')
    elif status == 'ready':
        lines.append(f'W{wid:<2}[....................] 待機中')
    else:
        tp = d.get('drive_type', '共有')
        nm = d.get('drive', '')
        pt = d.get('part', '')
        cur = d.get('current', 0)
        task_size = d.get('total', 1)
        drive_total = d.get('drive_total', task_size)
        label = f'{tp} {nm}({pt})' if pt else f'{tp} {nm}'
        pct = cur * 100 // task_size if task_size > 0 else 0
        filled = pct // 5
        bar = '#' * filled + '.' * (20 - filled)
        lines.append(f'W{wid:<2}[{bar}] {pct:3d}% {label} {cur}/{task_size}/{drive_total}')

lines.append('---')

total_current = total_proc + total_skip + total_errors
total_detail = total_proc + total_skip
# 完了タスク数・ドライブ数を集計
import re as _re
all_completed = []
total_tasks_done = 0
total_tasks = 0
for i in range(worker_count):
    wf = os.path.join(progress_dir, f'worker_{i}.json')
    if os.path.exists(wf):
        try:
            wd = json.load(open(wf))
            total_tasks_done += wd.get('drives_done', 0)
            total_tasks = max(total_tasks, wd.get('drives_total', 0))
            all_completed.extend(wd.get('completed_tasks', []))
        except:
            pass

# ドライブごとに全パート完了かチェック
drive_parts = {}  # drive_name → {完了パート番号のset}
drive_total_parts = {}  # drive_name → 総パート数
for ct in all_completed:
    m = _re.match(r'^(.+)\((\d+)/(\d+)\)$', ct)
    if m:
        name, part_num, part_total = m.group(1), int(m.group(2)), int(m.group(3))
        drive_parts.setdefault(name, set()).add(part_num)
        drive_total_parts[name] = part_total

total_drives_all = len(set(drive_total_parts.keys()) | set(
    d.get('drive','') for i in range(worker_count)
    for d in [json.load(open(os.path.join(progress_dir, f'worker_{i}.json')))]
    if os.path.exists(os.path.join(progress_dir, f'worker_{i}.json'))
    and d.get('drive','') and d.get('status') not in ('loading','ready','done')
) if True else set())
# 全パート完了したドライブ数
drives_done = sum(1 for name, parts in drive_parts.items()
                  if len(parts) >= drive_total_parts.get(name, 999))
# 総ドライブ数はファイル一覧取得時の数（run_build.sh のヘッダから取得不可なのでタスクから推定）
all_drive_names = set()
for ct in all_completed:
    m = _re.match(r'^(.+)\(\d+/\d+\)$', ct)
    if m:
        all_drive_names.add(m.group(1))
# 処理中ドライブも含める
for i in range(worker_count):
    wf = os.path.join(progress_dir, f'worker_{i}.json')
    if os.path.exists(wf):
        try:
            wd = json.load(open(wf))
            if wd.get('drive',''):
                all_drive_names.add(wd['drive'])
        except:
            pass
total_drive_count = len(all_drive_names) if all_drive_names else '?'

lines.append(f'workers:{worker_count}  drives:{drives_done}/{total_drive_count}  tasks:{total_tasks_done}/{total_tasks}  scanned:{total_current}  detail:{total_proc}/{total_detail}  chunks:{total_chunks}  errors:{total_errors}')

lines.append(f'Ctrl+C to stop | log: tail -f /tmp/gridworldrag_build.log')

for line in lines:
    # 表示幅で切り詰めて行クリア
    t = truncate(line, cols - 1)
    print(f'\033[K{t}')
" 2>/dev/null

    if [ -n "$BUILD_PID" ] && ! kill -0 "$BUILD_PID" 2>/dev/null; then
        echo ""
        echo "Build complete!"
        tail -10 "$LOG_FILE"
        exit 0
    fi

    sleep "$SLEEP_SEC"
done
