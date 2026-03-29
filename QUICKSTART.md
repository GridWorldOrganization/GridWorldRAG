# GridWorldRAG クイックスタート

## 前提条件

- macOS (Apple Silicon)
- Homebrew (`/opt/homebrew`)
- Google Cloud プロジェクトに OAuth クライアント ID (デスクトップアプリ) と Google Drive API が有効

## 手順

### 1. クローン

```bash
git clone https://github.com/GridWorldOrganization/GridWorldRAG.git
cd GridWorldRAG
```

### 2. セットアップ (venv + DB + パッケージ一括)

```bash
./setup.sh
```

内部で以下を実行:
- Python 3.12 venv 作成
- `requirements.txt` のパッケージインストール
- PostgreSQL DB `gridworldrag` 作成 + `schema.sql` 適用 (pgvector 拡張含む)
- `config.env.example` → `config.env` コピー

### 3. config.env を編集

```bash
vi config.env
```

```env
GOOGLE_EMAIL=you@example.com
GOOGLE_OAUTH_CLIENT_ID=xxxx.apps.googleusercontent.com
GOOGLE_OAUTH_CLIENT_SECRET=xxxx
```

PostgreSQL 設定はデフォルトのままで通常 OK。

### 4. 共有ドライブのホワイトリスト設定

共有ドライブをインデックスする場合:

```bash
cp shared_drives_whitelist.txt.example shared_drives_whitelist.txt
vi shared_drives_whitelist.txt
```

1行に `ドライブID<TAB>名前` を記載。`#` で始まる行はコメント。

### 5. 並列ワーカー数の設定

```bash
# 自動設定（推奨）
./run_calc_workers.sh

# 手動で調整する場合は config.env の PARALLEL_WORKERS を編集
```

### 6. インデックス構築

```bash
# 並列版（推奨）
./run_build.sh

# シングル版
./run_build_single.sh
```

実行の流れ:
1. **ファイル一覧取得**（2〜3分）: 全共有ドライブからファイル一覧を収集
2. **並列処理開始**: ワーカーがドライブを分担し並列でインデックス構築
3. **モニター表示**: 各ワーカーの進捗率が2秒ごとに更新される

初回実行時、ブラウザが開いて Google 認証を求められる。認証後 `token.pickle` にキャッシュされ、次回以降は自動。

### スコープ制御 (config.env)

| 設定 | デフォルト | 説明 |
|------|-----------|------|
| `INDEX_MY_DRIVE` | `0` | マイドライブ (自分所有ファイルのみ) |
| `INDEX_SHARED_DRIVES` | `1` | 共有ドライブ (ホワイトリスト必須) |

## DB 確認コマンド

```bash
export PATH="/opt/homebrew/opt/postgresql@17/bin:$PATH"

# レコード数
psql -d gridworldrag -c "SELECT COUNT(*) FROM documents;"

# タイトル一覧 (先頭10件)
psql -d gridworldrag -c "SELECT title, chunk_index, owner FROM documents LIMIT 10;"

# ベクトル検索テスト (Python)
source .venv/bin/activate
python -c "
from src.db import connect, search_similar
from sentence_transformers import SentenceTransformer
model = SentenceTransformer('multi-qa-mpnet-base-dot-v1')
q = model.encode('検索したいキーワード')
conn = connect()
for r in search_similar(conn, q, n_results=3):
    print(f'{r[1]}: {r[2][:80]}...')
conn.close()
"
```

## トラブルシューティング

| 症状 | 対処 |
|------|------|
| `PostgreSQL に接続できません` | `brew services start postgresql@17` |
| `config.env が見つかりません` | `cp config.env.example config.env` して値を設定 |
| `torch` インストール失敗 | ARM 版 Python (`/opt/homebrew/opt/python@3.12/bin/python3.12`) で venv を再作成 |
| Google 認証エラー | `token.pickle` を削除して再実行 |
| 共有ドライブが取得できない | `shared_drives_whitelist.txt` にドライブ ID があるか確認 |
