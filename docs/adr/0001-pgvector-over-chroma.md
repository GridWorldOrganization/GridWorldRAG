# ADR 0001: pgvector を採用（ChromaDB ではなく）

- **Status**: Accepted
- **Date**: 2026-03-30
- **Deciders**: GridWorld Organization メンテナチーム

## Context

Google Drive ドキュメントのセマンティック検索バックエンドとして Vector DB が必要だった。候補:

1. **ChromaDB** — Python ネイティブ、セットアップ軽量、Vector DB 専用
2. **PostgreSQL + pgvector** — リレーショナル DB + ベクトル拡張
3. **Pinecone / Weaviate** — クラウドベース
4. **FAISS + SQLite** — 手書きの組合せ

要件:

- Claude Code から MCP 経由で検索 → 単一プロセスから同期的アクセス可能であること
- オーナー・更新日時・フォルダパス・権限などメタデータとの複合フィルタが必須
- 本番運用実績があり、データロスのリスクが低いこと
- クラウド依存を避けたい（個人 Mac / 社内 LAN で完結）
- 将来、構造化データ（スプレッドシート抽出値）との JOIN を視野に入れたい

## Decision

**PostgreSQL 17 + pgvector 0.8 を採用する**。

- 埋め込みは `VECTOR(768)` カラム、IVFFlat インデックス（`lists=100`）
- メタデータは同じ `documents` テーブルに通常のカラム / JSONB として格納
- 検索は SQL `ORDER BY embedding <=> $1` でベクトル類似度を取得
- Homebrew で `postgresql@17` + `pgvector` をインストール

## Consequences

### 良い面

- **複合フィルタが SQL で書ける**: `WHERE owner = ... AND drive_modified_at > ... ORDER BY embedding <=>` のような組合せが自然
- **本番運用実績が豊富**: PostgreSQL 自体の安定性・バックアップ・レプリケーション資産を利用可能
- **構造化データとの JOIN 可**: 将来スプレッドシート値を別テーブルに持たせた時も JOIN で結合できる
- **全文検索（tsvector）と併用可**: ベクトル検索 + キーワード検索のハイブリッド検索に拡張可能
- **学習コストが低い**: SQL は既知
- **Claude Code から `psql` 直接実行可能**: デバッグ容易

### 悪い面 / トレードオフ

- **セットアップが Homebrew 等 OS 依存** — ChromaDB は `pip install` のみ
- **IVFFlat のチューニング** — `lists` 値の最適化が必要（小規模では IVFFlat より brute force が速い場合あり）
- **pgvector の更新頻度は ChromaDB より遅い** — 最新のインデックス戦略（例: SPANN）は非対応
- **Python 外からの利用が想定されていない** — 現時点ではこれは問題なし
- **WSL / Docker ユーザー向けのセットアップ説明が増える**

### 後悔ポイント（v0.2.x 時点）

なし。IVFFlat → HNSW への切替はロードマップで検討中（[roadmap.md](../roadmap.md)）だが、現在の規模（< 300k レコード）では IVFFlat で十分。

## Alternatives Considered

### ChromaDB

却下理由:

- メタデータフィルタが限定的（`$eq` / `$gt` 等の Mongo 風 DSL、JOIN 不可）
- 全文検索との併用が不可
- 本番運用実績が PostgreSQL と比較して浅い

### Pinecone / Weaviate（クラウド）

却下理由:

- クラウド依存（オフライン開発不可）
- 月額コスト（GridWorldRAG は個人 Mac / 社内 LAN 前提）
- ベンダーロックイン

### FAISS + SQLite

却下理由:

- インデックス更新のアトミック性を自前で保証する必要あり
- 権限 JSONB のような複雑メタデータの取扱いが面倒
- pgvector と比較した優位性がない

## References

- [pgvector GitHub](https://github.com/pgvector/pgvector)
- [docs/vectordb.md](../vectordb.md) — 選定経緯の補足
