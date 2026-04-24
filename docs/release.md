# リリース手順

メンテナ向けのリリースプロセス記録。

## バージョニング

[Semantic Versioning 2.0.0](https://semver.org/lang/ja/) に準拠:

- **MAJOR** (`X.0.0`): 互換性のない API / DB スキーマ変更
- **MINOR** (`0.X.0`): 後方互換な新機能
- **PATCH** (`0.0.X`): 後方互換なバグ修正

### 破壊的変更の扱い

- DB TRUNCATE が必要な場合は MAJOR 相当。ただし v0.x 系ではマイナーでも明記して許容（CHANGELOG 冒頭に ⚠️ 警告を置く）
- v1.0.0 以降は厳密に SemVer を守る

## リリースチェックリスト

### 1. 事前準備

- [ ] `master` ブランチが最新（`git pull`）
- [ ] すべてのテストが pass（`for t in tests/test_*.py; do GRIDWORLDRAG_SKIP_CONFIG=1 .venv/bin/python "$t"; done`）
- [ ] Lint が pass（CI 緑）
- [ ] 全オープン PR が merge or close 済み
- [ ] Milestone の全 Issue が close 済み

### 2. CHANGELOG 更新

```bash
vi CHANGELOG.md
```

- [Keep a Changelog](https://keepachangelog.com/en/1.0.0/) 形式
- セクション: `Added` / `Changed` / `Fixed` / `Removed` / `Breaking`
- 破壊的変更は冒頭に ⚠️ 警告
- 日付は ISO 8601（`2026-04-21`）
- 各エントリに技術的根拠と背景を 1-2 行記載（なぜ変更したか）

### 3. バージョンバンプ

現在はコード内に明示的な `__version__` が無いため、CHANGELOG のヘッダーバージョンを正とする。

v0.3.0 以降は `src/__init__.py` に `__version__` を導入予定（[roadmap.md](./roadmap.md)）。

### 4. ドキュメント更新

- [ ] README.md のバッジに新バージョン反映（自動更新されるが確認）
- [ ] 新機能は README / QUICKSTART / docs/*.md に反映
- [ ] Breaking changes は ADR を追加（必要なら）
- [ ] `mac-resident-daemon.md` の進捗セクションを更新

### 5. commit + tag

```bash
git add CHANGELOG.md <変更ファイル>
git commit -m "release: vX.Y.Z"

git tag -a vX.Y.Z -m "Release vX.Y.Z"
git push origin master
git push origin vX.Y.Z
```

### 6. GitHub Release 作成

```bash
gh release create vX.Y.Z \
  --title "vX.Y.Z" \
  --notes-file <(sed -n '/## \[X\.Y\.Z\]/,/## \[/{/## \[/!p;}' CHANGELOG.md)
```

または GitHub Web UI で作成。Release Notes には CHANGELOG の該当節をコピペ。

### 7. リリース後

- [ ] `gh release list` で公開確認
- [ ] 既存ユーザー向けにアップグレード手順を共有（破壊的変更の場合）
- [ ] 次の Milestone 作成
- [ ] Issue トリアージ

## 破壊的変更（DB rebuild）のユーザー手順

```bash
# 1. 最新を pull
git pull origin master

# 2. 依存更新
./setup.sh   # or pip install -r requirements.txt

# 3. DB TRUNCATE
psql -d gridworldrag_1 -c "TRUNCATE documents;"

# 4. 再ビルド
./run_build.sh

# 5. 動作確認
# Claude Code で MCP search 実行
```

## ホットフィックス手順

Critical なバグ（セキュリティ / データロス）の場合:

1. `fix/critical-<issue>` ブランチ作成
2. 最小限の修正 + テスト追加
3. PR 作成、メンテナ 1 名レビュー即 merge
4. PATCH バージョンで即リリース（上記フロー短縮版）
5. 破壊的変更がなければ CHANGELOG の `Fixed` に追記のみ

## 参考リンク

- [SemVer 2.0.0](https://semver.org/)
- [Keep a Changelog](https://keepachangelog.com/)
- [Conventional Commits](https://www.conventionalcommits.org/) — コミットメッセージ規約（推奨）
- [gh CLI Docs](https://cli.github.com/manual/)
