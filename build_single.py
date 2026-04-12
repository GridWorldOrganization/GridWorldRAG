#!/usr/bin/env python3
"""
build_single.py - Google Drive 全件インデックス構築（シングルプロセス版）

Google Drive の全ファイルを取得し、テキスト抽出 → チャンク分割 →
埋め込み生成 → PostgreSQL + pgvector に一括投入する。
"""

from langchain_text_splitters import RecursiveCharacterTextSplitter
from sentence_transformers import SentenceTransformer

from src.config import EMBEDDING_MODEL, CHUNK_SIZE, CHUNK_OVERLAP, BATCH_SIZE, INDEX_IMAGE_OCR, EMBEDDING_DEVICE
from src.drive_client import (
    authenticate, list_all_files, extract_text,
    extract_spreadsheet_sheets,
)
from src.db import connect, insert_chunks
from src.indexer import make_chunk_entry


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
    model = SentenceTransformer(EMBEDDING_MODEL, device=(None if EMBEDDING_DEVICE == "auto" else EMBEDDING_DEVICE))
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
        mime_type = file_info["mimeType"]
        print(f"  [{i+1}/{len(files)}] {file_name[:60]}...", end="")

        # スプレッドシート: シート別処理
        if mime_type == "application/vnd.google-apps.spreadsheet":
            sheets = extract_spreadsheet_sheets(file_info["id"])
            if not sheets:
                skipped += 1
                print(" スキップ（シートなし）")
                continue

            file_chunks = 0
            for sheet in sheets:
                is_partial = sheet.get("failed", False)
                content = sheet.get("content")
                if content:
                    sheet_text = f"[シート: {sheet['name']}]\n{content}"
                else:
                    sheet_text = f"[シート: {sheet['name']}]"

                chunks = splitter.split_text(sheet_text)
                if not chunks:
                    chunks = [sheet_text]
                try:
                    embeddings = model.encode(chunks)
                except Exception as e:
                    errors += 1
                    print(f" エラー({sheet['name']}): {e}")
                    continue

                for ci, (chunk_text, emb) in enumerate(zip(chunks, embeddings)):
                    batch.append(make_chunk_entry(
                        file_info, chunk_text, emb, ci,
                        sheet_gid=sheet["gid"], sheet_name=sheet["name"],
                        partial_content=is_partial,
                    ))
                file_chunks += len(chunks)

            partial_count = sum(1 for s in sheets if s.get("failed"))
            if file_chunks > 0:
                total_chunks += file_chunks
                processed += 1
                suffix = f" [{partial_count}シート partial]" if partial_count else ""
                print(f" OK ({len(sheets)} シート, {file_chunks} チャンク){suffix}")
            else:
                skipped += 1
                print(" スキップ（空）")

        # その他のファイル
        else:
            # 画像: OCR無効時はファイル名のみDB投入
            if mime_type.startswith("image/") and not INDEX_IMAGE_OCR:
                text = f"[画像] {file_name}"
                try:
                    emb = model.encode([text])[0]
                    batch.append(make_chunk_entry(file_info, text, emb, 0))
                    total_chunks += 1
                    processed += 1
                    print(" OK (メタデータ)")
                except Exception:
                    errors += 1
                    print(" エラー")
                continue

            text, is_partial = extract_text(service, file_info)
            if not text or not text.strip():
                skipped += 1
                print(" スキップ")
                continue

            chunks = splitter.split_text(text)
            if not chunks:
                skipped += 1
                print(" スキップ（空）")
                continue

            try:
                embeddings = model.encode(chunks)
            except Exception as e:
                errors += 1
                print(f" エラー: {e}")
                continue

            for ci, (chunk_text, emb) in enumerate(zip(chunks, embeddings)):
                batch.append(make_chunk_entry(file_info, chunk_text, emb, ci, partial_content=is_partial))

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
