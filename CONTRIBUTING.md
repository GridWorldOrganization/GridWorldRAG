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
