# Contributing to GridWorldRAG

## Development Setup

```bash
git clone https://github.com/GridWorldOrganization/GridWorldRAG.git
cd GridWorldRAG
./setup.sh
cp config.env.example config.env
# Set GOOGLE_EMAIL, GOOGLE_OAUTH_CLIENT_ID, GOOGLE_OAUTH_CLIENT_SECRET in config.env
```

## Branching

- `master` — stable releases
- Feature branches: `feature/<name>`
- Bug fixes: `fix/<name>`

## Pull Requests

1. Fork the repository and create a branch from `master`
2. Make your changes
3. Ensure no sensitive files are included (`token.pickle`, `config.env`, `shared_drives_whitelist.txt`)
4. Open a PR — the template will guide you through what to include

## Lint / CI

`master` への push と PR で GitHub Actions の lint が自動実行される。

```bash
# ローカルで事前チェック（CI と同じ設定）
pip install flake8 pyflakes
flake8 src/ gridworld-rag-mcp/ --max-line-length=120 \
  --extend-ignore=E501,W503,E221,E402,E302,E305,E401 --exclude=.venv
pyflakes build_parallel.py src/ gridworld-rag-mcp/ sync.py
```

- `flake8`: スタイルチェック（`.github/workflows/lint.yml` で定義）
- `pyflakes`: 未使用 import・未定義変数の検出（CI 外だがローカルで必ず実行）

## Code Style

- Python 3.12+
- PEP 8 (`flake8` with `--max-line-length=120`)
- Docstrings and comments in Japanese are fine
- No speculative abstractions — keep it simple

## What Not to Include

- `token.pickle` — Google OAuth token (personal credential)
- `config.env` — contains secrets
- `shared_drives_whitelist.txt` — contains internal Drive IDs
- `.venv/` — Python virtual environment

## Issues

Bug reports and feature requests → [GitHub Issues](https://github.com/GridWorldOrganization/GridWorldRAG/issues)

Please use the provided issue templates.

## License

By contributing, you agree that your contributions will be licensed under the [MIT License](LICENSE).
