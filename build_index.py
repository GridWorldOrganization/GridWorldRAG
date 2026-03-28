#!/usr/bin/env python3
"""
build_index.py - Google Drive 全件インデックス構築

Google Drive の全ファイルを取得し、テキスト抽出 → チャンク分割 →
埋め込み生成 → PostgreSQL + pgvector に一括投入する。
"""

from langchain_text_splitters import RecursiveCharacterTextSplitter
from sentence_transformers import SentenceTransformer

from src.config import EMBEDDING_MODEL, CHUNK_SIZE, CHUNK_OVERLAP, BATCH_SIZE
from src.drive_client import authenticate, list_all_files, extract_text
from src.db import connect, insert_chunks


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
    conn = connect()
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
