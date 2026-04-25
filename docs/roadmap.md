# Roadmap

GridWorldRAG の今後の方向性。本プロジェクトは **Windows 11 ネイティブの単一系統** で開発されている。

## 現行: v0.6.1 開発中（Windows 版）

- 🎯 **Mode**: 常駐 daemon（`rag_daemon.py`）+ FastAPI Web UI + Electron ミニモニタ
- 📅 **Latest**: v0.6.1
- 🔧 **状態**: アクティブ開発中
- 📋 **特徴**: per-FD スキーマ分離、GPU 埋め込み、AWS serverless、マルチユーザー認証、NSSM Windows Service

## アーカイブ: Mac 版 v0.2.1（開発中止）

初期 v0.1〜v0.2 系は macOS (Apple Silicon) 上の `build_parallel.py` + `sync_rotate.py` + launchd というバッチ + cron 構成だった。v0.2.1 を最終リリースとして **Mac 版は開発中止**。後継として常駐 daemon 型を Windows ネイティブで実装する方針に切り替え、Windows 版に一本化された。

- 📅 **Final**: v0.2.1（2026-04-21）
- 🔧 **サポート**: ❌ 終了。バグ修正・機能追加なし。
- 📦 **コード**: 履歴・`docs/mac-resident-daemon.md`（当初構想）参照

## 計画中（Windows 版）

### 直近の改善候補

| 項目 | 状態 | 備考 |
|---|---|---|
| ミニモニタに開始/停止トグルボタン | ⏳ 設計済み | `daemon_config.paused` フラグ + `/api/daemon/{pause,resume}` |
| HNSW index 切替オプション | ⏳ 候補 | IVFFlat → HNSW で検索速度向上 |
| Reranker 追加 | ⏳ 候補 | 小型 cross-encoder で精度向上 |
| MCP HTTP 化（Cowork 遠隔対応） | ⏳ 構想 | 現状 stdio。HTTP に拡張して LAN/SaaS 経由で利用 |
| マルチユーザー認証 | ⏳ 構想 | per-FD スキーマ × ユーザー権限 |
| AWS serverless 化 | ⏳ 構想 | Drive 監視 / 埋め込みを Lambda 等にオフロード |
| 英語ドキュメント（README.en.md） | ⏳ 候補 | 国際化対応 |

> 詳細マイルストーンは GitHub Milestones を参照。

## 不採用 / 保留

| 項目 | 理由 |
|---|---|
| ChromaDB へのマイグレーション | SQL 複合フィルタの価値が大きいため pgvector を継続 |
| Webhook 受信（ngrok 等） | Drive Webhook の 7 日失効・不安定要因あり。Changes API ポーリングで十分なリアルタイム性 |
| Google Drive 以外の source（Notion, Slack 等） | スコープ拡大につき別プロジェクトで検討 |
| LLM 回答生成の内蔵 | Claude Code を通じた MCP 利用が前提。LLM は呼び出し側が選択 |
| Mac / Linux サポート | Mac 版は v0.2.1 で開発中止。Windows ネイティブに集中 |

## 参考リンク

- [mac-resident-daemon.md](./mac-resident-daemon.md) — Mac 版時代の常駐 daemon 構想（履歴用）
- [CHANGELOG.md](../CHANGELOG.md) — 過去のリリース
- [GitHub Milestones](https://github.com/GridWorldOrganization/GridWorldRAG/milestones) — 実装トラッキング
