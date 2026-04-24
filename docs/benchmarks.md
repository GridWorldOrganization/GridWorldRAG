# Benchmarks

GridWorldRAG の性能実測値。測定環境は下記、数値は継続的に更新予定。

> **Note**: 数値は参考値。実際の性能は Drive のファイル構成、ネットワーク、マシンスペックで大きく変動します。

## 測定環境

| 項目 | 値 |
|---|---|
| マシン | MacBook Pro M2 Max (12-core CPU, 96GB RAM) |
| OS | macOS 15.x (Apple Silicon) |
| Python | 3.12.x |
| PostgreSQL | 17.x (Homebrew ARM) |
| pgvector | 0.8.x |
| 埋め込みモデル | `paraphrase-multilingual-mpnet-base-v2`（768 次元） |
| Drive 環境 | 共有ドライブ 22 個、計 61,810 アイテム（ファイル 49,372 / フォルダ 12,438） |
| ネットワーク | 光回線（実測 300 Mbps 下り） |
| 計測日 | 2026-04-21 (v0.2.1) |

## 初回フルビルド（`./run_build.sh`）

| フェーズ | 所要時間 | 備考 |
|---|---|---|
| Phase 1: ファイル一覧取得 | 約 3 分 | `FETCH_THREADS=3` で並列 |
| Phase 2: タスク分割 | < 1 秒 | 即完了 |
| Phase 3: VectorDB 作成 | 約 25 分 | `PARALLEL_WORKERS=8` |
| **合計** | **約 28 分** | 49k ファイル処理 |

### ワーカー数による差

| `PARALLEL_WORKERS` | Phase 3 時間 | Sheets 429 頻度 |
|---|---|---|
| 4 | 約 45 分 | ほぼ無し |
| 8 | 約 25 分 | 中程度（Semaphore でカバー） |
| 12 | 約 22 分 | 頻発（体感悪化） |

**推奨**: 8 ワーカー（`run_calc_workers.sh` が自動算出）。

### ファイル種別ごとのスループット

| 種別 | 平均処理時間 | 主因 |
|---|---|---|
| Google Docs | 0.3 秒 | export text |
| Google Sheets（全シート） | 2〜8 秒 | Sheets API + シート数 |
| Google Slides | 0.4 秒 | export text |
| PDF（テキストベース） | 1〜3 秒 | pypdf |
| PDF（スキャン） | 0.2 秒 | メタデータのみ |
| 画像（OCR 無効） | 0.1 秒 | メタデータのみ |
| 画像（OCR 有効） | 3〜15 秒 | pytesseract |
| 動画 / 音声 | 0.1 秒 | メタデータのみ |
| フォルダ | 0.05 秒 | ほぼ即時 |

## 差分同期（`sync_rotate.py`）

### 変更ゼロ時（ベストケース）

| 項目 | 値 |
|---|---|
| 全 22 ドライブ巡回時間 | **約 7 秒** |
| Changes API コール数 | 22 |
| 埋め込みモデルロード | スキップ（遅延ロード） |
| CPU 使用率 | < 5% |
| メモリ消費 | 約 30 MB |

### 変更 1 ファイル時

| 項目 | 値 |
|---|---|
| 全 22 ドライブ巡回時間 | 約 8〜12 秒 |
| 埋め込みモデルロード | 5〜10 秒（初回） |
| 変更ファイル処理 | 0.5〜5 秒（種別による） |

### 変更多数（100 ファイル）時

| 項目 | 値 |
|---|---|
| 全 22 ドライブ巡回時間 | 約 30〜60 秒 |
| 処理速度 | 約 2〜5 ファイル/秒 |

## 検索（MCP `search` ツール）

| 項目 | 値 |
|---|---|
| 埋め込み生成（クエリ文字列） | 約 50 ms |
| pgvector 類似度検索（top 5） | 約 20〜50 ms（IVFFlat, lists=100） |
| 総レイテンシ | 約 80〜120 ms |

### 初回検索のみ追加コスト

MCP プロセス起動直後は埋め込みモデルのロードで **追加 5〜10 秒**。v0.7.0 で daemon IPC 経由の encode に移行予定（[Roadmap](./roadmap.md)）。

## DB サイズ

| レコード数 | DB サイズ | 備考 |
|---|---|---|
| 10,000 | 約 40 MB | 小規模 |
| 50,000 | 約 200 MB | 中規模 |
| 100,000 | 約 400 MB | 大規模 |
| 300,000 | 約 1.2 GB | 社内本番相当 |

（embedding 768次元 × float32 = 3 KB/レコード + メタデータ 1〜2 KB + インデックス）

## ディスク I/O

`/tmp/gridworldrag_taskdata.pkl` のサイズ（Phase 2 生成物）:

| ドライブ数 | ファイル数 | pkl サイズ |
|---|---|---|
| 22 | 49,372 | 約 150 MB |

`BUILD_MIN_TMP_FREE_BYTES` デフォルト 500 MB のプリフライトチェックで担保。

## ネットワーク

Drive API コール数（初回フルビルド、49k ファイル）:

| API | コール数 |
|---|---|
| `files.list`（Phase 1） | 約 250（ページング × 22 ドライブ） |
| `files.export`（Docs/Slides） | 約 30,000 |
| `files.get` + `get_media`（PDF/image） | 約 15,000 |
| `spreadsheets.values.get`（Sheets） | 約 4,000（シート別） |
| `changes.list`（sync_rotate、変更ゼロ/実行） | 22 |

## 継続測定

ベンチマーク再現方法:

```bash
# Phase 別時間測定
time ./run_build.sh

# DB サイズ
psql -d gridworldrag_1 -c "SELECT pg_size_pretty(pg_database_size('gridworldrag_1'));"

# レコード数
psql -d gridworldrag_1 -c "SELECT COUNT(*) FROM documents;"

# 検索レイテンシ
python -c "
import time
from src.db import connect, search_similar
from sentence_transformers import SentenceTransformer

model = SentenceTransformer('paraphrase-multilingual-mpnet-base-v2')
conn = connect()

for q in ['採用計画', '売上目標', 'エンジニア'] * 10:
    t0 = time.perf_counter()
    q_vec = model.encode(q)
    search_similar(conn, q_vec, n_results=5)
    print(f'{q}: {(time.perf_counter()-t0)*1000:.1f} ms')
"
```

## 参考

- 測定用スクリプトは `scripts/benchmark.sh` に追加予定（[Roadmap](./roadmap.md)）
- 環境ごとの差分を知りたい場合は Issue で共有歓迎
