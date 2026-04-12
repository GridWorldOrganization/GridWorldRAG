#!/usr/bin/env python3
"""
ocr_scan.py - 画像ファイルの後追い OCR → DB 追加

build_index.py で画像OCRをスキップした後、
ファイル単位・フォルダ単位で OCR を実行し DB に追加する。

使い方:
    # ファイル単位（Drive ファイル ID を指定）
    python ocr_scan.py --file-id 1ABCxyz

    # フォルダ単位（Drive フォルダ ID を指定）
    python ocr_scan.py --folder-id 0AFxyz

    # Google Workspace URL でも指定可能
    python ocr_scan.py --url "https://drive.google.com/file/d/1ABCxyz/view"
    python ocr_scan.py --url "https://drive.google.com/drive/folders/0AFxyz"

    # ドライラン（DB に書き込まず確認のみ）
    python ocr_scan.py --folder-id 0AFxyz --dry-run
"""

import argparse

from langchain_text_splitters import RecursiveCharacterTextSplitter
from sentence_transformers import SentenceTransformer

from src.config import EMBEDDING_MODEL, CHUNK_SIZE, CHUNK_OVERLAP, BATCH_SIZE, EMBEDDING_DEVICE
from src.drive_client import authenticate, extract_text, _download_content, _try_ocr_image
from src.db import connect, insert_chunks, extract_file_id_from_url


def _list_images_in_folder(service, folder_id, recursive=True):
    """フォルダ内の画像ファイルを取得する。"""
    images = []
    page_token = None

    while True:
        kwargs = {
            "q": f"'{folder_id}' in parents and trashed = false",
            "spaces": "drive",
            "fields": "nextPageToken, files(id, name, mimeType, modifiedTime, owners, webViewLink)",
            "pageSize": 1000,
            "supportsAllDrives": True,
            "includeItemsFromAllDrives": True,
        }
        if page_token:
            kwargs["pageToken"] = page_token

        response = service.files().list(**kwargs).execute()
        files = response.get("files", [])

        for f in files:
            if f["mimeType"].startswith("image/"):
                images.append(f)
            elif recursive and f["mimeType"] == "application/vnd.google-apps.folder":
                images.extend(_list_images_in_folder(service, f["id"], recursive=True))

        page_token = response.get("nextPageToken")
        if not page_token:
            break

    return images


def _get_file_info(service, file_id):
    """ファイル ID からメタデータを取得する。"""
    return service.files().get(
        fileId=file_id,
        fields="id, name, mimeType, modifiedTime, owners, webViewLink",
        supportsAllDrives=True,
    ).execute()


def main():
    parser = argparse.ArgumentParser(description="画像ファイルの後追い OCR → DB 追加")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--file-id", help="Drive ファイル ID")
    group.add_argument("--folder-id", help="Drive フォルダ ID（中の画像を再帰的に処理）")
    group.add_argument("--url", help="Google Drive の URL（ファイルまたはフォルダ）")
    parser.add_argument("--dry-run", action="store_true", help="DB に書き込まず確認のみ")
    args = parser.parse_args()

    # URL からID抽出
    if args.url:
        extracted_id = extract_file_id_from_url(args.url)
        if not extracted_id:
            print(f"エラー: URL からIDを抽出できません: {args.url}")
            return
        # フォルダかファイルかを判定
        service = authenticate()
        info = _get_file_info(service, extracted_id)
        if info["mimeType"] == "application/vnd.google-apps.folder":
            args.folder_id = extracted_id
        else:
            args.file_id = extracted_id
    else:
        service = authenticate()

    print("=" * 60)
    print("GridWorldRAG - OCR スキャン")
    print("=" * 60)

    # 対象ファイルを収集
    if args.file_id:
        file_info = _get_file_info(service, args.file_id)
        if not file_info["mimeType"].startswith("image/"):
            print(f"エラー: 画像ファイルではありません: {file_info['name']} ({file_info['mimeType']})")
            return
        target_files = [file_info]
        print(f"\n対象: {file_info['name']}")
    else:
        print(f"\nフォルダ内の画像を検索中...")
        target_files = _list_images_in_folder(service, args.folder_id)
        print(f"  {len(target_files)} 件の画像ファイルを発見")

    if not target_files:
        print("対象ファイルがありません。")
        return

    if args.dry_run:
        print("\n[ドライラン] 以下のファイルが処理対象:")
        for f in target_files:
            print(f"  - {f['name']} ({f['mimeType']})")
        print(f"\n合計: {len(target_files)} 件")
        return

    # モデル・DB準備
    print(f"\n埋め込みモデル({EMBEDDING_MODEL})を読み込み中...")
    model = SentenceTransformer(EMBEDDING_MODEL, device=(None if EMBEDDING_DEVICE == "auto" else EMBEDDING_DEVICE))
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE,
        chunk_overlap=CHUNK_OVERLAP,
    )
    conn = connect()

    # 処理
    processed = 0
    skipped = 0
    errors = 0
    total_chunks = 0
    batch = []

    for i, file_info in enumerate(target_files):
        print(f"  [{i+1}/{len(target_files)}] {file_info['name'][:60]}...", end="")

        try:
            content = _download_content(service, file_info["id"])
            text = _try_ocr_image(content, file_info["name"])
        except Exception as e:
            errors += 1
            print(f" エラー: {e}")
            continue

        if not text or not text.strip():
            skipped += 1
            print(" スキップ（テキストなし）")
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

        owner = ""
        if file_info.get("owners"):
            owner = file_info["owners"][0].get("emailAddress", "")

        for ci, (chunk_text, emb) in enumerate(zip(chunks, embeddings)):
            batch.append({
                "drive_file_id": f"{file_info['id']}_chunk_{ci}",
                "title": file_info["name"],
                "content": chunk_text,
                "chunk_index": ci,
                "owner": owner,
                "source_url": file_info.get("webViewLink", ""),
                "file_type": file_info["mimeType"],
                "drive_modified_at": file_info.get("modifiedTime"),
                "embedding": emb.tolist(),
                "sheet_gid": None,
                "sheet_name": None,
            })

        total_chunks += len(chunks)
        processed += 1
        print(f" OK ({len(chunks)} チャンク)")

        if len(batch) >= BATCH_SIZE:
            insert_chunks(conn, batch)
            batch = []

    if batch:
        insert_chunks(conn, batch)

    conn.close()

    print("\n" + "=" * 60)
    print("完了")
    print(f"  処理済み: {processed}")
    print(f"  スキップ: {skipped}")
    print(f"  エラー:   {errors}")
    print(f"  チャンク: {total_chunks}")
    print("=" * 60)


if __name__ == "__main__":
    main()
