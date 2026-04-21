# Mac 常駐デーモン型アーキテクチャ (v0.3.0 → v1.0.0)

このドキュメントは、GridWorldRAG を v0.2.x の「バッチ + launchd cron」型から
「Mac 常駐デーモン + GUI モニター + MCP」型へ段階的に切り替える設計をまとめる。

- **対象バージョン**: v0.3.0 〜 v1.0.0（破壊的変更枠）
- **原方針（Spreadsheet 260421）**:
  1. 従来のバッチ型は取りやめる
  2. Mac 上での常駐デーモンを作り、バックグラウンドで動かす
  3. Mac 上のクライアント・ステータスモニターアプリで状況をモニタリング
  4. クライアントを停止してもバックグラウンドは動き続ける
  5. RAG 制作時は CPU を食うが、差分更新フェーズではほぼ食わない
  6. claude code はローカル Mac に対しオリジナル MCP 経由で通信する
  7. v0.3.0 から順次、最終的に v1.0.0 にする

- **関連図面（Google Drive）**:
  [GridWorldRAG_v0.3.0-v1.0.0_design_260421.drawio](https://drive.google.com/file/d/1wtHJXVxNaOtU5jd3MfDfTZFhqE36yKOZ/view)
  — 10 ページ構成で表紙・比較・全体構成・Daemon 内部・IPC・CEO/Eng Review・
  ロードマップ・リスク・実装チェックリストを収録。

---

## 1. 現状 (v0.2.1) と課題

### 現状アーキテクチャ

```
build_parallel.py  (手動・一回完結のバッチ, 3 フェーズ)
launchd (5 分 StartInterval) → sync_rotate.py (完了したら終了)
gridworld-rag-mcp (Claude Code ⇄ PostgreSQL 直結)
PostgreSQL + pgvector
```

### 課題

| # | 問題 | 影響 |
|---|------|------|
| 1 | バッチ起動時に CPU 100% | ユーザ体感が悪い |
| 2 | 5 分 cron は最小粒度 | 準リアルタイムが成立しない |
| 3 | モニターは tail 常時必要 | リモート/別セッションから状況が見えない |
| 4 | プロセスが消えるので状態照会不可 | 「今なにしてる？」の窓口がない |
| 5 | 5〜10 秒の埋め込みモデル再ロード | sync 毎に課金されるウォームアップ |
| 6 | イベント通知の push 起点がない | Telegram 等の即時通知が起こしにくい |

---

## 2. 新アーキテクチャ (v1.0.0 ターゲット)

### 2.1 コンポーネント

```
 ┌────────────────────────────────────────────────────────────────┐
 │ 外部                                                          │
 │   Google Drive  │  Google OAuth  │  Telegram Bot  │ Claude Code│
 └───────┬────────────────┬────────────────┬───────────┬─────────┘
         │                │                │           │
 ┌───────┼────────────────┼────────────────┼───────────┼─────────┐
 │ Mac ローカル (常駐)                                             │
 │                                                                │
 │  launchd LaunchAgent  (KeepAlive=true, RunAtLoad)              │
 │    ↓ spawn                                                     │
 │  ┌────────────── gridworldrag-daemon (常駐) ─────────────┐    │
 │  │  Scheduler / State Machine   Changes API Poller       │    │
 │  │  Worker Pool (dynamic N)     Event Bus (pub/sub)      │    │
 │  │  IPC Server (Unix socket, JSON-RPC 2.0)               │    │
 │  │  Embedding Service (singleton, warm)                  │    │
 │  └──┬────────────────────────────────┬──────────────────┘    │
 │     │ writer                         │ read/write            │
 │     ▼                                ▼                       │
 │  PostgreSQL + pgvector           SQLite (state.db)           │
 │  (chunks / embedding)           (phase / events / meta)      │
 │                                                                │
 │  ┌────────────┐  ┌───────────────────┐  ┌─────────────────┐ │
 │  │ GUI menubar │  │ gridworldragctl   │  │ gridworld-rag   │ │
 │  │ (monitor)   │  │ (CLI)             │  │ -mcp (stdio)    │ │
 │  └──────┬──────┘  └─────────┬─────────┘  └────────┬────────┘ │
 │         │ IPC                │ IPC                 │ IPC+PG   │
 └─────────┴────────────────────┴─────────────────────┴──────────┘
```

### 2.2 責務分離

| コンポーネント | 役割 | プロセス寿命 |
|----------------|------|--------------|
| **gridworldrag-daemon** | 唯一の writer、ライフサイクル・ジョブ管理、Changes API polling、Event 発行 | 常駐 |
| **gridworldrag-monitor (GUI)** | ステータス可視化、操作 UI | 任意（停止可） |
| **gridworldragctl (CLI)** | 診断・操作・tail | 一時 |
| **gridworld-rag-mcp** | Claude Code 向けセマンティック検索 MCP | 一時（Claude Code 再起動で再生成） |
| **PostgreSQL + pgvector** | 検索対象の chunks / embedding | 常駐（OS サービス） |
| **SQLite state.db** | daemon の phase / events / meta | daemon に付随 |

### 2.3 SSOT（単一情報源）

- **索引データ**: PostgreSQL（chunks, embedding, metadata）
- **daemon 状態**: SQLite state.db（phase, events, tokens, failed_files）
- **設定**: `config.env`（既存）＋ env var 追加

daemon は SQLite state.db の phase 行の更新後に event を発行する、という
順序を守る。クラッシュ時の復元は state.db から行う。

---

## 3. IPC プロトコル仕様

### 3.1 トランスポート

- Unix socket
- デフォルトパス: `~/Library/Application Support/gridworldrag/daemon.sock`
- パーミッション: `0700`（current user only）
- 環境変数で上書き: `GRIDWORLDRAG_DAEMON_SOCK`

### 3.2 メッセージフォーマット

newline-delimited UTF-8 JSON（1 行 1 メッセージ）:

**リクエスト**
```json
{"jsonrpc":"2.0","id":1,"method":"status","params":{}}
```

**レスポンス (成功)**
```json
{"jsonrpc":"2.0","id":1,"result":{"phase":"READY", ...}}
```

**レスポンス (エラー)**
```json
{"jsonrpc":"2.0","id":1,"error":{"code":-32601,"message":"Method not found"}}
```

**サーバプッシュイベント (subscribe 後)**
```json
{"jsonrpc":"2.0","method":"event","params":{"channel":"phase_change","payload":{...}}}
```

### 3.3 メソッド一覧（v1.0.0 時点）

| method | params | result | 備考 |
|--------|--------|--------|------|
| `status` | — | `{phase, since, reason, version, protocol_version, ...}` | 状態取得 |
| `subscribe` | `{channels?: [str]}` | `{sub_id}` + 以降イベント push | ストリーミング |
| `pause` | — | `{ok, new_phase}` | 一時停止 |
| `resume` | — | `{ok, new_phase}` | 再開 |
| `reindex` | `{drive_id?, full?}` | `{job_id}` | 再インデックス依頼 |
| `cancel_job` | `{job_id}` | `{ok}` | ジョブキャンセル |
| `stats` | — | `{per_drive, per_filetype, db_size, ...}` | 統計 |
| `recent_events` | `{since_id?, channels?, limit?}` | `{events: [...]}` | ログ参照 |
| `recent_changes` | `{n}` | `[{ts, drive_id, file_id, op, title}]` | 最近の変更 |
| `encode` | `{texts: str | [str]}` | `{vectors, dim}` | 埋め込み計算 |
| `warmup` | — | `{ok, loaded, model}` | モデルロード |
| `shutdown` | `{force?: bool}` | `{ok}` | 停止要求 |

### 3.4 エラーコード

JSON-RPC 2.0 標準に準拠：

- `-32700` Parse error
- `-32600` Invalid Request
- `-32601` Method not found
- `-32602` Invalid params
- `-32603` Internal error

---

## 4. 状態マシン

```
         ┌──────┐
         │ BOOT │
         └───┬──┘
             ▼
        ┌──────────┐
        │ CHECK_DB │ (スキーマ / 接続検証)
        └───┬──────┘
            │ ok
            ▼
      ┌─────────────────┐
      │ BOOTSTRAPPING   │ (未インデックスドライブ検出)
      └───┬─────────┬───┘
          │ 未完了有│ 全完了
          ▼         ▼
  ┌────────────────┐ ┌──────────────────┐
  │ INDEX_BUILDING │→│   MAINTAINING    │ ←→  PAUSED
  │ (CPU 100%)     │ │ (polling 30s,    │
  │                │ │  CPU ほぼ 0%)    │
  └───────┬────────┘ └─────────┬────────┘
          │ err                 │ SIGTERM
          ▼                     ▼
      ┌───────┐             ┌──────────┐
      │ ERROR │─recovered→  │ SHUTDOWN │
      └───────┘             └──────────┘
```

- **BOOT**: 起動直後、state.db 初期化
- **CHECK_DB**: PostgreSQL / pgvector 接続・スキーマ確認
- **BOOTSTRAPPING**: 未インデックスのドライブを検出
- **INDEX_BUILDING**: ワーカー N 並列稼働、CPU 100%
- **MAINTAINING**: polling のみ、CPU 1% 未満
- **PAUSED**: ユーザ操作またはバッテリー駆動で停止
- **ERROR**: disk_full / auth_lost 等、復旧可能なら MAINTAINING へ
- **SHUTDOWN**: SIGTERM でクリーン終了

---

## 5. モジュール構成 (`src/daemon/`)

| ファイル | 責務 |
|----------|------|
| `main.py` | asyncio event loop、signal handler、supervisor |
| `scheduler.py` | 状態マシン、phase 遷移、job queue |
| `workers.py` | multiprocessing pool、動的スケール |
| `poller.py` | Changes API（sync_rotate から移植） |
| `ipc.py` | Unix socket + JSON-RPC 2.0 server |
| `events.py` | pub/sub event bus、ring buffer |
| `embedding.py` | SentenceTransformer シングルトン |
| `state.py` | SQLite state.db リポジトリ |

補助:

| ファイル | 責務 |
|----------|------|
| `src/cli/gridworldragctl.py` | CLI: status/pause/resume/reindex/tail |
| `src/gui/monitor_rumps.py` (v0.6.0) | menubar GUI (Python + rumps) |
| `src/gui/monitor_swiftui/` (v0.8.0+) | ネイティブ GUI |

---

## 6. ロードマップ v0.3.0 → v1.0.0

### v0.3.0  Daemon 骨格
- `src/daemon/` 新設（main/scheduler/ipc/events/state/embedding）
- asyncio + Unix socket IPC サーバ（最小: status/shutdown/encode/warmup/recent_events/subscribe）
- SQLite state.db スキーマ追加
- launchd plist 差し替え（StartInterval → KeepAlive）
- 埋め込みモデルは daemon 内でロード、`encode` を IPC 公開
- `gridworldragctl` 最小版
- 機能的には何もしない骨格。既存 sync_rotate は並行で動かしたまま

**DoD**: launchd 起動後、`gridworldragctl status` が返る。

### v0.4.0  Sync を daemon に取り込む
- `sync_rotate.py` のロジックを `daemon/poller.py` に移管
- 旧 sync_rotate + 旧 launchd を無効化（移行スクリプト同梱）
- poller: 30 秒間隔、変更ゼロは即スキップ
- `failed_files` / disk_full 既存資産を state.db へ移設
- イベント: `file_processed` / `drive_synced` / `error` 発火
- `gridworldragctl tail` で event 購読

**DoD**: 従来の 5 分 cron と同等の sync が daemon 内で回る。

### v0.5.0  Build を daemon に取り込む
- `build_parallel.py` の 3 フェーズを `scheduler.py` に統合
- phase 遷移: `BOOT → CHECK_DB → BOOTSTRAPPING → INDEX_BUILDING → MAINTAINING`
- ワーカー動的スケール（phase により 0 〜 N）
- resume prompt → daemon 内で自動判定
- `/tmp` preflight / Sheets semaphore 既存資産を移植

**DoD**: 新規 Mac で clean install → 自動で全ドライブ index 完了。ユーザ介入ゼロ（whitelist のみ）。

### v0.6.0  CLI 拡充 + menubar GUI 最小版
- `gridworldragctl pause/resume/reindex/tail` 実装
- `gridworldrag-monitor` (rumps) メニューバー常駐
  - 状態アイコン（green/yellow/red）
  - 最終 sync / pending / phase の 1 行表示
  - メニュー: Open logs / Pause / Resume / Quit GUI
- GUI 終了 ≠ daemon 終了 を明示 UI

**DoD**: GUI quit した後も sync が動き続ける録画テスト成功。

### v0.7.0  MCP リファクタ
- `gridworld-rag-mcp` を daemon IPC ベースに接続
- `encode` は daemon に委譲（MCP 側はモデルを持たない）
- `stats`/`recent_changes` は live データを daemon から取得
- `search` は従来通り PG 直結（レイテンシ優先）
- MCP 起動時の初期化が 10 秒 → 1 秒未満

**DoD**: Claude Code から search/stats が違和感なく動作。旧 MCP からの移行ドキュメント完成。

### v0.8.0 〜 v0.9.0  GUI 本体 + 運用自動化
- v0.8.0 フル GUI: 詳細パネル（workers / file progress / errors / DB stats）
- v0.9.0 ハードニング: SwiftUI 再実装、バッテリー検知で自動 pause、Telegram 通知強化、`gridworldragctl doctor`（診断コマンド）

### v1.0.0  パッケージ + 公開
- brew tap (`GridWorldOrganization/gridworldrag`)
- `.pkg` 署名/公証（GUI + daemon 同梱）
- 既存 v0.2.x ユーザ向け `migrate` コマンド
- README / QUICKSTART 全面書き換え
- バージョン 1.0 ブランチ保護、LTS 化

---

## 7. CEO Review 要点（前提の問い直し）

### Q1. そもそも常駐デーモンは本当に必要か？
**回答**: 必要。論点は
(a) 埋め込みモデル再ロード（5〜10 秒）の消滅、
(b) GUI からの pause/resume / reindex 即時反映、
(c) イベント通知の push 起点、
(d) 準リアルタイム sync（30 秒〜）。
cron 粒度の 5 分は GUI 的に体感が悪く、「今なにしてる？」の問い合わせ先が無いことが本質的欠点。

### Q2. GUI を作る価値は？ CLI だけで足りないか？
**回答**: 自分（オーナー）しか使わないなら CLI で十分。
ただし将来 GridWorld 社内展開を見据えると、非エンジニアの役員にもステータスを示す必要が出る。
**妥協案**: v0.6.0 で「メニューバー・アイコンだけ」に絞り（緑/黄/赤 のドット + 最終同期時刻 + 数値 1 行）、全画面 GUI は v0.8.0 以降に後ろ倒し。

### Q3. Mac 専用で良いか？ Windows はどうする？
**回答**: v0.2.1 で Windows Task Scheduler 連携を入れたが、v1.0.0 までは Mac を主戦場とする。
Windows は v1.1.0 以降の課題。Windows 版 daemon = Windows Service（sc.exe）、IPC は named pipe。
プラットフォーム抽象化層（`ipc/backend.py`）を今のうちに切っておく。

### Q4. 10 倍プロダクトは何か？
**回答**: 「自分の Drive を Claude Code / API に貸し出す LAN サービス」。
社内の別 Mac から HTTP で検索できるようにすれば、ライセンス 1 本で家族/チームが全員使える。
Unix socket の上に localhost HTTP API を重ねる（同一プロトコル、トランスポートだけ差替え）を意識しておく。

### CEO 的最終判断

- ✅ **SCOPE HOLD**: 「Mac 常駐 + GUI + MCP」の三点は維持。足さない（v1.0.0 は Mac のみ）。
- ✅ **EXPAND**: メニューバー GUI を先に出す（v0.6.0）。フル画面 GUI は後ろ倒し。
- ✅ **EXPAND**: IPC プロトコル設計時に HTTP 変換可能性を確保（v1.x LAN API の布石）。
- ✅ **HOLD**: Telegram 通知は既に資産あり。デーモン化でイベント源が明確になるため活用強化。
- ❌ **捨てる**: 「GUI だけで全てできる」野望。運用は CLI > GUI で揃える。
- ❌ **捨てる**: Windows サポートは v1.0.0 範囲外。抽象層だけ残し実装は v1.1.0 以降。

### Definition of Done (v1.0.0)

- 「GUI を閉じても 3 日後に戻ってきたら最新」が実測で確認できる
- sync レイテンシ: 変更発生から DB 反映まで中央値 60 秒以内
- CPU: MAINTAINING 中の 5 分平均が 1% 未満
- 既存 v0.2.x ユーザ向け自動移行スクリプト同梱

---

## 8. Eng Review 要点

### アーキテクチャ論点

**A1. asyncio vs threading vs multiprocessing のハイブリッド**
- IPC / poller / event bus は asyncio
- CPU ワーカーは multiprocessing（GIL 回避）
- bridge は `ProcessPoolExecutor` + `run_in_executor`
- SentenceTransformer の推論は子プロセスで実行し、メインプロセスはモデルを持たず OS ページキャッシュだけ利用

**A2. DB 接続プール**
- daemon 側 writer pool: 最大 = worker 数 + 2
- MCP 側 reader pool: 別プロセス、最大 4
- 書き込みと読み取りで接続を明確に分離（pgbouncer 不要）

**A3. Unix Socket パーミッション**
- 0700 + owner=current user 強制
- `SO_PEERCRED` で接続元 UID 検証し、不一致は即閉じる

**A4. 状態の単一情報源 (SSOT)**
- SQLite（state.db）= 権威
- daemon の in-memory は起動時に state.db から復元
- phase 遷移は必ず SQLite 行更新 → event 発行の順

**A5. Changes API トークン管理**
- 既存 `sync_state.rotate_token_<drive_id>` の形式を温存
- 失敗時のロールバックはトークン更新前にジョブ成功確認（at-least-once semantics）

**A6. 埋め込みモデルのバージョン管理**
- state.db に `embedding_model_name` 列を持ち、daemon 起動時に不一致なら ERROR 状態 + GUI に通知
- モデル変更は原則 DB TRUNCATE + 再構築（v0.2.1 の轍）

**A7. MCP の冷キャッシュ問題**
- MCP は短命プロセスなので、起動毎にモデルロードすると initial search が遅い
- **対策**: MCP は Daemon IPC の `encode(q)` を叩き、モデルは daemon 側 1 本にする

### エッジケース / 失敗モード

| # | シナリオ | 対応 |
|---|----------|------|
| E1 | Daemon 再起動中に GUI が繋がる | GUI は自動 retry (backoff 1s → 30s)。subscribe 応答に phase と最終 event id を返し、GUI はギャップを検出 |
| E2 | 埋め込みモデル更新後の不一致 | 起動時に state.db の model_name を検証、不一致なら ERROR 停止し `gridworldragctl migrate --truncate` を案内 |
| E3 | PG ディスクフル | 既存 `DiskFullHalt` を継承し daemon は PAUSED へ（プロセスは落とさない） |
| E4 | OAuth トークン失効（24 時間） | refresh を指数バックオフ、3 回失敗で ERROR + Telegram 通知 |
| E5 | GUI (subscriber) が反応しない | pub バッファは ring buffer で DROP、イベントは state.db のログから replay |
| E6 | SIGTERM 受信 | ワーカー graceful shutdown (30s) → 強制 kill。書き込みトランザクション中は完了を待つ |
| E7 | 電源断 | launchd KeepAlive で再起動、state.db / PG から復元 |
| E8 | 巨大ファイル (数百MB PDF) で worker OOM | 子プロセス単位でメモリ制限（`resource.setrlimit RLIMIT_AS`）+ タイムアウト 10 分。失敗は `failed_files` へ |
| E9 | Unix Socket ファイルの残骸 | 起動時に socket が存在しても listen 不能なら削除して作り直し |
| E10 | GUI/CLI からの偽 shutdown | 操作ログ (who/when) を state.db に追記し事後追跡可能に |

### 未決事項（要決定）

- **O1. GUI 実装言語** — SwiftUI（ネイティブ）vs Python+rumps（速い）vs Tauri（将来 Windows も）。
  **推奨**: v0.6.0 は rumps、v0.8.0 で SwiftUI リプレース。
- **O2. 設定再読み込み方法** — SIGHUP vs IPC `reload()`。**推奨**: 両方サポート。
- **O3. パッケージング** — brew/homebrew formula にまとめる vs `setup.sh` 手動。**推奨**: 自作 tap を v0.9.0 で追加。

---

## 9. リスクマトリクス

| # | リスク | 影響 | 発生確率 | 緩和策 |
|---|--------|------|----------|--------|
| R1 | 長寿命プロセスのメモリリーク | 高 | 中 | 毎日 RSS を監視、閾値超でセルフ restart。ワーカーは `ProcessPoolExecutor` で定期再起動（`max_tasks_per_child` 相当） |
| R2 | IPC プロトコル後方互換破壊 | 中 | 高 | `protocol_version` フィールドを全メッセージに載せる。v0.3.0 で 1、v1.0.0 で 2 に上げる。CLI/GUI は `server_version` を見て UI を gate |
| R3 | 新 daemon と旧 sync_rotate の同時実行 | 高 | 中 | Changes API トークンの重複消費 / DB 書き込み競合防止のため、v0.3.0 起動時に launchd の旧 plist を unload 必須化（postinstall script）。lockfile キーを同名にして排他 |
| R4 | GUI 実装で macOS 署名/公証コスト | 中 | 高 | v0.6.0 は rumps（Python, 署名不要）で妥協。v1.0.0 時に Apple Developer ID + notarize を予算化 |
| R5 | DB スキーマ変更必要性が後から発覚 | 中 | 中 | `schema.sql` のバージョン列を追加、daemon 起動時に必要なら自動 ALTER。破壊的変更は v1.0.0 で一回にまとめる |

---

## 10. 移行計画 (v0.2.x → v1.0.0)

`scripts/migrate_to_v1.sh` で自動化する以下の手順:

```bash
# Step 1. 旧 launchd unload
launchctl unload ~/Library/LaunchAgents/co.gridworld.gridworldrag.sync.plist

# Step 2. 旧 lockfile 掃除
rm -f /tmp/gridworldrag_rotate.lock

# Step 3. パッケージ更新
git pull && ./setup.sh   # または pip install -U gridworldrag

# Step 4. 状態 DB マイグレーション
#   sync_state から state.db へ token 引き継ぎ、embedding_model_name 記録
gridworldragctl migrate

# Step 5. 新 launchd load
launchctl load ~/Library/LaunchAgents/co.gridworld.gridworldrag.daemon.plist

# Step 6. 動作確認
gridworldragctl status    # phase が MAINTAINING になることを確認

# Step 7. Claude Code の MCP 設定は変更不要（endpoint は同じ stdio）
```

Step 3 と 4 の間で DB TRUNCATE が不要なことを担保する設計（v0.2.1 で embedding モデル切替済みのため）。

### ロールバック計画

各 `v0.x.0` は git tag を確実に打つ。重大問題発生時は:

```bash
launchctl unload ~/Library/LaunchAgents/co.gridworld.gridworldrag.daemon.plist
git checkout v0.2.1
launchctl load ~/Library/LaunchAgents/co.gridworld.gridworldrag.sync.plist
```

で 10 分以内に旧環境に戻れる。state.db は常に残し、新環境へ再移行する時は
既存の新 state.db を破棄して再生成。**PG データには触らないため検索は常に継続可能**。

---

## 11. 実装チェックリスト（v0.3.0 キックオフ用）

### 新規ファイル/ディレクトリ

```
src/daemon/__init__.py
src/daemon/main.py                 # エントリポイント (python -m src.daemon.main)
src/daemon/scheduler.py            # state machine + job queue
src/daemon/workers.py              # multiprocessing pool
src/daemon/poller.py               # Drive Changes API (sync_rotate から移植)
src/daemon/ipc.py                  # Unix socket + JSON-RPC 2.0 server
src/daemon/events.py               # pub/sub event bus
src/daemon/embedding.py            # SentenceTransformer シングルトン
src/daemon/state.py                # SQLite state.db リポジトリ
src/cli/__init__.py
src/cli/gridworldragctl.py         # CLI エントリ
src/gui/__init__.py
src/gui/monitor_rumps.py           # v0.6.0 rumps menubar
src/gui/monitor_swiftui/           # v0.8.0 以降 (別リポ予定)
launchd/co.gridworld.gridworldrag.daemon.plist
scripts/migrate_to_v1.sh
schema_state.sql                   # state.db スキーマ
tests/test_daemon_scheduler.py, test_ipc.py, test_events.py など 20+ ファイル追加
```

### 既存ファイル変更

| ファイル | 変更内容 | 対象バージョン |
|----------|----------|----------------|
| `src/config.py` | daemon 用設定キー追加（`GRIDWORLDRAG_DAEMON_SOCK`, `POLL_INTERVAL_SEC`, `MAX_WORKERS_BOOT`） | v0.3.0 |
| `src/db.py` | writer/reader pool 分離、`insert_chunks` の `embed_model` フィールド追加 | v0.4.0 |
| `src/drive_client.py` | poller からの呼び出しに対応（stateless 化） | v0.4.0 |
| `src/indexer.py` | そのまま再利用、ただしワーカープロセス境界を意識 | v0.5.0 |
| `gridworld-rag-mcp/server.py` | search は PG 直結のまま、stats/recent_changes は daemon IPC に切替 | v0.7.0 |
| `build_parallel.py` | warning 出して `gridworldragctl start-build` に redirect | v0.5.0 |
| `sync_rotate.py` | warning 出して `gridworldragctl status` に redirect | v0.4.0 |
| `monitor.sh` | `gridworldragctl tail` に統合 | v0.6.0 |
| `launchd/co.gridworld.gridworldrag.sync.plist` | 削除 | v0.4.0 |
| `schema.sql` | `embedding_model` 列追加（ALTER TABLE） | v0.4.0 |
| `README.md` / `QUICKSTART.md` / `CLAUDE.md` | 全面書き換え | v1.0.0 |

### v1.0.0 Definition of Done（総合）

- [ ] `launchctl load` で起動、reboot 後も自動復帰
- [ ] GUI 終了しても daemon が継続（録画で実証）
- [ ] sync レイテンシ中央値 60 秒以内（変更 〜 DB 反映）
- [ ] MAINTAINING 中の CPU 使用率 5 分平均 1% 未満
- [ ] 埋め込みモデル再ロード 0 回（起動後 24 時間）
- [ ] `gridworldragctl doctor` が健全性チェック 20 項目を pass
- [ ] 全テストスイート（目標 150+ テスト）pass
- [ ] v0.2.x から `migrate` スクリプト 1 発で移行成功
- [ ] brew tap からワンライナーで install
- [ ] README + QUICKSTART + ARCHITECTURE ドキュメント完備

---

## 12. 参考リンク

- Spreadsheet (要件): [【PJ】GridWorldRAG 260421](https://docs.google.com/spreadsheets/d/1sBIiJqjzBdO1wCmbi_4BGGR-mN4EilGQb9FvCuo-b4A/edit?gid=846309224)
- Draw.io 設計図: [GridWorldRAG_v0.3.0-v1.0.0_design_260421](https://drive.google.com/file/d/1wtHJXVxNaOtU5jd3MfDfTZFhqE36yKOZ/view)
- 現行リリース: [v0.2.1](https://github.com/GridWorldOrganization/GridWorldRAG/releases/tag/v0.2.1)
