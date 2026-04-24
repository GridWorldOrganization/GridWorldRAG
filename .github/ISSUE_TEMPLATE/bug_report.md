---
name: Bug Report
about: Report a bug or unexpected behavior
title: '[Bug] '
labels: bug
assignees: ''
---

## Description

<!-- バグの内容を明確に記述 -->

## Steps to Reproduce

1.
2.
3.

## Expected Behavior

<!-- 期待した挙動 -->

## Actual Behavior

<!-- 実際に起きた挙動（エラーメッセージ・スタックトレース含む） -->

```
(エラー出力をここに貼る)
```

## Environment

| Item | Value |
|------|-------|
| OS | macOS (Apple Silicon / Intel) / Windows 11 WSL2 / Linux |
| Python | 例: 3.12.x |
| PostgreSQL | 例: 17.x |
| pgvector | 例: 0.8.x |
| GridWorldRAG version | 例: v0.2.1 (`git rev-parse HEAD`) |
| 埋め込みモデル | 例: paraphrase-multilingual-mpnet-base-v2 |
| Drive 環境 | 共有ドライブ / マイドライブ / 両方 |

## Logs

### `~/Library/Logs/gridworldrag/sync_rotate.log`（sync 系の場合）

```
(関連する直近のログを貼る)
```

### `/tmp/gridworldrag_build.log`（build 系の場合）

```
(関連する直近のログを貼る)
```

## Related config（機密値を除く）

```
(config.env の関連部分、例: PARALLEL_WORKERS=8, TASK_SPLIT_THRESHOLD=5000)
```

## Additional Context

<!-- 発生頻度、回避策、関連 Issue 等 -->
