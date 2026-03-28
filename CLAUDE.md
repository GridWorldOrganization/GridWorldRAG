# GridWorldRAG

## 環境上の注意

- このプロジェクトのフォルダ（`/Users/tobisako/sync_mac/`以下）は Google Drive に同期されている
- 巨大ファイルのダウンロードやビルド作業は `/Users/tobisako/tmp` で行うこと（同期対象外）
- pip install 時のキャッシュも `/Users/tobisako/tmp/pip-cache` を使用する

## M2 Mac 環境（重要）

このマシンは **Apple M2 Max（ARM64）** だが、Intel 用の Homebrew（`/usr/local`）と ARM 用の Homebrew（`/opt/homebrew`）が共存している。

- **必ず ARM 版（`/opt/homebrew`）を使うこと**
- Intel 版（`/usr/local`）の Python / PostgreSQL を使うと torch 等のパッケージが古いバージョンしか入らず動作しない
- venv 作成時は `/opt/homebrew/opt/python@3.12/bin/python3.12` を使う
- PostgreSQL は `/opt/homebrew/opt/postgresql@17/bin/` を使う

```bash
# 正しいコマンド例
/opt/homebrew/opt/python@3.12/bin/python3.12 -m venv .venv
export PATH="/opt/homebrew/opt/postgresql@17/bin:$PATH"
```

### なぜこうなったか

- Anaconda（Intel x86_64 版）が `/usr/local` の Homebrew と共にインストールされていた
- `python3` コマンドが Anaconda の Python 3.8（x86_64）を指していた
- Intel 版 Python で作った venv では PyTorch 2.2.2 が上限（macOS x86_64 の wheel 配布が 2.2.2 で終了）
- sentence-transformers 5.x は PyTorch 2.4+ を要求するため動作しなかった
- ARM 版 Python に切り替えたことで PyTorch 2.11.0 がインストール可能になり解決

## workspace-mcp の使い方

- workspace-mcp は開発中の確認・検証用途でのみ使用する
- 完成コード（Python）には MCP を使わない。`google-api-python-client` で直接 Google API を呼ぶ
