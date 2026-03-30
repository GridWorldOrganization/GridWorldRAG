"""インデックス構築の共通処理。

build_single.py / build_parallel.py から利用される。
"""


def extract_permissions(file_info):
    """ファイルのパーミッション情報を抽出する。"""
    perms = file_info.get("permissions", [])
    if not perms:
        return None
    return [
        {
            "email": p.get("emailAddress", ""),
            "name": p.get("displayName", ""),
            "role": p.get("role", ""),
            "type": p.get("type", ""),
        }
        for p in perms
    ]


def extract_owner(file_info):
    """ファイルのオーナーメールアドレスを取得する。"""
    owners = file_info.get("owners")
    if owners and isinstance(owners, list) and len(owners) > 0:
        owner = owners[0]
        if isinstance(owner, dict):
            return owner.get("emailAddress", "")
    return ""


def make_chunk_entry(file_info, chunk_text, embedding, chunk_index,
                     sheet_gid=None, sheet_name=None, partial_content=False):
    """1チャンク分の DB 挿入データを作成する。"""
    if sheet_gid is not None:
        drive_file_id = f"{file_info['id']}_sheet_{sheet_gid}_chunk_{chunk_index}"
    else:
        drive_file_id = f"{file_info['id']}_chunk_{chunk_index}"

    return {
        "drive_file_id": drive_file_id,
        "title": file_info["name"],
        "content": chunk_text,
        "chunk_index": chunk_index,
        "owner": extract_owner(file_info),
        "source_url": file_info.get("webViewLink", ""),
        "file_type": file_info["mimeType"],
        "drive_modified_at": file_info.get("modifiedTime"),
        "embedding": embedding.tolist() if hasattr(embedding, "tolist") else list(embedding),
        "sheet_gid": sheet_gid,
        "sheet_name": sheet_name,
        "permissions": extract_permissions(file_info),
        "partial_content": partial_content,
        "folder_path": file_info.get("folder_path", ""),
    }
