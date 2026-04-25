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


import re as _re

# Markdown heading or blank-line delimited paragraph boundary
_HEADING_RE = _re.compile(r"^#{1,6}\s", _re.MULTILINE)


def _split_into_sections(text: str) -> list[str]:
    """Split text into sections on Markdown headings. Preserves content
    following each heading until the next heading."""
    if not text:
        return []
    matches = list(_HEADING_RE.finditer(text))
    if not matches:
        return [text]
    sections: list[str] = []
    last_end = 0
    for i, m in enumerate(matches):
        if m.start() > last_end:
            prefix = text[last_end:m.start()].strip()
            if prefix:
                sections.append(prefix)
        next_start = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        sections.append(text[m.start():next_start].strip())
        last_end = next_start
    return [s for s in sections if s]


def chunk_text(text: str, chunk_size: int, overlap: int) -> Iterator[str]:
    """Structure-aware chunker.

    Strategy:
      1. Split by Markdown headings first (preserves section boundaries)
      2. For each section, split by paragraph (\\n\\n+) if too big
      3. Greedy combine paragraphs into chunks <= chunk_size
      4. Fall back to char-window split for single paragraphs exceeding chunk_size
    """
    if not text:
        return
    if chunk_size <= 0:
        yield text
        return

    sections = _split_into_sections(text)
    if not sections:
        sections = [text]

    paragraph_re = _re.compile(r"\n\s*\n+")
    step = max(1, chunk_size - max(0, overlap))

    buf = ""
    for section in sections:
        paragraphs = paragraph_re.split(section)
        for para in paragraphs:
            p = para.strip()
            if not p:
                continue

            # Oversized paragraph — flush buffer, then char-window split.
            if len(p) > chunk_size:
                if buf:
                    yield buf
                    buf = ""
                i = 0
                while i < len(p):
                    yield p[i:i + chunk_size]
                    i += step
                continue

            # Fits in buffer?
            if len(buf) + 2 + len(p) <= chunk_size:
                buf = (buf + "\n\n" + p) if buf else p
            else:
                if buf:
                    yield buf
                buf = p
    if buf:
        yield buf
