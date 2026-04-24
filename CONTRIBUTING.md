# Contributing to GridWorldRAG

ご関心ありがとうございます。Issue 報告・PR・Discussion 全て歓迎します。

## Code of Conduct

本プロジェクトは [Contributor Covenant](./CODE_OF_CONDUCT.md) を採用しています。コントリビュートの際は遵守してください。

## Development Setup

```bash
git clone https://github.com/GridWorldOrganization/GridWorldRAG.git
cd GridWorldRAG
./setup.sh
cp config.env.example config.env
# GOOGLE_EMAIL, GOOGLE_OAUTH_CLIENT_ID, GOOGLE_OAUTH_CLIENT_SECRET を設定
```

詳細は [QUICKSTART.md](./QUICKSTART.md) 参照。

## Branching

- `master` — 安定版リリース（タグ付き）
- `feature/<name>` — 新機能開発ブランチ
- `fix/<name>` — バグ修正ブランチ
- `docs/<name>` — ドキュメント更新ブランチ

## Pull Request

### 手順

1. リポジトリを fork、`master` からブランチを切る
2. 変更を実装（小さく、論理単位で commit）
3. テストを追加（新機能・バグ修正の場合）
4. Lint / Tests ローカルで pass させる
5. PR 作成（template が自動で表示される）

### PR の粒度

- **1 PR = 1 論点**。複数変更を混ぜない
- 大きい変更は事前に Issue / Discussion で設計を議論
- 破壊的変更は必ず CHANGELOG に `⚠️ BREAKING` 付きで記載

### 機密ファイルチェック

PR に以下が含まれていないこと:

- `token.pickle`（Google OAuth トークン）
- `config.env`（認証情報含む）
- `shared_drives_whitelist.txt`（内部 Drive ID）
- `.venv/`（仮想環境）
- `credentials.json` 等の Google Cloud 認証ファイル

## Lint / CI

`master` への push と PR で GitHub Actions で以下が自動実行されます:

- **Lint (`flake8`)** — `.github/workflows/lint.yml`
- **Tests (`unittest`)** — `.github/workflows/tests.yml`
- **pyflakes** — 未使用 import / 未定義変数検出
- **CodeQL** — セキュリティスキャン

ローカルでの事前チェック:

```bash
pip install flake8 pyflakes

# Lint
flake8 src/ gridworld-rag-mcp/ tests/ \
  --max-line-length=120 \
  --extend-ignore=E501,W503,E221,E402,E302,E305,E401 \
  --exclude=.venv

# 未使用 import 検出
pyflakes build_parallel.py src/ gridworld-rag-mcp/ sync_rotate.py tests/

# Tests
for t in tests/test_*.py; do GRIDWORLDRAG_SKIP_CONFIG=1 .venv/bin/python "$t"; done
```

## Tests

単体テストは `tests/` 配下、**外部依存なしの純ロジック**のみ。

```bash
# 全テスト実行
for t in tests/test_*.py; do GRIDWORLDRAG_SKIP_CONFIG=1 .venv/bin/python "$t"; done
```

### テスト追加ポリシー

- 外部リソース（Drive API, PostgreSQL）にアクセスしないもののみ
- LIKE エスケープ・lockfile・Drive API fields 等、単一関数の挙動を直接検証
- 統合テストは追加しない（実環境で smoke test する方針）
- モックも最小限（`unittest.mock` は使うが、DB / API 全体はモックしない）

### 新規テストの作法

```python
#!/usr/bin/env python3
"""tests/test_my_feature.py — 新機能の単体テスト"""

import os
import sys
import unittest

os.environ["GRIDWORLDRAG_SKIP_CONFIG"] = "1"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.my_module import my_function


class TestMyFeature(unittest.TestCase):
    def test_basic_case(self):
        self.assertEqual(my_function(1, 2), 3)


if __name__ == "__main__":
    unittest.main()
```

## Code Style

- **Python 3.12+**
- **PEP 8**（`flake8 --max-line-length=120`）
- 日本語コメント・docstring OK
- 型ヒント推奨（必須ではない）
- 投機的な抽象化はしない — シンプルに
- 関数は 50 行程度で収める、超える場合は分割を検討
- `try/finally` でリソース解放を保証（DB カーソル等）
- 書込み系はエラー時 `rollback()` を必ず呼ぶ

### Conventional Commits

コミットメッセージは [Conventional Commits](https://www.conventionalcommits.org/) 形式を推奨:

```
feat(sync): ローテーション型差分同期を追加
fix(db): LIKE エスケープで Drive file ID の `_` を保護
docs(readme): Windows サポート節を追加
chore(deps): psycopg2 を 2.9.11 に更新
```

### Prefix 一覧

- `feat`: 新機能
- `fix`: バグ修正
- `docs`: ドキュメント
- `refactor`: 挙動変更なしの整理
- `perf`: 性能改善
- `test`: テスト
- `chore`: ビルド・ツール
- `security`: セキュリティ修正

## Architecture Decision Records (ADR)

大きな技術選定は [docs/adr/](./docs/adr/) に ADR として記録してください。新規 ADR の書き方は [docs/adr/README.md](./docs/adr/README.md) 参照。

## Release

リリース手順は [docs/release.md](./docs/release.md) を参照（メンテナ向け）。

## Developer Certificate of Origin (DCO)

本プロジェクトは [DCO](https://developercertificate.org/) によるコントリビュート認証を推奨します。コミットメッセージに以下を含めてください:

```
Signed-off-by: Your Name <your.email@example.com>
```

`git commit -s` オプションで自動付与されます。

## Issues

バグ報告・機能要望は [GitHub Issues](https://github.com/GridWorldOrganization/GridWorldRAG/issues) へ。使い方の質問は [Discussions](https://github.com/GridWorldOrganization/GridWorldRAG/discussions) 推奨。

- バグ: `bug_report.md` テンプレート
- 機能要望: `feature_request.md` テンプレート
- 質問: `question.md` テンプレート
- ドキュメント問題: `docs.md` テンプレート

## Security

セキュリティ脆弱性は公開 Issue ではなく [GitHub Security Advisory](https://github.com/GridWorldOrganization/GridWorldRAG/security/advisories/new) で報告してください。詳細は [SECURITY.md](./SECURITY.md) 参照。

## License

コントリビュートすることで、あなたの貢献は [MIT License](./LICENSE) の下でライセンスされることに同意することになります。
