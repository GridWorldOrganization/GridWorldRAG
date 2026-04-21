"""Index construction helpers (shared by daemon)."""
from __future__ import annotations

from typing import Iterator


def extract_permissions(file_info: dict):
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


def extract_owner(file_info: dict) -> str:
    owners = file_info.get("owners")
    if owners and isinstance(owners, list) and len(owners) > 0:
        owner = owners[0]
        if isinstance(owner, dict):
            return owner.get("emailAddress", "")
    return ""


def make_chunk_entry(file_info: dict, chunk_text: str, embedding,
                     chunk_index: int, *, sheet_gid: str | None = None,
                     sheet_name: str | None = None,
                     partial_content: bool = False) -> dict:
    if sheet_gid is not None:
        drive_file_id = f"{file_info['id']}_sheet_{sheet_gid}_chunk_{chunk_index}"
    else:
        drive_file_id = f"{file_info['id']}_chunk_{chunk_index}"
    return {
        "drive_file_id": drive_file_id,
        "title": file_info.get("name", ""),
        "content": chunk_text,
        "chunk_index": chunk_index,
        "owner": extract_owner(file_info),
        "source_url": file_info.get("webViewLink", ""),
        "file_type": file_info.get("mimeType", ""),
        "drive_modified_at": file_info.get("modifiedTime"),
        "embedding": embedding.tolist() if hasattr(embedding, "tolist") else list(embedding),
        "sheet_gid": sheet_gid,
        "sheet_name": sheet_name,
        "permissions": extract_permissions(file_info),
        "partial_content": partial_content,
        "folder_path": file_info.get("folder_path", ""),
    }


def chunk_text(text: str, chunk_size: int, overlap: int) -> Iterator[str]:
    """Simple character-window chunker. Tokenizer-aware chunking is not needed at this stage."""
    if not text:
        return
    if chunk_size <= 0:
        yield text
        return
    step = max(1, chunk_size - max(0, overlap))
    i = 0
    n = len(text)
    while i < n:
        yield text[i:i + chunk_size]
        i += step
