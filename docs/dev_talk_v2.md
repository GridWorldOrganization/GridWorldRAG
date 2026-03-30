# 開発記録 v2（2026-03-30〜31）

SSL クラッシュ修正から始まり、例外ハードニング、ワークスティール、resume 対応まで。
実際のビルド実行で発覚した問題と、その場で考案した解決手法を記録する。

---

## 1. SSL SEGFAULT / SIGABRT の根本原因と解決

### 発覚

db3 ビルド中にワーカープロセスがクラッシュ。macOS のクラッシュレポートから原因を特定。

### 1回目: SIGSEGV（NULL deref）

```
Thread 27 Crashed:
  tls_get_more_records + 244  ← NULL pointer dereference
  SSL_read_ex
  _ssl__SSLSocket_read_impl
```

**原因**: ファイルダウンロードのタイムアウト制御に daemon スレッドを使っていた。タイムアウト後、放棄された daemon スレッドが SSL ソケットを掴んだまま残存。次のファイル処理で同じ `service` オブジェクト（= 同じ SSL コネクションプール）を再利用 → 2スレッドが同じ SSL ソケットを同時アクセス → SEGFAULT。

**暫定対策**: タイムアウト後に `service = authenticate()` で再作成。

### 2回目: SIGABRT（double-free）

暫定対策後も別のクラッシュが発生。

```
Thread 25 Crashed:
  free_small_botch + 40       ← malloc が double-free を検出
  tls_release_read_buffer + 56
  SSL_read_ex
```

**原因**: `service` を再作成しても、放棄スレッドが旧 SSL バッファを掴んだまま。新スレッドがバッファを解放した後に旧スレッドも解放 → double-free。daemon スレッド方式そのものが根本的に危険。

### 根本解決: daemon スレッド廃止 → httplib2 ソケットタイムアウト

```python
# Before: daemon スレッドでタイムアウト（SSL double-free の原因）
t = threading.Thread(target=_run, daemon=True)
t.start()
t.join(timeout=15)
if t.is_alive():
    raise _DownloadTimeoutError()  # スレッドは放棄、SSL は掴まれたまま

# After: httplib2 のソケットタイムアウト（SSL は1スレッドのみ）
authorized_http = google_auth_httplib2.AuthorizedHttp(
    creds, http=httplib2.Http(timeout=DRIVE_DOWNLOAD_TIMEOUT_SEC))
service = build("drive", "v3", http=authorized_http)
```

タイムアウトはソケットレベルで発生するため、`socket.timeout` 例外が Python に返り、スレッド競合は起きない。

### 環境要因: psycopg2-binary の重複 libssl

クラッシュレポートのバイナリイメージに **2つの libssl.3.dylib** が存在:
1. Homebrew 版（Python `_ssl` モジュール）
2. psycopg2-binary バンドル版（DB接続）

同一プロセスに 2つの OpenSSL インスタンス → グローバル SSL 状態が干渉。

**解決**: `psycopg2-binary` → `psycopg2`（ソースビルド）に切り替え。Homebrew の libssl を共有し、重複を排除。

```bash
export PATH="/opt/homebrew/opt/postgresql@17/bin:/opt/homebrew/opt/openssl@3/bin:$PATH"
pip install --no-binary :all: psycopg2==2.9.11
```

---

## 2. 例外ハードニング

コードレビューで発見した問題を一括修正。

### db.py: カーソルリーク・未コミットトランザクション

**Before**: カーソルが例外時に閉じられず、トランザクションも放置。

```python
cur = conn.cursor()
cur.execute(...)  # ← ここで例外 → cur.close() に到達しない
conn.commit()
cur.close()
```

**After**: 全関数で `try/finally`、書き込み系は `rollback()`。

```python
cur = conn.cursor()
try:
    cur.execute(...)
    conn.commit()
except Exception:
    conn.rollback()
    raise
finally:
    cur.close()
```

### build_parallel.py: ワーカーの conn.close() 保護

`_worker` を `_worker` + `_worker_main` に分離。`conn.close()` を `try/finally` で保護し、致命的エラーでも DB 接続がリークしない。

### insert_chunks の DB 接続復旧

```python
try:
    insert_chunks(conn, batch)
except Exception as _db_ex:
    print(f"DB挿入エラー（{len(batch)}件ロスト）: {_db_ex}")
    conn.close()
    conn = connect()  # 接続復旧して処理継続
```

### pyflakes による事前検知

`_worker_main` に `insert_chunks` と `connect` の import を入れ忘れ → 実行時 `NameError`。

```
NameError: name 'insert_chunks' is not defined
```

`pyflakes` で事前に検知可能だった:

```
build_parallel.py:191:5: 'src.db.insert_chunks' imported but unused
```

`_worker` で import したが `_worker_main`（別関数）では見えない。Python は動的言語なので `py_compile` では検出不可、`pyflakes` なら検出可能。

---

## 3. Resume 対応（途中再開）

### 問題

ビルド中断後の再実行で、処理済みファイルを全て再処理 → 無駄。

### 解決

`_process_file` の先頭で DB に既存データがあるかチェック:

```python
if conn is not None:
    cur = conn.cursor()
    try:
        cur.execute("SELECT 1 FROM documents WHERE drive_file_id LIKE %s LIMIT 1",
                    (f"{file_id}_%",))
        if cur.fetchone():
            return 0, 1, 0, 0  # スキップ
    finally:
        cur.close()
```

- UPSERT + resume チェックで冪等な再実行を保証
- `LIKE 'prefix%'` は btree インデックスで高速
- 30,000 件処理済みの状態で再実行 → 未処理分だけ処理

---

## 4. ワークスティール

### 問題

7ワーカー中5つが完了、残り W2・W4 だけが大きなタスク（各5000件）を処理中。推定完了: **13時間**。

### 解決: 忙しいワーカーが遊んでいるワーカーを検知し、残りを再分配

```
W2 が 5000件中 250件目を処理中...
  ↓ 50件ごとに _count_idle_workers() チェック
  「W1,W3,W5,W6,W7 が idle だ」(5人遊んでる)
  ↓
  残り 4750 件のうち:
    950件 → W2 が継続処理
    3800件 → 4サブタスクに分割してキューに投入
  ↓
  W1,W3,W5,W7 が即座にサブタスクを取得
  ↓
  5ワーカーで並列処理
```

### アーキテクチャ変更

1. **sentinel を即時投入しない** → ワーカーは `get(timeout=5)` でポーリング待機
2. **監視スレッド** が全ワーカー idle を検知 → sentinel 投入で終了
3. タスクに `file_start`/`file_end` を追加 → サブタスクは元タスクのスライスを参照
4. `_redistributed` フラグで1タスク1回に制限（指数増殖防止）

### 踏んだバグ: for ループでの変数差し替え（131% 問題）

再分配後に `files` と `task_size` を差し替えたが、Python の `for fi, file_info in enumerate(files)` は **開始時にイテレータを固定** するため、差し替えが効かずループが元のリスト長で継続 → 進捗が 131% に。

**解決**: `for` → `while fi < task_size` + `fi += 1` に変更。

---

## 5. ファイル一覧取得の最適化

### 問題

2スレッドで22ドライブのファイル一覧を取得。最後に巨大ドライブ（GW_LIB_過去PJ実績 33,817件）が残ると、1スレッドだけが数分間働く。

### 解決 1: 大きいドライブを先に処理

各ドライブに `pageSize=1000` で1ページだけプローブ（22回、各1秒以下）:

```python
_probe = service.files().list(
    driveId=drive_id, corpora="drive", q="trashed = false",
    fields="nextPageToken, files(id)", pageSize=1000, ...
).execute()
_est = 10000 if "nextPageToken" in _probe else len(_probe.get("files", []))
```

`nextPageToken` の有無で「1000件超」かを即判定。大きい順にソートしてスレッドに投入。

### 解決 2: スレッド数を設定可能に（FETCH_THREADS）

```
# config.env
FETCH_THREADS=3
```

2 → 3スレッド化で並列度向上。ドライブ名ラベルも `A:# B:#` → `A:# B:# C:#` に動的対応。

### ルートフォルダ分割は不可能

「1ドライブをフォルダ単位で分割して並列取得」は検討したが、Drive API の制約で不可:
- `'folder_id' in parents` は直接の子のみ（再帰なし）
- 再帰するとフォルダ数分のAPIコール（GW_LIB_過去PJ実績: 8,820回 vs 現在の34回）
- 現在の一括取得（`corpora=drive`）が API 的に最速
