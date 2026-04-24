# ADR 0003: psycopg2 の source build 版を採用（psycopg2-binary ではなく）

- **Status**: Accepted
- **Date**: 2026-03-31
- **Deciders**: GridWorld Organization メンテナチーム

## Context

Python から PostgreSQL に接続するドライバとして `psycopg2` を利用する必要があった。選択肢:

1. **`psycopg2-binary`** — PyPI で wheel 配布、libssl / libpq をバンドル
2. **`psycopg2`** — source build、システム lib をリンク

当初は `psycopg2-binary` を採用していたが、`build_parallel.py` 実行中に以下のクラッシュが頻発:

```
SIGSEGV (signal 11) in SSL_read_ex (tls_get_more_records + 244)
SIGABRT (signal 6) in tls_release_read_buffer + 56 (double-free)
```

クラッシュレポートのバイナリイメージに **2 つの libssl.3.dylib** が存在:

1. Homebrew 版（Python の `_ssl` モジュールがリンク）
2. psycopg2-binary バンドル版（DB 接続用）

同一プロセスに 2 つの OpenSSL インスタンスが同居していた。これに daemon スレッドでのタイムアウト制御（後に [別の問題](./0002-rotation-based-sync.md) と判明）が組み合わさり、SSL のグローバル状態が干渉して SEGFAULT / double-free を引き起こしていた。

## Decision

**`psycopg2`（source build 版）を `requirements.txt` で指定する**。

```
psycopg2==2.9.11
```

インストール時に Homebrew の `libssl` / `libpq` を強制的にリンクする:

```bash
export PATH="/opt/homebrew/opt/postgresql@17/bin:/opt/homebrew/opt/openssl@3/bin:$PATH"
pip install --no-binary :all: psycopg2==2.9.11
```

## Consequences

### 良い面

- **SSL の重複が解消**: プロセス内で 1 つの OpenSSL のみが使われる
- **SEGFAULT / double-free が再現しなくなった**: 実測で 48 時間連続ビルドでもクラッシュゼロ
- **メモリ使用量がやや少ない**: バンドル版 lib を重複ロードしないため
- **システム OpenSSL のセキュリティ更新が自動で反映される**: バンドル版は psycopg2-binary 側のリリース待ち

### 悪い面 / トレードオフ

- **インストール前提条件が増える**: `libpq-dev` / `libssl-dev` / `build-essential` が必要（Linux）、`postgresql@17` + `openssl@3` の Homebrew インストール（Mac）
- **`pip install` に数十秒かかる**（バンドル版は即時）
- **CI セットアップが若干複雑化**: Docker イメージや Linux CI で apt パッケージ追加が必要
- **新規ユーザー向けドキュメントに記述が増える**: [CLAUDE.md](../../CLAUDE.md) に明記

### 代替策で検討したが不採用

- **バンドル版のまま daemon thread を廃止するのみ** — SSL 重複自体は残るため、将来別の原因で再発するリスクを残す
- **`pg8000`（pure Python driver）** — 性能が `psycopg2` より大きく劣る（特に COPY 系）
- **`asyncpg`** — 非同期専用、同期クライアントが必要な場面で使いにくい

## Alternatives Considered

### 継続して `psycopg2-binary` を使用し、SSL 問題を個別に回避

却下理由:

- 根本原因が「2 つの OpenSSL 同居」なので、いつ別の形で再発するか分からない
- daemon thread を廃止しても SSL 状態の干渉リスクは残る
- psycopg2-binary は公式も「本番環境では source build 版を推奨」としている

### pure Python driver (`pg8000`) への切替

却下理由:

- COPY / 大量 insert の性能が 3〜5 倍遅い（実測）
- pgvector 拡張への対応が不完全（カスタム型登録の手間）
- 既存コードの変更が大きい

## 併せて実施した修正

- [daemon thread での timeout 廃止 → httplib2 socket timeout](./0002-rotation-based-sync.md) （同時期に発覚した別問題）
- `db.py` の全関数で `try/finally` によるカーソルリーク防止
- 書き込み系の `rollback()` 追加

## References

- [psycopg2 公式: Binary vs Source](https://www.psycopg.org/docs/install.html#binary-install-from-pypi)
- [psycopg/psycopg2 Issue #543](https://github.com/psycopg/psycopg2/issues/543) — binary vs source のディスカッション
- [CHANGELOG v0.1.1](../../CHANGELOG.md#011---2026-03-31) — SSL crash fix
- [docs/dev_talk_v2.md](../dev_talk_v2.md) — クラッシュの詳細記録
