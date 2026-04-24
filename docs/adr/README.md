# Architecture Decision Records (ADR)

主要な技術選定の意思決定記録。設計判断の「なぜ」を後から辿れるようにするためのドキュメント。

## 一覧

| # | タイトル | 状態 | 日付 |
|---|---|---|---|
| [0001](./0001-pgvector-over-chroma.md) | pgvector を採用（ChromaDB ではなく） | Accepted | 2026-03-30 |
| [0002](./0002-rotation-based-sync.md) | ローテーション型差分同期を採用 | Accepted | 2026-04-11 |
| [0003](./0003-psycopg2-source-over-binary.md) | psycopg2 source build 版を採用 | Accepted | 2026-03-31 |

## フォーマット

[Michael Nygard 形式](https://cognitect.com/blog/2011/11/15/documenting-architecture-decisions) に準拠:

- **Title**: `NNNN-decision-topic.md`
- **Status**: Proposed / Accepted / Deprecated / Superseded
- **Context**: なぜこの決定が必要になったか（背景）
- **Decision**: 何を決めたか
- **Consequences**: 決定による影響（良い面・悪い面・トレードオフ）

## 新規 ADR 追加

1. 番号は連番（既存の最大 + 1）
2. ファイル名は `NNNN-short-topic-kebab-case.md`
3. 状態が変わったら（Deprecated / Superseded）、古い ADR にリンクを追加
4. README.md の一覧テーブルを更新
