# ADR 0002: ローテーション型差分同期を採用（Webhook 経由ではなく）

- **Status**: Accepted
- **Date**: 2026-04-11
- **Deciders**: GridWorld Organization メンテナチーム

## Context

初回フルビルド後、Google Drive の変更を継続的に反映する仕組みが必要だった。候補:

1. **Google Drive Webhook (Push Notification)** + ngrok 等で公開 URL を付与
2. **Changes API をポーリング** — drive 単位でページトークンを持ち、変更ゼロなら即スキップ
3. **定期フル再ビルド** — 毎日 1 回 `build_parallel.py` を再実行

要件:

- Mac スリープ中の変更も復帰後に確実に取り込む
- 数百〜数万のファイル規模で 5 分間隔の定期実行に耐える
- 個人 Mac で完結（ngrok 等の外部公開サービス依存を避ける）
- 多重起動の防止
- 失敗したファイルが永遠に欠落しない（at-least-once）

## Decision

**Changes API ベースのローテーション型差分同期を採用する（`sync_rotate.py`）**。

- 共有ドライブごとに独立した `sync_state.rotate_token_<drive_id>` をページトークンとして保持
- 1 実行 = 全ドライブ を順番にチェック（ローテーション）
- 変更ゼロのドライブは Changes API 1 コールで即スキップ
- 埋め込みモデル（SentenceTransformer）は変更発生時のみ遅延ロード
- `/tmp/gridworldrag_rotate.lock` + PID liveness probe で多重起動防止
- launchd (Mac) / Windows Task Scheduler (WSL) から 5 分間隔で定期実行
- 失敗ファイルは `sync_state.failed_files` JSON キューに積み、次回最優先で再試行
- `upsert_file_chunks(file_id, chunks)` で delete + insert をアトミック実行

## Consequences

### 良い面

- **ngrok / 公開 URL 不要**: 個人 Mac のみで完結、外部公開の運用リスクなし
- **Mac スリープ耐性**: 復帰後の次回実行で取りこぼしを確実にキャッチ（Webhook は配信保証がない）
- **CPU 効率**: 変更ゼロ時は 22 ドライブ × 1 API コールで 7 秒完了、CPU < 5%
- **埋め込みモデル未ロード**: 変更なし時はメモリ消費 30 MB 程度（モデルは遅延ロード）
- **Webhook 失効リスクなし**: Webhook チャンネルは最大 7 日で失効するが、Changes API は永続的にトークンで追跡
- **永久データロス防止**: `failed_files` キューでリカバリされるため、トークン前進が安全
- **実装がシンプル**: HTTP サーバー不要、cron 1 本で動く

### 悪い面 / トレードオフ

- **準リアルタイム性が劣る**: 5 分間隔のため、最悪 5 分の遅延（Webhook なら数秒）
- **Drive API クォータ消費**: 変更ゼロ時も 1 ドライブ 1 コール発生
- **大量変更時は遅い**: 100 ファイル変更で約 30〜60 秒かかる（Webhook なら個別にストリーム可能）
- **ローテーション順序固定**: 常に同じ順序で巡回するため、特定ドライブ優先はできない

### 後悔ポイント

なし。v0.3.0 以降の常駐デーモン化で polling 間隔 30 秒まで短縮可能となる予定（[mac-resident-daemon.md](../mac-resident-daemon.md)）。

## Alternatives Considered

### Webhook + ngrok

却下理由:

- 公式仕様上、Webhook 配信は保証されない
- Mac スリープ中は受信不能 → 結局ポーリングのバックアップが必要
- Webhook チャンネルが 7 日で失効 → 再登録運用が複雑
- ngrok は個人利用なら可だが本番運用向けではない
- ユーザー環境での設定ハードルが高い（URL 固定化・認証・再起動耐性）

### 定期フル再ビルド（毎日 1 回）

却下理由:

- 遅延が最大 24 時間
- CPU を毎日 30 分消費（MAINTAINING 時 < 1% と比較し大きな差）
- 長時間ビルドはユーザー体感が悪い

### Drive 全件の直接ポーリング（`files().list()` で更新日時チェック）

却下理由:

- Changes API と比較して API コール数が桁違いに多い
- 削除検知が困難（存在しなくなったファイルを個別確認不可）
- ページングを毎回頭から → 非効率

## References

- [Google Drive Changes API](https://developers.google.com/drive/api/reference/rest/v3/changes)
- [sync_rotate.py 実装](../../sync_rotate.py)
- [CHANGELOG v0.1.2](../../CHANGELOG.md#012---2026-04-11) — 初期実装
- [docs/architecture.md](../architecture.md) — 同期フロー Mermaid 図
