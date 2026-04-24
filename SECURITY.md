# Security Policy

## サポート中のバージョン

現在セキュリティアップデートを受け取るバージョン:

| バージョン | サポート状況 |
|---|---|
| 0.2.x | ✅ サポート中 |
| 0.1.x | ❌ サポート終了 |

リリースノートは [CHANGELOG.md](./CHANGELOG.md) 参照。

## 脆弱性の報告方法

**GitHub 公開 Issue では報告しないでください。** 脆弱性詳細が修正前に一般公開されることを防ぐため、以下の方法でプライベートに連絡してください。

### 推奨: GitHub Private Vulnerability Reporting

1. [GitHub Security Advisories](https://github.com/GridWorldOrganization/GridWorldRAG/security/advisories/new) から新規レポート作成
2. 影響範囲、再現手順、想定被害を記載
3. メンテナが確認次第、連絡します

### 代替: メール

上記が使えない場合、リポジトリメンテナに直接連絡してください。
対応可能なメンテナは GitHub プロフィールから確認できます。

## 報告に含めてほしい情報

- 脆弱性の種類（例: SQL injection, SSRF, 認証バイパス, 機密情報漏洩）
- 影響を受けるバージョン / commit SHA
- 再現手順（PoC コード歓迎）
- 想定される影響（例: 任意のファイル読取、DB 破壊、トークン流出）
- 発見者の連絡先（公表可否の希望含む）

## 対応までのタイムライン（目安）

| 経過日数 | 対応内容 |
|---|---|
| 2 営業日以内 | 受領確認の返信 |
| 7 営業日以内 | 脆弱性の確認・影響度判定 |
| 30 日以内 | パッチリリース（Critical / High の場合） |
| 90 日以内 | 公表（報告者と調整の上） |

影響度の高い問題は優先して対応します。

## スコープ

以下は本プロジェクトのセキュリティスコープ **内**:

- `build_parallel.py`, `sync_rotate.py`, `src/*.py`, `gridworld-rag-mcp/server.py` のコード脆弱性
- 認証情報（`config.env`, `token.pickle`）の不適切な取扱い
- SQL injection, path traversal, SSRF, 任意コード実行
- MCP サーバー経由でのデータ漏洩経路
- Drive API のスコープ過剰要求

以下はスコープ **外**:

- ユーザー環境の OS / Homebrew / PostgreSQL 脆弱性（上流へ報告してください）
- Google Drive / Google Cloud 側の脆弱性（[Google VRP](https://bughunters.google.com/about/rules/6625378258649088) へ）
- 依存パッケージの脆弱性（Dependabot が自動検知・PR 作成）
- DoS（大量データ投入による OOM 等、意図的な攻撃シナリオが想定されない）

## 既知のセキュリティ考慮点

### 認証情報の扱い

- `config.env` に OAuth Client Secret と Client ID を平文保存します
- `token.pickle` に OAuth アクセストークン / リフレッシュトークンを保存します
- **両方とも `.gitignore` で除外済** — 絶対にコミットしないこと
- パーミッションは `0600` を推奨

### Drive API スコープ

本プロジェクトは `https://www.googleapis.com/auth/drive.readonly` のみを要求します。書き込み・削除は一切行いません。詳細は `src/drive_client.py` 参照。

### PostgreSQL 接続

ローカル接続 (`localhost`) のみ想定。リモート接続を有効にする場合は別途 TLS / pg_hba.conf の設定が必要です。本プロジェクトは DB 側のハードニング手順を提供していません。

### 権限情報の保持

`documents.permissions` JSONB カラムに Google Drive のファイル ACL を記録します。現時点では **検索フィルタには使用していない**（将来の機能拡張用）。閲覧制御は依然として Google Drive 側が担います。

## クレジット

報告いただいた方は、本人の希望があれば CHANGELOG および GitHub Security Advisory にクレジット記載します。
