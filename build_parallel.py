#!/usr/bin/env python3
"""
build_parallel.py - タスクキュー方式の並列インデックス構築

大きな共有ドライブは TASK_SPLIT_THRESHOLD で分割し、
ワーカーは手が空いたら次のタスクをキューから取って処理する。

使い方:
    python build_parallel.py              # config.env の PARALLEL_WORKERS で並列
    python build_parallel.py -w 4         # 4 並列
    python build_parallel.py --dry-run    # 処理対象の確認のみ

モニター付き起動:
    ./run_build.sh                        # 起動 + リアルタイムモニター
"""

import argparse
import json
import multiprocessing
import pickle
import os
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from src.config import (
    EMBEDDING_MODEL, CHUNK_SIZE, CHUNK_OVERLAP, BATCH_SIZE,
    GOOGLE_EMAIL, INDEX_MY_DRIVE, INDEX_SHARED_DRIVES, INDEX_IMAGE_OCR,
    PARALLEL_WORKERS, TASK_SPLIT_THRESHOLD, MONITOR_INTERVAL_MS,
    WORKER_START_INTERVAL_SEC, load_shared_drives_whitelist,
)
from src.drive_client import (
    authenticate, list_files_in_drive, extract_text,
    extract_spreadsheet_sheets, SKIP_MIME_TYPES,
)
from src.indexer import make_chunk_entry

PROGRESS_DIR = "/tmp/gridworldrag_progress"
FILELIST_PKL = "/tmp/gridworldrag_filelist.pkl"
TASK_DATA_PKL = "/tmp/gridworldrag_taskdata.pkl"
FOLDER_MIME = "application/vnd.google-apps.folder"


# ---------------------------------------------------------------------------
# 進捗書き出し
# ---------------------------------------------------------------------------

def _write_progress(worker_id, *, task_label="", task_current=0, task_size=0,
                    processed=0, skipped=0, errors=0, chunks=0,
                    status="running", drive_type="共有",
                    tasks_done=0, tasks_total=0, part="", drive_total=0,
                    completed_tasks=None):
    """ワーカーの進捗を JSON ファイルに書き出す（モニター用）。"""
    os.makedirs(PROGRESS_DIR, exist_ok=True)
    path = os.path.join(PROGRESS_DIR, f"worker_{worker_id}.json")
    with open(path, "w") as f:
        json.dump({
            "worker_id": worker_id,
            "drive": task_label,
            "drive_type": drive_type,
            "current": task_current,
            "total": task_size,
            "processed": processed,
            "skipped": skipped,
            "errors": errors,
            "chunks": chunks,
            "status": status,
            "drives_done": tasks_done,
            "drives_total": tasks_total,
            "part": part,
            "drive_total": drive_total,
            "completed_tasks": completed_tasks or [],
        }, f)


# ---------------------------------------------------------------------------
# ワーカー
# ---------------------------------------------------------------------------

def _process_file(file_info, model, splitter, service, batch):
    """1ファイルを処理し、チャンクを batch に追加する。

    Returns:
        (processed_count, skipped_count, error_count, chunk_count)
    """
    file_name = file_info["name"]
    mime_type = file_info["mimeType"]

    # 画像: OCR 無効時はファイル名のみ DB 投入
    if mime_type.startswith("image/") and not INDEX_IMAGE_OCR:
        try:
            text = f"[画像] {file_name}"
            emb = model.encode([text])[0]
            batch.append(make_chunk_entry(file_info, text, emb, 0))
            return 1, 0, 0, 1
        except Exception:
            return 0, 0, 1, 0

    # スプレッドシート: シート別処理
    if mime_type == "application/vnd.google-apps.spreadsheet":
        sheets = extract_spreadsheet_sheets(file_info["id"])
        if not sheets:
            return 0, 1, 0, 0

        file_chunks = 0
        file_errors = 0
        for sheet in sheets:
            sheet_content = f"[シート: {sheet['name']}]\n{sheet['content']}"
            chunks = splitter.split_text(sheet_content)
            if not chunks:
                continue
            try:
                embeddings = model.encode(chunks)
            except Exception:
                file_errors += 1
                continue
            for ci, (chunk_text, emb) in enumerate(zip(chunks, embeddings)):
                batch.append(make_chunk_entry(
                    file_info, chunk_text, emb, ci,
                    sheet_gid=sheet["gid"], sheet_name=sheet["name"],
                ))
            file_chunks += len(chunks)

        if file_chunks > 0:
            return 1, 0, file_errors, file_chunks
        return 0, 1, file_errors, 0

    # その他のファイル（Docs, PDF, フォルダ, テキスト, 動画, 音声 等）
    text = extract_text(service, file_info)
    if not text or not text.strip():
        return 0, 1, 0, 0

    chunks = splitter.split_text(text)
    if not chunks:
        return 0, 1, 0, 0

    try:
        embeddings = model.encode(chunks)
    except Exception:
        return 0, 0, 1, 0

    for ci, (chunk_text, emb) in enumerate(zip(chunks, embeddings)):
        batch.append(make_chunk_entry(file_info, chunk_text, emb, ci))

    return 1, 0, 0, len(chunks)


def _worker(worker_id, task_queue, tasks_total, results_queue):
    """ワーカープロセス: キューからタスクを取得して順次処理する。"""
    # 起動中マーカー（モデルロード前に即座に書き出し）
    _write_progress(worker_id, status="loading", tasks_total=tasks_total)

    from langchain_text_splitters import RecursiveCharacterTextSplitter
    from sentence_transformers import SentenceTransformer
    from src.db import connect, insert_chunks

    model = SentenceTransformer(EMBEDDING_MODEL)
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP,
    )
    conn = connect()
    service = authenticate()

    total_processed = 0
    total_skipped = 0
    total_errors = 0
    total_chunks = 0
    tasks_done = 0
    completed_tasks = []  # 完了タスクのラベル+パート ["GW_庶務(1/1)", "GW_LIB_EDU(1/5)"]

    def progress(status="running", **kwargs):
        """進捗書き出しのショートカット。"""
        _write_progress(
            worker_id, status=status,
            processed=total_processed, skipped=total_skipped,
            errors=total_errors, chunks=total_chunks,
            tasks_done=tasks_done, tasks_total=tasks_total,
            completed_tasks=completed_tasks,
            **kwargs,
        )

    # タスクのファイルデータを pickle から読み込み
    with open(TASK_DATA_PKL, "rb") as f:
        all_task_data = pickle.load(f)

    progress(status="ready")

    while True:
        task = task_queue.get()  # ブロッキング取得（センチネル None で終了）
        if task is None:
            break

        task_id = task["task_id"]
        task_label = task["label"]
        drive_type = task["drive_type"]
        files = all_task_data[task_id]
        task_part = task["part"]
        task_drive_total = task["drive_total"]
        task_size = len(files)
        task_start = time.strftime("%H:%M:%S")
        task_processed_before = total_processed
        task_skipped_before = total_skipped
        task_errors_before = total_errors
        print(f"[{task_start}] W{worker_id + 1} 開始: {task_label}({task_part}) {task_size} ファイル")

        batch = []
        for fi, file_info in enumerate(files):
            p, s, e, c = _process_file(file_info, model, splitter, service, batch)
            total_processed += p
            total_skipped += s
            total_errors += e
            total_chunks += c

            # バッチ挿入
            if len(batch) >= BATCH_SIZE:
                insert_chunks(conn, batch)
                batch = []

            # 進捗更新
            progress(
                task_label=task_label, task_current=fi + 1, task_size=task_size,
                drive_type=drive_type, part=task_part, drive_total=task_drive_total,
            )

        # タスク完了: 残りバッチ挿入
        if batch:
            insert_chunks(conn, batch)

        task_end = time.strftime("%H:%M:%S")
        task_p = total_processed - task_processed_before
        task_s = total_skipped - task_skipped_before
        task_e = total_errors - task_errors_before
        print(f"[{task_end}] W{worker_id + 1} 完了: {task_label}({task_part})"
              f"  処理:{task_p} スキップ:{task_s} エラー:{task_e}")
        completed_tasks.append(f"{task_label}({task_part})")
        tasks_done += 1

        # 100%表示を確実に見せてから待機中に遷移
        progress(
            task_label=task_label, task_current=task_size, task_size=task_size,
            drive_type=drive_type, part=task_part, drive_total=task_drive_total,
        )
        t_100 = time.strftime("%H:%M:%S")
        time.sleep(MONITOR_INTERVAL_MS / 1000)
        progress(status="ready")
        t_ready = time.strftime("%H:%M:%S")
        time.sleep(MONITOR_INTERVAL_MS / 1000)
        t_before_get = time.strftime("%H:%M:%S")
        print(f"[DEBUG] W{worker_id+1} タスク間: 100%={t_100} ready={t_ready} get前={t_before_get}")

    conn.close()
    progress(status="done")

    results_queue.put({
        "worker_id": worker_id,
        "processed": total_processed,
        "skipped": total_skipped,
        "errors": total_errors,
        "total_chunks": total_chunks,
        "tasks_done": tasks_done,
    })


# ---------------------------------------------------------------------------
# タスク分割
# ---------------------------------------------------------------------------

def _count_files_folders(files):
    """ファイル数とフォルダ数を返す。"""
    folders = sum(1 for f in files if f["mimeType"] == FOLDER_MIME)
    return len(files) - folders, folders


def _split_into_tasks(drive_file_lists):
    """大きなドライブを分割してタスクリストを作る。

    TASK_SPLIT_THRESHOLD を超えるドライブは複数タスクに分割される。
    """
    threshold = TASK_SPLIT_THRESHOLD
    tasks = []

    for drive_name, drive_type, files in drive_file_lists:
        drive_total = len(files)
        if drive_total <= threshold:
            tasks.append({
                "label": drive_name,
                "drive_type": drive_type,
                "files": files,
                "part": "1/1",
                "drive_total": drive_total,
            })
        else:
            n_parts = (drive_total + threshold - 1) // threshold
            for part_i in range(n_parts):
                start = part_i * threshold
                end = min(start + threshold, drive_total)
                tasks.append({
                    "label": drive_name,
                    "drive_type": drive_type,
                    "files": files[start:end],
                    "part": f"{part_i + 1}/{n_parts}",
                    "drive_total": drive_total,
                })

    return tasks


# ---------------------------------------------------------------------------
# ファイル一覧取得
# ---------------------------------------------------------------------------

def collect_file_lists(service):
    """全ドライブのファイル一覧を収集する。結果は stdout に出力。"""
    drive_file_lists = []

    # 取得対象ドライブ数を計算
    total_drives = 0
    if INDEX_MY_DRIVE:
        total_drives += 1
    whitelist = set()
    if INDEX_SHARED_DRIVES:
        whitelist = load_shared_drives_whitelist()
        total_drives += len(whitelist)

    done_drives = 0

    # ドットアニメーション + スレッドA/B 表示
    _fetch_done = [0]
    _thread_current = {"A": "-", "B": "-"}  # スレッドA/Bが処理中の番号
    _fetch_lock = threading.Lock()
    _fetch_stop = threading.Event()

    def _dot_animator():
        dots = 0
        while not _fetch_stop.is_set():
            dots = (dots % 3) + 1
            d = "." * dots
            with _fetch_lock:
                done = _fetch_done[0]
                a = _thread_current["A"]
                b = _thread_current["B"]
            msg = f"2スレッドでファイル一覧を取得中 A:#{a} B:#{b} ({done}/{total_drives}完了)"
            print(f"\r{msg}{d}\033[K", end="", flush=True)
            _fetch_stop.wait(0.5)

    _dot_thread = threading.Thread(target=_dot_animator, daemon=True)
    _dot_thread.start()

    # スレッド名→A/Bのマッピング
    _thread_names = {}
    _thread_name_lock = threading.Lock()

    def _get_thread_label():
        tid = threading.current_thread().name
        with _thread_name_lock:
            if tid not in _thread_names:
                _thread_names[tid] = "A" if len(_thread_names) == 0 else "B"
            return _thread_names[tid]

    def _fetch_start(idx):
        label = _get_thread_label()
        with _fetch_lock:
            _thread_current[label] = str(idx)

    def _fetch_complete(idx):
        label = _get_thread_label()
        with _fetch_lock:
            _thread_current[label] = "-"
            _fetch_done[0] += 1

    if INDEX_MY_DRIVE:
        done_drives += 1
        _fetch_start(done_drives)
        my_files = list_files_in_drive(service, corpora="user")
        _fetch_complete(done_drives)
        my_files = [
            f for f in my_files
            if not f.get("driveId")
            and f.get("owners")
            and any(o.get("emailAddress") == GOOGLE_EMAIL for o in f["owners"])
        ]
        if my_files:
            drive_file_lists.append(("マイドライブ", "マイ", my_files))

    if INDEX_SHARED_DRIVES and whitelist:
        drives_response = service.drives().list(pageSize=100).execute()
        drive_names = {d["id"]: d["name"] for d in drives_response.get("drives", [])}

        # スレッド間のレート制限協調
        rate_lock = threading.Lock()
        rate_backoff = [0.0]

        # 取得対象リストを番号付きで作成
        fetch_list = []
        for drive_id in whitelist:
            done_drives += 1
            drive_name = drive_names.get(drive_id, drive_id)
            fetch_list.append((done_drives, drive_id, drive_name))

        def _fetch_drive(idx, drive_id, drive_name):
            import time as t
            _fetch_start(idx)
            # レート制限バックオフ中なら待つ
            while True:
                with rate_lock:
                    wait_until = rate_backoff[0]
                now = t.time()
                if now >= wait_until:
                    break
                t.sleep(wait_until - now)
            try:
                svc = authenticate()
                files = list_files_in_drive(svc, drive_id=drive_id)
                _fetch_complete(idx)
                return idx, drive_name, files, None
            except Exception as e:
                error_str = str(e)
                if "rate limit" in error_str.lower() or "429" in error_str:
                    with rate_lock:
                        rate_backoff[0] = t.time() + 10
                    t.sleep(10)
                    try:
                        svc = authenticate()
                        files = list_files_in_drive(svc, drive_id=drive_id)
                        _fetch_complete(idx)
                        return idx, drive_name, files, None
                    except Exception as e2:
                        _fetch_complete(idx)
                        return idx, drive_name, None, e2
                _fetch_complete(idx)
                return idx, drive_name, None, e

        # 2スレッドで並列取得
        executor = ThreadPoolExecutor(max_workers=2)
        try:
            futures = [
                executor.submit(_fetch_drive, idx, did, dname)
                for idx, did, dname in fetch_list
            ]

            results = []
            for future in as_completed(futures):
                idx, drive_name, files, error = future.result()
                if error:
                    print(f"ERROR: {drive_name}: {error}", file=sys.stderr)
                elif files:
                    results.append((idx, drive_name, files))
        except KeyboardInterrupt:
            executor.shutdown(wait=False, cancel_futures=True)
            _fetch_stop.set()
            raise
        finally:
            executor.shutdown(wait=False)

        # 番号順にソートして追加
        for idx, drive_name, files in sorted(results):
            drive_file_lists.append((drive_name, "共有", files))

    # アニメーション停止
    _fetch_stop.set()
    _dot_thread.join()

    return drive_file_lists


def print_file_list_summary(drive_file_lists):
    """ファイル一覧のサマリを stdout に出力する。"""
    total_fc = sum(
        sum(1 for f in files if f["mimeType"] != FOLDER_MIME)
        for _, _, files in drive_file_lists
    )
    total_dc = sum(len(files) for _, _, files in drive_file_lists) - total_fc

    for i, (name, dtype, files) in enumerate(drive_file_lists, 1):
        fc, dc = _count_files_folders(files)
        print(f"  ({i}/{len(drive_file_lists)}) {name} {fc + dc}アイテム ({fc}ファイル {dc}フォルダ)")
    print(f"合計: {len(drive_file_lists)} ドライブ, {total_fc + total_dc}アイテム ({total_fc}ファイル {total_dc}フォルダ)")


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="タスクキュー方式の並列インデックス構築")
    parser.add_argument("-w", "--workers", type=int, default=PARALLEL_WORKERS,
                        help=f"並列ワーカー数 (デフォルト: {PARALLEL_WORKERS})")
    parser.add_argument("--dry-run", action="store_true", help="処理対象の確認のみ")
    parser.add_argument("--fetch-only", action="store_true",
                        help="ファイル一覧取得のみ（結果を /tmp/gridworldrag_filelist.pkl に保存）")
    parser.add_argument("--work-only", action="store_true",
                        help="ワーカー処理のみ（/tmp/gridworldrag_filelist.pkl を読み込み）")
    args = parser.parse_args()

    if args.work_only:
        # pickle からファイル一覧を読み込み
        with open(FILELIST_PKL, "rb") as f:
            drive_file_lists = pickle.load(f)
        # 認証（ワーカー内の get_sheets_service() 等で _credentials が必要）
        authenticate()
    else:
        # 1. 認証
        print("Google Drive 認証中...", end="", flush=True)
        service = authenticate()
        print(" 完了", flush=True)

        # 2. ファイル一覧取得
        try:
            drive_file_lists = collect_file_lists(service)
        except KeyboardInterrupt:
            print(f"\r2スレッドでファイル一覧を取得中... 中断\033[K")
            sys.exit(1)
        print(f"\r2スレッドでファイル一覧を取得中... 完了\033[K")
        print_file_list_summary(drive_file_lists)

        if args.fetch_only:
            # pickle に保存して終了
            with open(FILELIST_PKL, "wb") as f:
                pickle.dump(drive_file_lists, f)
            return

    # 3. タスク分割
    tasks = _split_into_tasks(drive_file_lists)
    n_workers = min(args.workers, len(tasks))

    print(f"\nタスク分割: {len(drive_file_lists)} ドライブ → {len(tasks)} タスク "
          f"(閾値: {TASK_SPLIT_THRESHOLD})")
    for t in tasks:
        print(f"  {t['label']}({t['part']}): {len(t['files'])} ファイル")

    if args.dry_run:
        return

    # 4. タスクキュー（センチネル方式）
    # タスクにファイルデータを入れると Queue のパイプが詰まるため、
    # ファイルデータは pickle ファイルに保存し、キューにはインデックスのみ入れる
    task_file_data = {}  # task_id → files
    task_meta = []  # キューに入れる軽量タスク情報
    for i, t in enumerate(tasks):
        task_file_data[i] = t["files"]
        task_meta.append({
            "task_id": i,
            "label": t["label"],
            "drive_type": t["drive_type"],
            "part": t["part"],
            "drive_total": t["drive_total"],
        })

    # ファイルデータを pickle に保存（ワーカーが読み込む）
    with open(TASK_DATA_PKL, "wb") as f:
        pickle.dump(task_file_data, f)

    # 各ドライブの 1/n → 全ドライブの 2/n → ... の順でキューに入れる
    max_parts = max((int(t["part"].split("/")[1]) for t in tasks), default=1)
    task_queue = multiprocessing.Queue()
    for part_round in range(1, max_parts + 1):
        for tm in task_meta:
            part_num = int(tm["part"].split("/")[0])
            if part_num == part_round:
                task_queue.put(tm)
    for _ in range(n_workers):
        task_queue.put(None)  # 終了マーカー

    os.makedirs(PROGRESS_DIR, exist_ok=True)

    # 5. ワーカー起動
    start_time = time.time()
    results_queue = multiprocessing.Queue()
    processes = []

    for i in range(n_workers):
        p = multiprocessing.Process(
            target=_worker,
            args=(i, task_queue, len(tasks), results_queue),
        )
        p.daemon = True
        p.start()
        processes.append(p)
        if i < n_workers - 1:
            time.sleep(WORKER_START_INTERVAL_SEC)

    print(f"\n{n_workers} ワーカー起動、{len(tasks)} タスクを処理中...")

    # シャットダウン制御
    shutdown_requested = False

    def _shutdown(signum, frame):
        nonlocal shutdown_requested
        if shutdown_requested:
            return
        shutdown_requested = True
        print("\n停止シグナル受信。子ワーカーを終了中...")
        for i, p in enumerate(processes):
            if p.is_alive():
                p.terminate()
                print(f"  W{i + 1} を停止中...", end="")
        for i, p in enumerate(processes):
            p.join(timeout=10)
            if p.is_alive():
                p.kill()
                p.join(timeout=3)
                print(f"  W{i + 1} 強制終了")
            else:
                print(f"  W{i + 1} 停止完了")

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    for p in processes:
        p.join()

    elapsed = time.time() - start_time

    # 6. 最終チェック
    print("\n最終チェック中...")
    total_processed = 0
    total_skipped = 0
    total_errors = 0
    total_chunks = 0

    for i in range(n_workers):
        progress_file = os.path.join(PROGRESS_DIR, f"worker_{i}.json")
        if os.path.exists(progress_file):
            with open(progress_file) as f:
                data = json.load(f)
            status = data.get("status", "unknown")
            w_proc = data.get("processed", 0)
            w_skip = data.get("skipped", 0)
            w_err = data.get("errors", 0)
            w_chunks = data.get("chunks", 0)
            w_tasks = data.get("drives_done", 0)
            total_processed += w_proc
            total_skipped += w_skip
            total_errors += w_err
            total_chunks += w_chunks
            mark = "完了" if status == "done" else "中断"
            print(f"  W{i + 1}: {mark}  タスク:{w_tasks}"
                  f"  処理:{w_proc} スキップ:{w_skip}"
                  f"  エラー:{w_err} チャンク:{w_chunks}")
        else:
            print(f"  W{i + 1}: 未起動")

    alive = [p for p in processes if p.is_alive()]
    if alive:
        print(f"\n警告: {len(alive)} 個のワーカーがまだ生存中。強制終了します。")
        for p in alive:
            p.kill()
            p.join()

    # 7. 整合性チェック: 全タスクの処理漏れ確認 + DB 書き込み確認
    if not shutdown_requested:
        print("\n整合性チェック中...")
        from src.db import connect as db_connect

        check_conn = db_connect()
        check_cur = check_conn.cursor()

        # DB 内の全ファイルIDを一括取得（drive_file_id の先頭部分）
        check_cur.execute(
            "SELECT DISTINCT split_part(drive_file_id, '_chunk_', 1) FROM documents"
            " UNION "
            "SELECT DISTINCT split_part(drive_file_id, '_sheet_', 1) FROM documents"
        )
        db_file_ids = {row[0] for row in check_cur.fetchall()}

        all_ok = True
        for i, t in enumerate(tasks, 1):
            label = f"{t['label']}({t['part']})"
            indexable_ids = {
                f["id"] for f in t["files"]
                if f["mimeType"] not in SKIP_MIME_TYPES
            }
            found = len(indexable_ids & db_file_ids)
            expected = len(indexable_ids)
            missing = expected - found

            if missing > 0 and missing > expected * 0.1:
                print(f"  ({i}/{len(tasks)}) {label}: 警告 DB {found}/{expected}"
                      f" ({missing} 件不足)")
                all_ok = False
            else:
                print(f"  ({i}/{len(tasks)}) {label}: OK DB {found}/{expected}")

        check_cur.execute("SELECT COUNT(*) FROM documents")
        total_db_rows = check_cur.fetchone()[0]
        check_cur.close()
        check_conn.close()

        print(f"\n  DB 合計: {total_db_rows} レコード")
        if all_ok:
            print("  整合性チェック: OK")
        else:
            print("  整合性チェック: 一部警告あり（ログを確認してください）")

    print("\n" + "=" * 60)
    print("中断されました" if shutdown_requested else "完了")
    print(f"  処理済み: {total_processed}  スキップ: {total_skipped}"
          f"  エラー: {total_errors}")
    print(f"  チャンク: {total_chunks}")
    print(f"  所要時間: {elapsed / 60:.1f} 分"
          f"  ワーカー: {n_workers}  タスク: {len(tasks)}")
    print("=" * 60)


if __name__ == "__main__":
    main()
