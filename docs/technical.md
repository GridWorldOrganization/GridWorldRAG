# GridWorldRAG テクニカルリファレンス

## アーキテクチャ

```
Google Drive ──→ build_parallel.py ──→ PostgreSQL + pgvector
                 (build_single.py)              ↑
                 watcher.py ───────────────────┘ (差分更新・予定)
                                                ↑
Claude Code ←→ mcp_server.py ─────────────────┘ (検索・予定)
```

## 処理フロー全体像

`./run_build.sh` 実行時の全処理を順序立てて記述する。

### Phase 1: 初期化（run_build.sh）

1. 前回の進捗ファイル (`/tmp/gridworldrag_progress/`) をクリア、ログファイルを初期化
2. venv を有効化
3. `config.env` から `PARALLEL_WORKERS` を読み取り
4. `shared_drives_whitelist.txt` からドライブ総数を算出
5. ヘッダ表示（ドライブ数、ワーカー数）
6. 開始時刻を `/tmp/gridworldrag_start_time` に記録
7. `build_parallel.py --fetch-only` をフォアグラウンド実行（stdout に直接表示）
8. `build_parallel.py --work-only` をバックグラウンド起動（stdout+stderr → ログファイル）
9. Ctrl+C トラップを設定
10. モニター起動

### Phase 2: Google Drive 認証（build_parallel.py）

1. `token.pickle` が存在すれば読み込み
2. トークンが有効ならそのまま使用
3. 期限切れならリフレッシュ
4. トークンがなければブラウザで OAuth 認証（初回のみ）
5. 認証情報を `_credentials` に保持（Sheets API でも再利用）
6. Drive API v3 のサービスオブジェクトを返す

### Phase 3: ファイル一覧取得（collect_file_lists）

1. `config.env` の `INDEX_MY_DRIVE` / `INDEX_SHARED_DRIVES` を確認
2. `shared_drives_whitelist.txt` からホワイトリストを読み込み
3. 取得対象ドライブ数を算出
4. マイドライブ（有効時）:
   - `list_files_in_drive(service, corpora="user")` で全ファイル取得
   - 自分がオーナーかつ共有ドライブ外のファイルのみに絞り込み
5. 共有ドライブ（有効時）:
   - ホワイトリストの各ドライブ ID に対して `list_files_in_drive(service, drive_id=id)` を実行
   - `PROGRESS n/n` をログ出力（run_build.sh が表示に利用）
6. 取得した全ファイルには以下のメタデータが含まれる:
   - `id`, `name`, `mimeType`, `modifiedTime`, `owners`, `webViewLink`, `driveId`
   - `permissions(emailAddress, role, type, displayName)`
7. ファイル一覧を `FILELIST_START` / `FILELIST_END` ブロックでログ出力
8. 結果: `[(drive_name, drive_type, [files]), ...]`

#### run_build.sh 側の表示

- `--fetch-only` はフォアグラウンドで実行されるため、stdout がそのまま画面に表示される
- ファイル一覧取得完了後、結果を `/tmp/gridworldrag_filelist.pkl` に pickle 保存
- `--work-only` でワーカーフェーズに遷移

### Phase 4: タスク分割（_split_into_tasks）

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

### Phase 5: ワーカー起動（--work-only モード）

1. pickle からファイル一覧を読み込み
2. `authenticate()` で認証（`_credentials` をセット、ワーカー内の Sheets API で必要）
3. `min(PARALLEL_WORKERS, タスク数)` 個のワーカープロセスを生成
4. 各ワーカーは `daemon=True`（親プロセス終了時に自動停止）
5. SIGINT / SIGTERM ハンドラを設定（Ctrl+C で全ワーカーを terminate → join）
6. 各ワーカーの初期化（`_worker` → `_worker_main`）:
   - `SentenceTransformer(EMBEDDING_MODEL)` をロード（約 420MB / ワーカー）
   - `RecursiveCharacterTextSplitter` を生成
   - PostgreSQL に接続（`try/finally` で `conn.close()` を保護）
   - Google Drive API に認証（`httplib2.Http(timeout=N)` でソケットタイムアウト付き）

### Phase 6: ファイル処理（_worker → _process_file）

各ワーカーはキューからタスクを1つ取得し、完了したら次のタスクを取る。

#### 6-1. タスクの取得

```
while True:
    task = task_queue.get()  # ブロッキング取得（センチネル None で終了）
    if task is None:
        break
```

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
4. 10% 以上の欠落がある場合は警告表示
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

ファイル一覧取得は `--fetch-only` で stdout に直接出力される（ログファイルには入らない）。
2スレッドで並列取得し、ドットアニメーション付きでインプレース表示する。

```
Google Drive 認証中... 完了
2スレッドでファイル一覧を取得中 #3,#9 (4/21完了)...  ← インプレース更新
2スレッドでファイル一覧を取得中... 完了
  (1/21) GW_PJ_DEV 27 ファイル 6 フォルダ
  ...
合計: 21 ドライブ, 49307 ファイル 12415 フォルダ
```

Ctrl+C 時は `KeyboardInterrupt` をキャッチし、`ThreadPoolExecutor` を `cancel_futures=True` で即停止する。

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
[11:32:05] W1 開始: GW_LIB_過去PJ実績(1/7) 5000 ファイル
[11:35:42] W1 完了: GW_LIB_過去PJ実績(1/7)  処理:1200 スキップ:3500 エラー:0
[11:35:42] W1 開始: GW_PJ_Secret(1/1) 135 ファイル
[11:35:50] W1 完了: GW_PJ_Secret(1/1)  処理:45 スキップ:80 エラー:0
```

- `[HH:MM:SS] Wn 開始:` — ワーカー n がタスクを取得して処理開始
- `[HH:MM:SS] Wn 完了:` — タスク完了。処理/スキップ/エラーはそのタスク単体の値

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
  - `OK DB found/expected` — 10% 未満の欠落は OK
  - `警告 DB found/expected (n 件不足)` — 10% 以上の欠落

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

`/tmp/gridworldrag_progress/worker_{n}.json`（n は 0 始まり）

### JSON フィールド

| フィールド | 型 | 説明 |
|-----------|-----|------|
| `worker_id` | int | ワーカー ID（0 始まり） |
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

`tput sc/rc`（カーソル位置の保存/復元）方式を採用。macOS Terminal.app / iTerm2 で動作確認済み。

- 初回: `TOTAL_LINES` 行分の空行で表示領域を確保 → カーソルを先頭に戻して `tput sc` で位置保存
- 更新: `tput rc` で保存位置に戻り → 各行を `\033[K`（行クリア）付きで上書き
- ちらつきなし、カーソル位置ずれなし

以下の方式は不採用:

| 方式 | 不採用理由 |
|------|-----------|
| `clear` + `echo` | ファイル一覧表示が消える。画面が毎回全クリアされて目障り |
| `\033[nA`（相対カーソル移動） | ファイル一覧（20行超）の後にモニターが開始すると、カーソル上移動がファイル一覧領域に突入し表示が壊れる |
| `\033[H`（カーソル先頭移動） | 画面最上部に移動してしまい、ファイル一覧の上にモニターを上書きする |

## タスク分割ロジック

1. 各ドライブのファイル数を `TASK_SPLIT_THRESHOLD`（config.env）と比較
2. 閾値以下 → 1 タスク（`part: "1/1"`）
3. 閾値超過 → `ceil(ファイル数 / 閾値)` タスクに分割（`part: "1/7"` 等）
4. タスクをラウンドロビン順でキューに投入:
   - 全ドライブの `(1/n)` → 全ドライブの `(2/n)` → ... の順
   - これにより各ドライブの最初の分割が優先処理され、全ドライブに均等に着手する
   - 手が空いたワーカーから `(2/n)` 以降に着手
5. キュー末尾にワーカー数分の `None`（センチネル）を追加
6. ワーカーはキューからタスクを1つずつ取得（ブロッキング `get()`）、`None` で終了

### タスクキューのデータ分離

`multiprocessing.Queue` は内部的にパイプを使用しており、大きなデータ（数千ファイルのメタデータ）を直接入れるとパイプが詰まり、後続ワーカーがタスクを取得できなくなる。

これを回避するため、タスクデータを2層に分離している:

- **キュー**: 軽量なメタデータのみ（`task_id`, `label`, `part` 等、数百バイト/タスク）
- **pickle ファイル** (`/tmp/gridworldrag_taskdata.pkl`): ファイルメタデータ本体（数MB）

各ワーカーは起動時に pickle ファイルを読み込み、キューから受け取った `task_id` で対応するファイルリストを参照する。

## API レート制限対策

Google Drive API / Sheets API の呼び出しには `_api_call_with_retry()` でリトライを適用。

- 対象エラー: `429`, `rate limit`, `quota`
- リトライ: 最大 5 回
- 待機時間: `2^attempt + random(0,1)` 秒（エクスポネンシャルバックオフ）
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
