# Roadmap

GridWorldRAG の今後の方向性。

本リポジトリには **Mac 版**（GridWorldRAG v0.2.x）と **Windows 版**（WinServerRAG v0.6.x）の 2 系統がある。Windows 版は Mac 版の [常駐デーモン型アーキテクチャ](./mac-resident-daemon.md) 構想を先行実装した形になっている。

## Windows 版（WinServerRAG）: v0.6.1 開発中

- 🎯 **Mode**: 常駐 daemon（`rag_daemon.py`）+ FastAPI Web UI + Electron ミニモニタ
- 📅 **Latest**: v0.6.1
- 🔧 **状態**: アクティブ開発中
- 📋 **特徴**: per-FD スキーマ分離、GPU 埋め込み、AWS serverless、マルチユーザー認証、NSSM Windows Service

## Mac 版（GridWorldRAG）: v0.2.1 安定版

- 🎯 **Mode**: バッチ + launchd/Task Scheduler cron
- 📅 **Released**: 2026-04-21
- 🔧 **サポート**: ✅ セキュリティ修正 + 小改善
- 📋 **想定用途**: 単独 Mac / Windows 環境での個人・小規模チーム利用

## 計画中

### v0.2.x（継続的改善）

| 項目 | 状態 | 備考 |
|---|---|---|
| ドキュメント整備（world-class 水準） | ⏳ 進行中 | README バッジ / TOC / Architecture / SECURITY 等 |
| Docker サポート | ⏳ 候補 | ローカル Homebrew 依存を軽減 |
| CI 拡充（tests workflow + CodeQL） | ⏳ 進行中 | 現在は lint のみ |
| HNSW index 切替オプション | ⏳ 候補 | IVFFlat → HNSW で検索速度向上 |
| Reranker 追加 | ⏳ 候補 | 小型 cross-encoder で精度向上 |
| 英語ドキュメント（README.en.md） | ⏳ 候補 | 国際化対応 |

### v0.3.0（Daemon 骨格） — 次マイルストーン

- `src/daemon/` ディレクトリ新設
- `asyncio` ベースの常駐プロセス骨格
- Unix socket IPC（JSON-RPC 2.0）
- SQLite state.db スキーマ追加
- launchd plist を `StartInterval` → `KeepAlive` に差し替え
- 埋め込みモデルを daemon 内でロード（MCP 冷キャッシュ問題の解消）
- `gridworldragctl` CLI 最小版

**DoD**: `launchctl load` 後に `gridworldragctl status` が応答。既存 sync_rotate は並行で動かしたまま。

### v0.4.0（Sync を daemon に取り込む）

- `sync_rotate.py` のロジックを `daemon/poller.py` に移管
- polling 間隔を 5 分 → 30 秒に短縮可能に
- `failed_files` / disk_full 既存資産を state.db へ移設
- イベント: `file_processed` / `drive_synced` / `error` 発火

### v0.5.0（Build を daemon に取り込む）

- `build_parallel.py` の 3 フェーズを `scheduler.py` に統合
- `BOOT → CHECK_DB → BOOTSTRAPPING → INDEX_BUILDING → MAINTAINING` 状態マシン
- ワーカー動的スケール
- resume prompt を daemon 内で自動判定

### v0.6.0（CLI + menubar GUI）

- `gridworldragctl pause/resume/reindex/tail`
- macOS メニューバー常駐（rumps, Python）
- GUI 終了 ≠ daemon 終了 を明示

### v0.7.0（MCP リファクタ）

- MCP を daemon IPC 経由に接続
- `encode` は daemon に委譲（MCP はモデルを持たない → 起動 10 秒 → 1 秒未満）
- `search` は PG 直結のまま（レイテンシ優先）

### v0.8.0 〜 v0.9.0（GUI 本体 + 運用自動化）

- フル GUI（詳細パネル: workers / file progress / errors / DB stats）
- SwiftUI 再実装（任意）
- バッテリー検知で自動 pause
- `gridworldragctl doctor`（診断コマンド）

### v1.0.0（パッケージ + 公開）

- `brew tap GridWorldOrganization/gridworldrag` でワンライナー install
- `.pkg` 署名/公証（GUI + daemon 同梱）
- v0.2.x ユーザ向け `migrate` コマンド
- LTS 化

### v1.1.0 以降（候補）

- Windows ネイティブ Service（sc.exe）対応
- LAN サービス化（localhost HTTP API で家族・チーム共有）
- Linux systemd 公式サポート
- 英語ドキュメント全般

## 不採用 / 保留

| 項目 | 理由 |
|---|---|
| ChromaDB へのマイグレーション | SQL 複合フィルタの価値が大きいため pgvector を継続 |
| Webhook 受信（ngrok 等） | Mac スリープ・7 日失効等の不安定要因。Changes API ポーリングで十分なリアルタイム性 |
| Google Drive 以外の source（Notion, Slack 等） | スコープ拡大につき別プロジェクトで検討 |
| LLM 回答生成の内蔵 | Claude Code を通じた MCP 利用が前提。LLM は呼び出し側が選択 |

## 参考リンク

- [mac-resident-daemon.md](./mac-resident-daemon.md) — v0.3.0 〜 v1.0.0 の詳細設計
- [CHANGELOG.md](../CHANGELOG.md) — 過去のリリース
- [GitHub Milestones](https://github.com/GridWorldOrganization/GridWorldRAG/milestones) — 実装トラッキング
