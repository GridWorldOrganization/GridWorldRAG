# FAQ

## 設計 / 技術選定

### Q. なぜ pgvector か（ChromaDB じゃないのか）

Google Drive のドキュメントはオーナー・更新日時・フォルダパス・権限（JSONB）といった構造化メタデータを多く持つため、**ベクトル類似度 + メタデータフィルタ** の複合クエリを SQL で書ける pgvector が最適でした。ChromaDB はベクトル検索専用でメタデータフィルタが限定的、かつ全文検索（tsvector）との併用ができません。詳細は [ADR 0001](./adr/0001-pgvector-over-chroma.md)。

### Q. なぜ MCP 経由で Claude Code から検索するのか

Claude Code は MCP プロトコルで外部ツールを呼び出せるため、ユーザーが「〇〇の資料ある？」と自然言語で聞くだけで Claude Code が自動的に `search` ツールを呼び、結果を文脈に取り込んで回答します。REST API や独自 CLI を作るより、Claude Code ネイティブの統合になるのが最大の価値です。

### Q. なぜ OCR がデフォルト無効なのか

画像 OCR は 1 ファイル数秒〜数十秒かかり、ビルド時間を大幅に延ばします。ほとんどのユーザーは OCR 不要（画像はファイル名で十分）なため、デフォルトは無効。必要なら `INDEX_IMAGE_OCR=1` で有効化するか、後追いで `ocr_scan.py` を使います。

### Q. なぜ埋め込みモデルを `multi-qa-mpnet-base-dot-v1` から `paraphrase-multilingual-mpnet-base-v2` に変えたのか

旧モデルは **英語 tokenizer 専用** で、日本語の熟語漢字を全て `[UNK]` に潰していました。「採用計画」と「税務申告」が cosine 1.00 で誤ヒットする致命的バグがあり、多言語モデルに切替。768 次元を維持するため schema 変更不要でした。詳細は [CHANGELOG v0.2.1](../CHANGELOG.md#021---2026-04-21)。

### Q. なぜ `psycopg2-binary` ではなく `psycopg2` (source build) か

`psycopg2-binary` はバンドル libssl を同梱するため、Python の `_ssl` モジュールと 2 つの OpenSSL が同居して SSL 不安定化を起こします。source build 版はシステム OpenSSL を共有するため安定。詳細は [ADR 0003](./adr/0003-psycopg2-source-over-binary.md)。

### Q. なぜ Webhook を使わず Changes API ポーリングなのか

Google Drive の Webhook は配信保証がなく、Mac スリープ中は受信不能、チャンネルが最大 7 日で失効するなど運用負荷が高いです。Changes API は drive 単位でページトークンを持ち、変更ゼロなら 1 API コールで即終了するため、5 分間隔で 22 ドライブを一巡しても 7 秒で完了します。Webhook 無しでも実用的なリアルタイム性が得られます。

## セットアップ

### Q. Intel Mac でも動きますか

動きますが**非推奨**。`/usr/local` 側の Homebrew では PyTorch 最新版が入らず、埋め込みモデルが古いバージョンに固定されます。Apple Silicon 推奨、Intel Mac なら Linux VM / Docker の方が安定です。

### Q. Windows ネイティブで動きますか

**動きません。WSL2 (Ubuntu 22.04+) 必須**です。Windows Task Scheduler から WSL 内の `run_sync_rotate.sh` を呼ぶ構成です。詳細は [scheduler/windows/README.md](../scheduler/windows/README.md)。

### Q. Docker で動かせますか

v0.2.x 時点では公式 Docker イメージは提供していませんが、[Dockerfile](../Dockerfile) と [docker-compose.yml](../docker-compose.yml) を参考に自前でビルドできます。v1.0.0 で公式サポート予定（[Roadmap](./roadmap.md)）。

### Q. `brew services start postgresql@17` が失敗する

既存の他バージョン（例: postgresql@14）がポート 5432 を掴んでいる可能性。`brew services list` で確認、必要なら `brew services stop postgresql@14`。

## 運用

### Q. 再実行しても安全ですか

はい、完全に安全です。全ての DB 書き込みは UPSERT（`ON CONFLICT DO UPDATE`）で冪等。`build_parallel.py` は各ファイル処理前に DB チェックして既存分をスキップします。

### Q. ビルド中に Ctrl+C で止めたら再開できますか

はい。`filelist.pkl` と `taskdata.pkl` が `/tmp` に残っていれば、次回起動時に resume プロンプトが出ます。Y（デフォルト）で Phase 1 をスキップし、処理済みファイルを瞬時にスキップして続きから処理します。

### Q. ホワイトリスト（`shared_drives_whitelist.txt`）を更新しました。どうやって反映する？

`./run_build.sh` 実行時の resume プロンプトで **N** を選択してください。Phase 1 から再実行し、最新のホワイトリストで取得します。Y（デフォルト）は前回の filelist.pkl を再利用するため、ホワイトリスト変更は反映されません。

### Q. DB のディスク使用量はどれくらい？

ざっくり **1 ドキュメントあたり約 3〜5 KB**（embedding 768次元 × float32 + メタデータ）。50,000 件で 150〜250MB 程度。詳細実測は [benchmarks.md](./benchmarks.md)。

### Q. 別マシンに DB を引き継げますか

はい。`export_db.sh` でダンプを作り、受け取りマシンで `import_db.sh` で取り込めます。受け取り側は Google Drive API 認証不要で、MCP サーバーのみ動かせば検索できます。詳細は README「別マシンへのDB転送」。

## トラブル

### Q. 「前回実行中 skip」と毎回出る

`/tmp/gridworldrag_rotate.lock` に書かれた PID が生存中の可能性。プロセスが死んでいれば自動 takeover されますが、即時復旧したいなら `rm /tmp/gridworldrag_rotate.lock`。

### Q. `exit 2` で sync_rotate が毎回落ちる

PostgreSQL データディレクトリの空き容量が 1GB 未満。`df -h /opt/homebrew/var/postgresql@17` で確認。

### Q. Google 認証エラーが出る

`token.pickle` を削除して再実行。ブラウザで再認証を求められます。OAuth Client Secret を更新した場合も同様。

### Q. Sheets API 429 エラーが多発する

`build_parallel.py` は `multiprocessing.Semaphore(2)` で自動的に Sheets API 同時アクセスを 2 に制限しています。それでも 429 が続く場合、`WORKER_START_INTERVAL_SEC` を増やす（例: 10 → 15）か `PARALLEL_WORKERS` を減らしてください。

### Q. 検索結果が日本語で変な類似性を返す（v0.2.0 以前）

埋め込みモデルのバグです。**v0.2.1 以降** は `paraphrase-multilingual-mpnet-base-v2` に切替済みで解決。古い DB を使っている場合、TRUNCATE + 再ビルドが必要です。

## 開発 / コントリビュート

### Q. テストを増やしたい

`tests/test_*.py` に追加。ただし **外部依存なしの純ロジックのみ**。DB / Drive API をモックするテストは追加しない方針（[CONTRIBUTING.md](../CONTRIBUTING.md) 参照）。

### Q. 新しい埋め込みモデルを試したい

`src/config.py` の `EMBEDDING_MODEL` を変更後、DB TRUNCATE + 再ビルド。ただし次元数が 768 から変わる場合は `schema.sql` の `VECTOR(768)` も変更が必要です。

### Q. 新しいファイル形式をサポートしたい

`src/drive_client.py` の `extract_text()` に分岐を追加。既存パターン（PDF / Docs / Sheets 等）を参考にしてください。

## その他

### Q. ライセンスは？

[MIT License](../LICENSE)。商用利用・改変・再配布可能です。

### Q. このプロジェクトは本番運用されていますか？

GridWorld Organization 社内で日常的に使用中。月次で改善・修正を加えています。

### Q. 他の RAG ツール（LangChain / LlamaIndex 等）との違いは？

LangChain / LlamaIndex は **フレームワーク**（使い方の自由度高、構築に時間がかかる）、GridWorldRAG は **完成品**（Google Drive + pgvector + MCP に特化）。特定ユースケース（Claude Code から社内ドライブを検索）に最適化されています。
