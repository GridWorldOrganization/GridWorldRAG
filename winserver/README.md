# WinServerRAG

Windows 常駐の RAG サーバー。Google Drive 共有フォルダ単位で DB を持ち、
Claude Cowork から MCP 経由で遠隔検索可能。既存 GridWorldRAG のロジックを
Windows ネイティブ向けに移植したもの。

## Phase 1 構成

- `src/rag_daemon.py` — 常駐デーモン。ON 状態の FD を巡回同期
- `src/control_api.py` — FastAPI (+ WebSocket) 制御 API + Web モニター配信
- `web/` — Win モニター Web UI (vanilla JS)
- per-FD スキーマ方式: 共有フォルダごとに `fd_<drive_id>` スキーマを分離

## クイックスタート (開発)

1. Python 3.12+ を用意
2. 仮想環境: `python -m venv .venv && .venv\Scripts\activate`
3. 依存: `pip install -r requirements.txt`
4. PostgreSQL 17 + pgvector を起動 (Portable 手順 or winget)
5. DB 初期化: `python -m src.db_init`
6. `config\config.env.example` を `config\config.env` にコピーして編集
7. API + モニター起動: `scripts\run_api.bat`
8. デーモン起動 (別ターミナル): `scripts\run_daemon.bat`
9. ブラウザで `http://127.0.0.1:17600/` を開く

## 構成図

```
[Google Drive 共有フォルダ] --(whitelist)-->  rag_daemon  --(schema=fd_xxx)--> PostgreSQL + pgvector
                                                  |                                   ^
                                                  | status/progress                   |
                                                  v                                   |
                                            control_api (FastAPI)  <--- web monitor --/
                                                  ^
                                                  |
                                            Claude Cowork (後続: MCP HTTP)
```
