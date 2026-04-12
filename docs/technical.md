# GridWorldRAG テクニカルリファレンス

## アーキテクチャ

```
Google Drive ──→ build_parallel.py ──→ PostgreSQL + pgvector
                 (build_single.py)              ↑
                 sync_rotate.py ──────────────┘ (ローテーション差分同期)
                                                ↑
Claude Code ←→ gridworld-rag-mcp/server.py ───┘ (MCP 検索)
```

## 処理フロー全体像

`./run_build.sh` 実行時の全処理を順序立てて記述する。

### Phase 0: 初期化（run_build.sh）

1. 前回の進捗ファイル (`/tmp/gridworldrag_progress/`) をクリア、ログファイルを初期化
2. venv を有効化
3. `config.env` から `PARALLEL_WORKERS` を読み取り
4. `shared_drives_whitelist.txt` からドライブ総数を算出
5. ヘッダ表示（ドライブ数、ワーカー数）
6. 開始時刻を `/tmp/gridworldrag_start_time` に記録
7. **Resume 判定**: `filelist.pkl` が存在すれば `[Y/n]` プロンプト。Y（デフォルト）で Phase 1 スキップ、N で再取得
8. Phase 1 → Phase 2 → Phase 3 を順次実行
9. Ctrl+C トラップを設定
10. モニター起動

### Phase 1-a: Google Drive 認証（build_parallel.py）

1. `token.pickle` が存在すれば読み込み
2. トークンが有効ならそのまま使用
3. 期限切れなら `_api_call_with_retry` 付きでリフレッシュ（5xx / connection reset をリトライ）
4. トークンがなければブラウザで OAuth 認証（初回のみ）
5. 認証情報を `_credentials` に保持（Sheets API でも再利用）
6. Drive API v3 のサービスオブジェクトを返す

### Phase 1-b: ファイル一覧取得（collect_file_lists, `--fetch-only`）

1. `config.env` の `INDEX_MY_DRIVE` / `INDEX_SHARED_DRIVES` を確認
2. `shared_drives_whitelist.txt` からホワイトリストを読み込み
3. 取得対象ドライブ数を算出
4. **サイズプローブ**（共有ドライブのみ）:
   - 各ドライブに `pageSize=1000` で1ページだけ API コール（`FETCH_THREADS` スレッドで並列）
   - `nextPageToken` の有無で「1000件超」かを推定
   - 推定結果を大きい順にソート（最大ドライブを先に処理開始 → 最後に小さいドライブが残る）
   - プローブ結果・ソート順は stderr 経由でログファイルに記録
5. マイドライブ（有効時）:
   - `list_files_in_drive(service, corpora="user")` で全ファイル取得
   - 自分がオーナーかつ共有ドライブ外のファイルのみに絞り込み
6. 共有ドライブ（有効時）:
   - ソート順に `FETCH_THREADS`（config.env、デフォルト 3）スレッドで並列取得
   - 各ドライブの取得完了時にログ記録（件数付き）
   - レート制限（429）時は10秒バックオフ→1回リトライ
7. 取得した全ファイルには以下のメタデータが含まれる:
   - `id`, `name`, `mimeType`, `modifiedTime`, `owners`, `webViewLink`, `driveId`
   - `permissions(emailAddress, role, type, displayName)`
8. 結果: `[(drive_name, drive_type, [files]), ...]`

#### run_build.sh 側の表示

- `--fetch-only` はフォアグラウンドで実行。stdout はターミナル、stderr はログファイル
- ファイル一覧取得完了後、結果を `/tmp/gridworldrag_filelist.pkl` に pickle 保存
- `--work-only` でワーカーフェーズに遷移

### Phase 2: タスク分割（`--split-only`、_split_into_tasks）

1. 各ドライブのファイル数を `TASK_SPLIT_THRESHOLD`（config.env、デフォルト 5000）と比較
2. 閾値以下 → 1 タスク（`part: "1/1"`）
3. 閾値超過 → `ceil(ファイル数 / 閾値)` 個のタスクに分割
   - 例: 33,817 件 / 5,000 = 7 タスク（`part: "1/7"` 〜 `"7/7"`）
4. 各タスクは `{label, drive_type, files, part, drive_total}` の dict
5. 全タスクを `multiprocessing.Queue` に投入

### 処理の呼称

| 呼称 | 内容 |
|------|------|
| ファイル一覧取得 | Google Drive API で全ドライブのファイルメタデータを収集（2スレッド並列） |
| インデックス構築 | テキスト抽出→チャンク分割→埋め込み生成→DB投入（マルチプロセス並列） |

### ワーカーのステータス遷移

```
停止中 → 起動中 → 待機中 → running → (待機中 → running)* → done
```

| ステータス | 説明 |
|-----------|------|
| 停止中 | プロセス未起動（順次起動の順番待ち） |
| 起動中 | プロセス起動済み、モデル・DB・認証をロード中 |
| 待機中 | ロード完了、次のタスク取得前 |
| running | タスク処理中 |
| レート制限待ち | Google API のレート制限（429）に到達、バックオフ中 |
| done | 全タスク完了 |

### Phase 3: ワーカー起動（`--work-only` モード）

1. `taskdata.pkl` からタスクメタ + ファイルデータを読み込み（旧フォーマットの場合は filelist.pkl から再分割）
2. `authenticate()` で認証（`_credentials` をセット、ワーカー内の Sheets API で必要）
3. `min(PARALLEL_WORKERS, タスク数)` 個のワーカープロセスを生成
4. 各ワーカーは `daemon=True`（親プロセス終了時に自動停止）
5. `atexit` ハンドラ登録（予期しない終了時にワーカーを kill → join でゾンビ防止）
6. SIGINT / SIGTERM ハンドラ設定:
   - 1回目 Ctrl+C: 全ワーカーに terminate → join(10s) → kill
   - 2回目 Ctrl+C: 即座に全ワーカー kill → join(3s) → sys.exit(1)
7. sentinel 監視スレッド起動（全ワーカー idle 検知で終了シグナル送信）
8. 各ワーカーの初期化（`_worker` → `_worker_main`）:
   - `SentenceTransformer(EMBEDDING_MODEL)` をロード（約 420MB / ワーカー）
   - `RecursiveCharacterTextSplitter` を生成
   - PostgreSQL に接続（`try/finally` で `conn.close()` を保護）
   - Google Drive API に認証（`httplib2.Http(timeout=N)` でソケットタイムアウト付き）
   - 起動時に PID をログ記録

### Phase 3-b: ファイル処理（_worker_main）

各ワーカーはキューからタスクを `get(timeout=5)` で取得（ポーリング待機）し、完了したら次のタスクを取る。sentinel (`None`) を受け取ると終了。

#### 6-1. タスクの取得

```python
while True:
    try:
        task = task_queue.get(timeout=5)  # ポーリング（ワークスティール対応）
    except queue.Empty:
        continue  # idle 状態で待機（sentinel 監視スレッドが終了を判定）
    if task is None:
        break
```

タスクは `file_start` / `file_end` フィールドを持ち、元タスクのファイルリストのスライスを参照する（ワークスティールのサブタスク対応）。

#### 6-1.5. Resume チェック

各ファイル処理の前に DB に既存データがあるかチェック:

```python
from src.db import file_exists

if file_exists(conn, file_info["id"]):
    return -1, 1, 0, 0  # resumeSkip（ログに resumeSkip と表示）
```

内部的には `drive_file_id LIKE 'file_id%' ESCAPE '\'`（`_escape_like_literal` で `_` を
エスケープ済み）。Drive file ID に含まれる `_` が LIKE のワイルドカードとして誤解釈される
バグがあり、`src/db.py` の helper 経由でのみアクセスする設計に統一した（v0.1.2）。

- btree インデックスのプレフィックス一致で高速
- 再実行時に処理済みファイルを瞬時スキップ

#### 6-2. ファイルごとの処理（_process_file）

ファイルの MIME タイプに応じて以下の処理を行う:

| MIME タイプ | 処理 |
|------------|------|
| `application/vnd.google-apps.folder` | `[フォルダ] ファイル名` として埋め込み → DB |
| `application/vnd.google-apps.spreadsheet` | Sheets API で全シート取得 → シート別にチャンク → 埋め込み → DB |
| `application/vnd.google-apps.document` | テキストとしてエクスポート → チャンク → 埋め込み → DB |
| `application/vnd.google-apps.presentation` | テキストとしてエクスポート → チャンク → 埋め込み → DB |
| `application/pdf` | pypdf でテキスト抽出 → チャンク → 埋め込み → DB |
| `image/*`（OCR 無効時） | `[画像] ファイル名` として埋め込み → DB |
| `image/*`（OCR 有効時） | ダウンロード → pytesseract で OCR → チャンク → 埋め込み → DB |
| `video/*` | `[動画] ファイル名` として埋め込み → DB |
| `audio/*` | `[音声] ファイル名` として埋め込み → DB |
| `text/*`, `application/json` 等 | ダウンロード → チャンク → 埋め込み → DB |
| `application/vnd.google-apps.shortcut` 等 | スキップ（DB にも入らない） |

#### 6-3. チャンク分割

- `RecursiveCharacterTextSplitter` を使用
- `CHUNK_SIZE=600`, `CHUNK_OVERLAP=120`（config.py で定義）

#### 6-4. 埋め込み生成

- `SentenceTransformer('multi-qa-mpnet-base-dot-v1')` で 768 次元ベクトルを生成
- バッチエンコード: `model.encode(chunks)` で複数チャンクを一括処理

#### 6-5. DB 投入

- `BATCH_SIZE=100` ごとに `insert_chunks()` を呼び出し
- UPSERT（`ON CONFLICT (drive_file_id) DO UPDATE`）で冪等性を確保
- NUL 文字 (`\x00`) は自動除去
- `insert_chunks()` 失敗時は `rollback()` → DB接続を再作成して処理継続（バッチロストはログ記録）
- カーソルは `try/finally` で確実に `close()`

#### 6-6. ワークスティール

50ファイルごとに `_count_idle_workers()` で他ワーカーの進捗 JSON をチェック。以下の条件を全て満たすとき発動:

- 残りファイル数 >= 100
- idle ワーカー数 >= 2
- 同一タスク内で未発動（`_redistributed` フラグで1回制限）

発動時:
1. 残りの80%をサブタスクとして `task_queue` に投入（最大4分割）
2. 自身は残り20%を継続処理
3. idle ワーカーがサブタスクを即座に取得

```
例: 5000件中250件目でワークスティール発動
  W2 継続: 250〜1200（950件）
  サブタスク1 → W1 が取得: 1200〜2150（950件）
  サブタスク2 → W3 が取得: 2150〜3100（950件）
  サブタスク3 → W5 が取得: 3100〜4050（950件）
  サブタスク4 → W7 が取得: 4050〜5000（950件）
```

##### タスクラベル記法

モニター表示とログに出てくる `drive(part)` 形式の凡例:

| ラベル | 意味 |
|---|---|
| `GW_LIB(1/1)` | 分割なしの通常タスク（ドライブ全体が1タスク） |
| `GW_LIB(2/3)` | 初期分割の 3 パート中 2 番目のパート。`TASK_SPLIT_THRESHOLD` を超えたドライブに適用される（`_split_into_tasks` で生成） |
| `GW_LIB(2/3再1)` | 上記 `(2/3)` タスクからワークスティールで再分配された 1 番目のサブタスク。`再` は「再分配（redistribution）」の略。`再N` は同時に切り出した複数サブタスクを区別する 1-indexed の連番 |

つまり `GW_LIB(2/2再1)` は「GW_LIB の 2/2 パートから work-stealing によって切り出された 1 番目のサブタスク」を意味する。同じ親タスクから切り出される場合は `(2/2再2)`, `(2/2再3)` ... と続き、最大 4 分割まで（`n_splits = min(idle_count, 4, remaining_count)`）。

モニターの `tasks:X/Y` の分母 Y は、このサブタスクも含めた実際のタスク数を反映する（初期値より大きければそちら）。

#### 6-6. 進捗更新

- ファイル処理ごとに `/tmp/gridworldrag_progress/worker_{n}.json` を更新
- `monitor.sh` が 2 秒ごとにこのファイルを読み取りモニター表示

### Phase 7: 最終チェック

全ワーカーの `join()` 完了後、以下を実行:

#### 7-1. ワーカー結果集計

- 各ワーカーの進捗ファイルから `processed`, `skipped`, `errors`, `chunks` を集計
- 完了 / 中断ステータスを表示

#### 7-2. 整合性チェック

1. DB から全ファイル ID を一括取得（`split_part` で `drive_file_id` から抽出）
2. タスクごとに、対象ファイル ID が DB に存在するか集合演算で確認
3. `SKIP_MIME_TYPES`（ショートカット等）は対象外として除外
4. 1 件でも欠落があれば警告表示（10% 許容ウィンドウは v0.1.2 で撤廃）
5. DB 全体のレコード数を表示

#### 7-3. 最終サマリ出力

```
============================================================
完了
  処理済み: 12000  スキップ: 35000  エラー: 3
  チャンク: 95000
  所要時間: 25.3 分  ワーカー: 8  タスク: 28
============================================================
```

### Phase 8: モニター終了（monitor.sh）

1. `build_parallel.py --work-only` のプロセス終了を検知
2. `Build complete!` を表示
3. ログの最終 10 行を表示

### シャットダウンフロー（Ctrl+C 時）

```
Ctrl+C
  ↓
run_build.sh: trap → kill $BUILD_PID
  ↓
build_parallel.py: SIGTERM → _shutdown()
  ↓
全ワーカーに terminate() 送信
  ↓
各ワーカーの join(timeout=10) を待つ
  ↓
10秒以内に終了しなければ kill()
  ↓
最終チェックはスキップ（shutdown_requested=True）
  ↓
「中断されました」+ サマリ出力
```

## ログ仕様

### ログファイル

| ファイル | 内容 |
|----------|------|
| `/tmp/gridworldrag_build.log` | `build_parallel.py` の全出力 |

### ログフォーマット

#### ファイル一覧取得フェーズ

ファイル一覧取得は `--fetch-only` で実行される。stdout はターミナル直接表示、stderr はログファイルに記録される。

**stdout（ターミナル表示）:**
```
Google Drive 認証中... 完了
3スレッドでファイル一覧を取得中 A:#1 B:#3 C:#5 (4/22完了)...
3スレッドでファイル一覧を取得中... 完了
  (1/22) GW_LIB_過去PJ実績 33817アイテム (24997ファイル 8820フォルダ)
  ...
合計: 22 ドライブ, 61810アイテム (49372ファイル 12438フォルダ)
```

**stderr（ログファイル）:**
```
[05:10:01] ドライブサイズ推定（大きい順）:
[05:10:01]   GW_LIB_過去PJ実績: 1000+
[05:10:01]   GW_PJ: 1000+
[05:10:01]   GW_LIB: 1000+
[05:10:01]   ...
[05:10:01]   GW_営業: 14
[05:10:15] 取得完了 #5 GW_マーケティング: 52件
[05:10:22] 取得完了 #3 GJ_AI: 148件
[05:12:45] 取得完了 #1 GW_LIB_過去PJ実績: 33817件  ← 最大ドライブ
```

プローブ失敗・レート制限リトライもログに記録される。Ctrl+C 時は `KeyboardInterrupt` をキャッチし、`ThreadPoolExecutor` を `cancel_futures=True` で即停止する。

#### タスク分割フェーズ

```
タスク分割: 21 ドライブ → 28 タスク (閾値: 5000)
  GW_PJ_DEV(1/1): 33 ファイル
  GW_LIB_過去PJ実績(1/7): 5000 ファイル
  GW_LIB_過去PJ実績(2/7): 5000 ファイル
  ...
```

#### ワーカー処理フェーズ

```
[11:30:00] W1 起動 (PID 12345)
[11:30:05] W2 起動 (PID 12346)
...
[11:32:05] W1 開始: GW_LIB_過去PJ実績(1/7) 5000 ファイル
[11:32:05] W1 1/5000 resumeSkip   0.0s  [GW_LIB_過去PJ実績] ファイル名  ← DB に既存
[11:32:06] W1 2/5000 skip         0.0s  [GW_LIB_過去PJ実績] archive.zip  ← ファイル種別
[11:32:07] W1 3/5000 ok           1.5s  [GW_LIB_過去PJ実績] 提案書       ← 新規処理
[11:32:08] W1 3/5000 err          0.3s  [GW_LIB_過去PJ実績] 壊れた.pdf  (エラー詳細)
[11:33:00] W1 再分配: 残り3800件を4サブタスクに分割、950件を継続  ← ワークスティール
[11:33:00] W1 DB挿入エラー（100件ロスト）: connection reset         ← DB エラー
[11:33:00] W1 DB接続復旧                                            ← 自動復旧
[11:35:42] W1 完了: GW_LIB_過去PJ実績(1/7)  処理:1200 スキップ:3500 エラー:0
```

- `resumeSkip` — DB に既存（前回処理済み）→ 瞬時スキップ
- `skip` — ファイル種別でスキップ（ZIP 等）
- `ok` — 新規処理完了
- `err` — 処理エラー（エラー詳細付き）
- `再分配` — ワークスティール発動（idle ワーカーにサブタスク分配）
- `DB接続復旧` — insert_chunks 失敗後の自動復旧

#### 最終チェックフェーズ

```
最終チェック中...
  W1: 完了  タスク:5  処理:3200 スキップ:4500 エラー:2 チャンク:25000
  W2: 完了  タスク:4  処理:2800 スキップ:4000 エラー:0 チャンク:18000
  ...

整合性チェック中...
  (1/28) GW_PJ_DEV(1/1): OK DB 27/27
  (2/28) GW_LIB_過去PJ実績(1/7): OK DB 4800/5000
  (15/28) GW_LIB_過去PJ実績(3/7): 警告 DB 3200/5000 (1800 件不足)
  ...

  DB 合計: 125000 レコード
  整合性チェック: OK
```

- ワーカーごとの累積値（処理/スキップ/エラー/チャンク）
- 整合性チェック: タスクごとに DB 内のファイル数を確認
  - `OK DB found/expected` — 欠落なし
  - `警告 DB found/expected (n 件不足)` — 1 件以上の欠落

#### シャットダウン時

```
停止シグナル受信。子ワーカーを終了中...
  W1 を停止中...  W1 停止完了
  W2 を停止中...  W2 停止完了
  ...

中断されました
  処理済み: 5000  スキップ: 8000  エラー: 2
  チャンク: 42000
  所要時間: 15.3 分  ワーカー: 8  タスク: 28
```

## 進捗ファイル仕様

### パス

`/tmp/gridworldrag_progress/worker_{n}.json`（n は 1 始まり）

### JSON フィールド

| フィールド | 型 | 説明 |
|-----------|-----|------|
| `worker_id` | int | ワーカー ID（1 始まり） |
| `drive` | str | 現在処理中のドライブ名 |
| `drive_type` | str | `"共有"` or `"マイ"` |
| `current` | int | タスク内の処理済みファイル数 |
| `total` | int | タスク内の総ファイル数 |
| `processed` | int | DB 投入済みファイル数（ワーカー累積） |
| `skipped` | int | スキップ数（ワーカー累積） |
| `errors` | int | エラー数（ワーカー累積） |
| `chunks` | int | チャンク数（ワーカー累積） |
| `status` | str | `"waiting"` / `"running"` / `"done"` |
| `drives_done` | int | 完了タスク数 |
| `drives_total` | int | 全タスク総数（全ワーカー共通の値） |
| `part` | str | タスク分割情報（`"1/1"` or `"2/7"` 等） |
| `drive_total` | int | ドライブ全体のファイル数 |

### status 遷移

```
ready → running → (rate_limited → running)* → ready → running → ... → done
```

### モニター更新間隔

`config.env` の `MONITOR_INTERVAL_MS`（デフォルト: 1000ms）で設定可能。

### モニターの画面制御方式

相対カーソルアップ方式（`\033[NA]` + 前回印刷行数追跡）を採用。macOS Terminal.app / iTerm2 で動作確認済み。

- 初回: そのまま出力（`LAST_LINE_COUNT=0`）
- 更新: 前回印刷した行数分だけ `\033[NA]` でカーソルを上に戻し → 各行を `\033[K`（行クリア）付きで上書き
- 前回より行数が減った場合は余剰行もクリア

以下の方式は不採用:

| 方式 | 不採用理由 |
|------|-----------|
| `clear` + `echo` | ファイル一覧表示が消える。画面が毎回全クリアされて目障り |
| `tput sc/rc` | Python サブプロセスが ESC 7 を発行すると保存位置が上書きされ、20-30% の確率で表示崩壊 |
| `\033[H`（カーソル先頭移動） | 画面最上部に移動してしまい、ファイル一覧の上にモニターを上書きする |

## タスク分割ロジック

1. 各ドライブのファイル数を `TASK_SPLIT_THRESHOLD`（config.env）と比較
2. 閾値以下 → 1 タスク（`part: "1/1"`）
3. 閾値超過 → `ceil(ファイル数 / 閾値)` タスクに分割（`part: "1/7"` 等）
4. タスクをラウンドロビン順でキューに投入:
   - 全ドライブの `(1/n)` → 全ドライブの `(2/n)` → ... の順
   - これにより各ドライブの最初の分割が優先処理され、全ドライブに均等に着手する
   - 手が空いたワーカーから `(2/n)` 以降に着手
5. **センチネルは即時投入しない**: 全ワーカー idle + `pending_tasks` アトミックカウンタ == 0 を sentinel 監視スレッドが検知してから `None` を投入
6. ワーカーはキューからタスクを `get(timeout=5)` でポーリング取得、`None` で終了

### タスクキューのデータ分離

`multiprocessing.Queue` は内部的にパイプを使用しており、大きなデータ（数千ファイルのメタデータ）を直接入れるとパイプが詰まり、後続ワーカーがタスクを取得できなくなる。

これを回避するため、タスクデータを2層に分離している:

- **キュー**: 軽量なメタデータのみ（`task_id`, `label`, `part` 等、数百バイト/タスク）
- **pickle ファイル** (`/tmp/gridworldrag_taskdata.pkl`): ファイルメタデータ本体（数MB）

各ワーカーは起動時に pickle ファイルを読み込み、キューから受け取った `task_id` で対応するファイルリストを参照する。

## API レート制限対策

Google Drive API / Sheets API の呼び出しには `_api_call_with_retry()` でリトライを適用。

- 対象エラー: `429`, `rate limit`, `quota`, HTTP 5xx (`500`/`502`/`503`/`504`), `connection reset`/`aborted`
- リトライ: 最大 `API_MAX_RETRIES` 回（config.env、デフォルト 6）
- 待機時間: `API_BASE_DELAY_SEC * 2^attempt + random(0, API_BASE_DELAY_SEC)` 秒（エクスポネンシャルバックオフ）
- レート制限中はワーカーのステータスが `rate_limited`（レート制限待ち）に変わる

### Sheets API セマフォ

Sheets API は `ReadRequestsPerMinutePerUser: 60` の制限がある。8ワーカーが同時にスプレッドシートを処理すると即座に制限に到達する。

`multiprocessing.Semaphore(2)` でプロセス間セマフォを実装し、Sheets API（`extract_spreadsheet_sheets`）への同時アクセスを最大2ワーカーに制限している。

## DB スキーマ

### documents テーブル

| カラム | 型 | 説明 |
|--------|-----|------|
| `id` | SERIAL PK | 自動採番 |
| `drive_file_id` | TEXT UNIQUE | `{file_id}_chunk_{n}` or `{file_id}_sheet_{gid}_chunk_{n}` |
| `title` | TEXT | ファイル名 |
| `content` | TEXT | チャンクテキスト |
| `chunk_index` | INTEGER | チャンク番号 |
| `owner` | TEXT | オーナーのメールアドレス |
| `source_url` | TEXT | Google Drive の URL |
| `file_type` | TEXT | MIME タイプ |
| `drive_modified_at` | TIMESTAMPTZ | Drive 上の更新日時 |
| `embedding` | VECTOR(768) | 埋め込みベクトル |
| `sheet_gid` | TEXT | スプレッドシートのシート ID |
| `sheet_name` | TEXT | スプレッドシートのシート名 |
| `permissions` | JSONB | アクセス権限 |
| `partial_content` | BOOLEAN | テキスト抽出が部分的な場合 TRUE（PDF タイムアウト等） |
| `folder_path` | TEXT | ドライブ内のフォルダパス（`Drive / Folder / Sub`） |
| `created_at` | TIMESTAMPTZ | DB 投入日時 |

### permissions JSONB 構造

```json
[
  {"email": "user@example.com", "name": "田中", "role": "writer", "type": "user"},
  {"email": "example.com", "name": "", "role": "reader", "type": "domain"}
]

```

### drive_file_id の命名規則

| ファイル種別 | 形式 |
|-------------|------|
| 通常ファイル | `{drive_file_id}_chunk_{chunk_index}` |
| スプレッドシート | `{drive_file_id}_sheet_{sheet_gid}_chunk_{chunk_index}` |

### ワーカーステータス一覧

モニター（`monitor.sh`）および進捗 JSON (`/tmp/gridworldrag_progress/worker_N.json`) の `status` 値。

| status 値 | モニター表示 | 説明 |
|---|---|---|
| `running` | `XX% ドライブ名` | 通常稼働中 |
| `loading` | `起動中` | モデル・認証の初期化中 |
| `ready` | `待機中` | タスク待ち（次タスクの開始前） |
| `done` | `完了` | 全担当タスク完了 |
| `rate_limited` | `レート制限待ち` | API レート制限で一時停止 |
| `rate_limited_running` | `XX%(レート制限待ち)` | 処理継続しつつレート制限待ち |
| （JSON なし） | `停止中` | ワーカープロセス未起動 |

### content のプレフィックス

| プレフィックス | 説明 |
|---------------|------|
| `[フォルダ] ファイル名` | フォルダ（名前のみ） |
| `[画像] ファイル名` | 画像（OCR 無効時） |
| `[画像] ファイル名\nOCRテキスト` | 画像（OCR 有効時） |
| `[動画] ファイル名` | 動画（名前のみ） |
| `[音声] ファイル名` | 音声（名前のみ） |
| `[スキャンPDF] ファイル名` | テキスト抽出不可の PDF |
| `[PDF] ファイル名` | pypdf 未インストール時 |
| `[シート: シート名]\nデータ` | スプレッドシートのシート |
