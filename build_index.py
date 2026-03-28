#!/usr/bin/env python3
"""
build_index.py - Google Drive 全件インデックス構築スクリプト

Google Drive の全ファイルを取得し、テキスト抽出 → チャンク分割 →
埋め込み生成 → PostgreSQL + pgvector に一括投入する。
"""

import io
import os
import pickle
import sys
import time
from pathlib import Path

import psycopg2
from pgvector.psycopg2 import register_vector
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from langchain_text_splitters import RecursiveCharacterTextSplitter
from sentence_transformers import SentenceTransformer


def load_config():
    """config.env を読み込んで環境変数にセットする。"""
    config_path = Path(__file__).parent / "config.env"
    if not config_path.exists():
        print(f"エラー: {config_path} が見つかりません。")
        print("config.env.example をコピーして config.env を作成してください。")
        sys.exit(1)
    with open(config_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


load_config()

# --- 設定 ---
SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
GOOGLE_EMAIL = os.environ["GOOGLE_EMAIL"]
GOOGLE_CLIENT_ID = os.environ["GOOGLE_OAUTH_CLIENT_ID"]
GOOGLE_CLIENT_SECRET = os.environ["GOOGLE_OAUTH_CLIENT_SECRET"]
TOKEN_PATH = os.environ.get("GOOGLE_TOKEN_PATH", "token.pickle")
DB_NAME = os.environ.get("PGDATABASE", "gridworldrag")
DB_USER = os.environ.get("PGUSER", os.getenv("USER", "tobisako"))
DB_HOST = os.environ.get("PGHOST", "localhost")
DB_PORT = os.environ.get("PGPORT", "5432")
EMBEDDING_MODEL = "multi-qa-mpnet-base-dot-v1"
CHUNK_SIZE = 600
CHUNK_OVERLAP = 120
BATCH_SIZE = 100

# Google Docs 系の MIME タイプとエクスポート形式
EXPORT_MIME_MAP = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
    "application/vnd.google-apps.presentation": "text/plain",
}

# テキスト抽出可能な MIME タイプ
TEXT_MIME_TYPES = {
    "text/plain",
    "text/markdown",
    "text/csv",
    "text/html",
    "application/json",
    "application/xml",
}

# スキップする MIME タイプ
SKIP_MIME_TYPES = {
    "application/vnd.google-apps.folder",
    "application/vnd.google-apps.shortcut",
    "application/vnd.google-apps.form",
    "application/vnd.google-apps.map",
    "application/vnd.google-apps.site",
}


def authenticate():
    """Google Drive API の認証を行い、サービスオブジェクトを返す。

    config.env の CLIENT_ID / CLIENT_SECRET を使って OAuth 認証する。
    credentials.json ファイルは不要。
    """
    creds = None
    token_path = Path(__file__).parent / TOKEN_PATH

    if token_path.exists():
        with open(token_path, "rb") as f:
            creds = pickle.load(f)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            client_config = {
                "installed": {
                    "client_id": GOOGLE_CLIENT_ID,
                    "client_secret": GOOGLE_CLIENT_SECRET,
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": ["http://localhost"],
                }
            }
            flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(token_path, "wb") as f:
            pickle.dump(creds, f)

    return build("drive", "v3", credentials=creds)


def list_all_files(service):
    """Google Drive の全ファイルを取得する。"""
    files = []
    page_token = None
    query = "trashed = false"

    while True:
        response = service.files().list(
            q=query,
            spaces="drive",
            fields="nextPageToken, files(id, name, mimeType, modifiedTime, owners, webViewLink)",
            pageSize=1000,
            pageToken=page_token,
            includeItemsFromAllDrives=True,
            supportsAllDrives=True,
        ).execute()

        batch = response.get("files", [])
        files.extend(batch)
        print(f"  取得済み: {len(files)} ファイル", end="\r")

        page_token = response.get("nextPageToken")
        if not page_token:
            break

    print(f"  合計: {len(files)} ファイル")
    return files


def extract_text(service, file_info):
    """ファイルからテキストを抽出する。"""
    mime_type = file_info["mimeType"]
    file_id = file_info["id"]

    if mime_type in SKIP_MIME_TYPES:
        return None

    # Google Docs 系: エクスポート
    if mime_type in EXPORT_MIME_MAP:
        export_mime = EXPORT_MIME_MAP[mime_type]
        try:
            request = service.files().export_media(fileId=file_id, mimeType=export_mime)
            content = io.BytesIO()
            downloader = MediaIoBaseDownload(content, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()
            return content.getvalue().decode("utf-8", errors="replace")
        except Exception as e:
            print(f"  警告: エクスポート失敗 [{file_info['name']}]: {e}")
            return None

    # テキスト系ファイル: ダウンロード
    if mime_type in TEXT_MIME_TYPES:
        try:
            request = service.files().get_media(fileId=file_id)
            content = io.BytesIO()
            downloader = MediaIoBaseDownload(content, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()
            return content.getvalue().decode("utf-8", errors="replace")
        except Exception as e:
            print(f"  警告: ダウンロード失敗 [{file_info['name']}]: {e}")
            return None

    # PDF: テキスト抽出を試みる
    if mime_type == "application/pdf":
        try:
            request = service.files().get_media(fileId=file_id)
            content = io.BytesIO()
            downloader = MediaIoBaseDownload(content, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()
            content.seek(0)
            try:
                import pypdf
                reader = pypdf.PdfReader(content)
                text = "\n".join(page.extract_text() or "" for page in reader.pages)
                if text.strip():
                    return text
            except ImportError:
                print("  警告: pypdf がインストールされていません。PDF はスキップします。")
            return None
        except Exception as e:
            print(f"  警告: PDF 処理失敗 [{file_info['name']}]: {e}")
            return None

    return None


def connect_db():
    """PostgreSQL に接続する。"""
    conn = psycopg2.connect(
        dbname=DB_NAME,
        user=DB_USER,
        host=DB_HOST,
        port=DB_PORT,
    )
    register_vector(conn)
    return conn


def insert_chunks(conn, chunks_data):
    """チャンクデータを DB に一括挿入する。"""
    cur = conn.cursor()
    for chunk in chunks_data:
        cur.execute(
            """
            INSERT INTO documents
                (drive_file_id, title, content, chunk_index, owner, source_url, file_type, drive_modified_at, embedding)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (drive_file_id) DO UPDATE SET
                title = EXCLUDED.title,
                content = EXCLUDED.content,
                chunk_index = EXCLUDED.chunk_index,
                owner = EXCLUDED.owner,
                source_url = EXCLUDED.source_url,
                file_type = EXCLUDED.file_type,
                drive_modified_at = EXCLUDED.drive_modified_at,
                embedding = EXCLUDED.embedding
            """,
            (
                chunk["drive_file_id"],
                chunk["title"],
                chunk["content"],
                chunk["chunk_index"],
                chunk["owner"],
                chunk["source_url"],
                chunk["file_type"],
                chunk["drive_modified_at"],
                chunk["embedding"],
            ),
        )
    conn.commit()
    cur.close()


def main():
    print("=" * 60)
    print("GridWorldRAG - インデックス構築")
    print("=" * 60)

    # 1. Google Drive 認証
    print("\n[1/5] Google Drive 認証...")
    service = authenticate()
    print("  認証完了")

    # 2. ファイル一覧取得
    print("\n[2/5] Google Drive ファイル一覧を取得中...")
    files = list_all_files(service)

    # 3. 埋め込みモデル読み込み
    print(f"\n[3/5] 埋め込みモデル({EMBEDDING_MODEL})を読み込み中...")
    model = SentenceTransformer(EMBEDDING_MODEL)
    print("  モデル読み込み完了")

    # 4. テキスト分割器
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE,
        chunk_overlap=CHUNK_OVERLAP,
    )

    # 5. DB 接続
    print("\n[4/5] PostgreSQL に接続中...")
    conn = connect_db()
    print("  接続完了")

    # 6. ファイル処理
    print("\n[5/5] ファイルを処理中...")
    processed = 0
    skipped = 0
    errors = 0
    total_chunks = 0
    batch = []

    for i, file_info in enumerate(files):
        file_name = file_info["name"]
        print(f"  [{i+1}/{len(files)}] {file_name[:60]}...", end="")

        text = extract_text(service, file_info)
        if not text or not text.strip():
            skipped += 1
            print(" スキップ")
            continue

        # チャンク分割
        chunks = splitter.split_text(text)
        if not chunks:
            skipped += 1
            print(" スキップ（空）")
            continue

        # 埋め込み生成
        try:
            embeddings = model.encode(chunks)
        except Exception as e:
            errors += 1
            print(f" エラー: {e}")
            continue

        # メタデータ
        owner = ""
        if file_info.get("owners"):
            owner = file_info["owners"][0].get("emailAddress", "")

        for ci, (chunk_text, embedding) in enumerate(zip(chunks, embeddings)):
            batch.append({
                "drive_file_id": f"{file_info['id']}_chunk_{ci}",
                "title": file_name,
                "content": chunk_text,
                "chunk_index": ci,
                "owner": owner,
                "source_url": file_info.get("webViewLink", ""),
                "file_type": file_info["mimeType"],
                "drive_modified_at": file_info.get("modifiedTime"),
                "embedding": embedding.tolist(),
            })

        total_chunks += len(chunks)
        processed += 1
        print(f" OK ({len(chunks)} チャンク)")

        # バッチ挿入
        if len(batch) >= BATCH_SIZE:
            insert_chunks(conn, batch)
            batch = []

    # 残りのバッチを挿入
    if batch:
        insert_chunks(conn, batch)

    conn.close()

    # 結果表示
    print("\n" + "=" * 60)
    print("完了")
    print(f"  処理済みファイル: {processed}")
    print(f"  スキップ:         {skipped}")
    print(f"  エラー:           {errors}")
    print(f"  合計チャンク数:   {total_chunks}")
    print("=" * 60)


if __name__ == "__main__":
    main()
