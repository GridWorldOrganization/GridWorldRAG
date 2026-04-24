# Summary

<!-- この PR が何をするか 1-3 文で -->

## Related Issue

<!-- 関連 Issue 番号。例: Closes #42 / Refs #10 -->

Closes #

## Type of Change

<!-- 該当する項目にチェック -->

- [ ] 🐛 Bug fix（非破壊的、既存挙動を修正）
- [ ] ✨ New feature（非破壊的、新機能追加）
- [ ] 💥 Breaking change（既存挙動が変わる、DB rebuild / migration が必要）
- [ ] 📝 Docs only（ドキュメントのみ）
- [ ] ♻️ Refactor（挙動変更なし）
- [ ] ⚡ Performance
- [ ] 🔒 Security fix
- [ ] 🏗️ Build / CI / tooling
- [ ] 🧪 Tests only

## Changes

<!-- 主要な変更点を箇条書き -->

-
-
-

## Testing

### テスト手順

<!-- このPRをレビュワーがどう検証するか -->

-
-

### 自動テスト

- [ ] `tests/` に新規テスト追加 or 既存テスト更新（該当する場合）
- [ ] `for t in tests/test_*.py; do GRIDWORLDRAG_SKIP_CONFIG=1 .venv/bin/python "$t"; done` で全件 pass
- [ ] `flake8 src/ gridworld-rag-mcp/ tests/ --max-line-length=120 --extend-ignore=E501,W503,E221,E402,E302,E305,E401 --exclude=.venv` pass

### 手動検証（該当する場合）

- [ ] `./run_build.sh` end-to-end 実行
- [ ] `./run_sync_rotate.sh` 実行
- [ ] MCP `search` / `lookup` / `stats` / `folder_tree` / `recent_changes` / `sync_history` 動作確認
- [ ] Mac: launchd load / unload 確認
- [ ] Windows: Task Scheduler 登録 / 削除確認

## Screenshots / Logs

<!-- UI 変更やモニター画面変更がある場合、before/after のスクリーンショット -->
<!-- ログ出力の変更がある場合、サンプルログ -->

## Breaking Changes / Migration

<!-- 破壊的変更がある場合、ユーザー向けの移行手順を記載 -->
<!-- 例: "DB TRUNCATE + 再ビルドが必要" -->

- [ ] CHANGELOG.md に `BREAKING` 節を追加した（該当する場合）
- [ ] 移行スクリプト / 手順書を同梱した

## Security Checklist

- [ ] `token.pickle`, `config.env`, `shared_drives_whitelist.txt` をコミットしていない
- [ ] 新規依存パッケージは信頼できるソース（PyPI 公式）から
- [ ] 外部からの入力を直接 SQL / shell に渡していない（または適切にエスケープ）
- [ ] OAuth スコープを拡大していない（拡大した場合は SECURITY.md 更新）

## Documentation

- [ ] README.md / QUICKSTART.md / docs/*.md を更新した（該当する場合）
- [ ] CLAUDE.md のアーキテクチャ記述を最新化した（該当する場合）
- [ ] CHANGELOG.md に記載した（v0.X.Y 節）

## Checklist

- [ ] コードがプロジェクトのスタイルに従っている（`flake8` + `pyflakes`）
- [ ] セルフレビュー済み
- [ ] 難しい箇所にコメントを追加した
- [ ] 新規警告を生じさせていない
- [ ] 既存テストを壊していない

---

<!-- レビュワー向け: 主要な論点や優先的に見てほしい箇所があればここに記載 -->
