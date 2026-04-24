# サポートマトリクス

動作確認済み / 非推奨 / 未サポート の組合せ一覧。

## OS サポート

| OS | バージョン | 状態 | 常駐実行 | 備考 |
|---|---|---|---|---|
| macOS (Apple Silicon) | 14.x (Sonoma) | ✅ 公式サポート | launchd | M1/M2/M3 推奨 |
| macOS (Apple Silicon) | 15.x (Sequoia) | ✅ 公式サポート | launchd | 2026 最新 |
| macOS (Intel) | 13.x+ | ⚠️ 動作するが非推奨 | launchd | PyTorch 2.2.2 が上限 |
| Windows 10 | - | ❌ ネイティブ不可 | Task Scheduler + WSL | WSL2 必須 |
| Windows 11 | - | ✅ 公式サポート（WSL2 経由） | Task Scheduler + WSL | WSL2 必須 |
| Ubuntu | 22.04 LTS | ✅ 動作確認済 | 自前 systemd | 常駐は自前設定 |
| Ubuntu | 24.04 LTS | ✅ 動作確認済 | 自前 systemd | 常駐は自前設定 |
| Debian | 12 | ⚠️ 未検証（たぶん動く） | 自前 systemd | apt パッケージ互換 |
| Fedora | 最新 | ⚠️ 未検証 | 自前 systemd | dnf で要調整 |
| その他 Linux | - | ⚠️ 未検証 | - | Issue で報告歓迎 |

## Python バージョン

| Python | 状態 | 備考 |
|---|---|---|
| 3.10 | ❌ 未サポート | `langchain-text-splitters` 等の要件 |
| 3.11 | ⚠️ 動作するかも | 公式テストなし |
| 3.12 | ✅ 公式サポート | 推奨 |
| 3.13 | ⚠️ 未検証 | 依存パッケージの対応待ち |

## PostgreSQL バージョン

| PostgreSQL | pgvector | 状態 | 備考 |
|---|---|---|---|
| 14 | 0.8+ | ⚠️ 動作するかも | 公式テストなし |
| 15 | 0.8+ | ⚠️ 動作するかも | 公式テストなし |
| 16 | 0.8+ | ✅ 動作確認済 | 推奨上位互換 |
| 17 | 0.8+ | ✅ 公式サポート | 推奨 |

## pgvector バージョン

| pgvector | 状態 | インデックス | 備考 |
|---|---|---|---|
| 0.6.x | ❌ 非対応 | IVFFlat のみ | 古すぎる |
| 0.7.x | ⚠️ 動作するかも | IVFFlat / HNSW | 公式テストなし |
| 0.8.x | ✅ 公式サポート | IVFFlat / HNSW | 推奨 |

## 埋め込みモデル

| モデル | 次元 | 状態 | 備考 |
|---|---|---|---|
| `multi-qa-mpnet-base-dot-v1` | 768 | ❌ 廃止（v0.2.1） | 日本語 tokenizer 不対応 |
| `paraphrase-multilingual-mpnet-base-v2` | 768 | ✅ 現行デフォルト（v0.2.1+） | 50+ 言語対応 |
| `multi-qa-MiniLM-L6-cos-v1` | 384 | ⚠️ schema 変更必要 | 英語特化、軽量 |
| `BAAI/bge-m3` | 1024 | ⚠️ schema 変更必要 | 多言語・高性能 |
| カスタム 768 次元モデル | 768 | ⚠️ 非公式 | ユーザー責任 |

## Google Drive 環境

| 要素 | 状態 | 備考 |
|---|---|---|
| 個人 Google アカウント | ✅ 対応 | マイドライブ + 共有ファイル |
| Google Workspace アカウント | ✅ 対応 | 共有ドライブ含む |
| OAuth デスクトップアプリ | ✅ 推奨 | インストール手順で説明 |
| OAuth Web アプリ | ⚠️ 未検証 | リダイレクト URI 調整必要 |
| サービスアカウント | ⚠️ 未検証 | Drive の共有設定に依存 |

### ファイル種別対応

詳細は [README.md](../README.md#ファイル種別ごとの対応状況) 参照。

## Claude Code

| Claude Code | MCP サポート | 状態 |
|---|---|---|
| `> 1.0` | 公式 MCP | ✅ 対応 |
| `< 1.0` | - | ❌ 非対応 |

## ネットワーク要件

| 項目 | 要件 |
|---|---|
| 初回ビルド時の帯域 | 下り 100 Mbps 推奨 |
| 差分同期 | 下り 10 Mbps で十分 |
| Drive API 接続先 | `*.googleapis.com` (HTTPS 443) |
| OAuth コールバック | ローカルホスト（インターネット接続不要） |
| MCP | stdio（ネットワーク不要、Claude Code と同一マシン） |

## ハードウェア推奨

### 最小構成

- CPU: 2 コア以上
- RAM: 8 GB（埋め込みモデル 420 MB × ワーカー数 + PG）
- ディスク: 5 GB 空き（初回ビルド用 `/tmp` + DB）
- ネットワーク: 10 Mbps+

### 推奨構成

- CPU: 8 コア以上（`PARALLEL_WORKERS=8` を活かせる）
- RAM: 16 GB+
- ディスク: SSD、20 GB 空き
- ネットワーク: 100 Mbps+

### 大規模（100k+ ファイル）

- CPU: 12 コア+
- RAM: 32 GB+
- ディスク: NVMe SSD、50 GB+
- PostgreSQL 専用のリソース（他アプリと共有しない）

## 検証環境の報告

環境ごとの動作確認結果を共有したい場合、[Discussions](https://github.com/GridWorldOrganization/GridWorldRAG/discussions) に「動作報告: [OS] [版] [コンピュータ]」スレッドを立ててください。
