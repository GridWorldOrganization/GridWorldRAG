"""Google Drive API client.

Handles authentication, per-drive enumeration, Changes-API deltas, and
content extraction (Docs export, Sheets, PDF, OCR). Windows-native
paths via pathlib throughout. Called from `rag_daemon` workers and
the `control_api` discovery endpoints.
"""
from __future__ import annotations

import io
import pickle
import random as _random
import socket
import time as _time
from pathlib import Path

from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

from src.config import (
    GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, TOKEN_PATH,
    DRIVE_DOWNLOAD_TIMEOUT_SEC,
    API_MAX_RETRIES, API_BASE_DELAY_SEC, API_SHEET_MAX_RETRIES,
    INDEX_IMAGE_OCR,
)

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

_credentials = None
_rate_limit_callback = None
_sheets_service_cache = None

# Thread-local services (one Drive/Sheets pair per worker thread).
import threading as _threading
_thread_local = _threading.local()

# Sheets API per-minute quota is 60 read requests/minute per user. With
# Sheets API v4 project quota is 300 read/min. Two concurrent callers at
# ~300ms each produce ~400/min, so we still need throttling. Keep the
# concurrency at 2 and release *between* each sheet's HTTP call (see
# extract_spreadsheet_sheets) instead of holding the token for the whole
# spreadsheet. Raising this to 4 in testing caused bursty 429s whose 5s
# exponential backoff (API_BASE_DELAY_SEC) stacked into 40+ second
# stalls, worse than the serialization we were trying to eliminate.
_sheets_semaphore = _threading.Semaphore(2)


def set_rate_limit_callback(cb):
    global _rate_limit_callback
    _rate_limit_callback = cb


def _is_retriable_error(error_str: str) -> bool:
    s = error_str.lower()
    if "rate limit" in s or "429" in s or "quota" in s:
        return True
    for code in ("500", "502", "503", "504"):
        if code in error_str:
            return True
    for k in ("internal error", "bad gateway", "service unavailable",
              "gateway timeout", "backenderror",
              "connection reset", "connection aborted"):
        if k in s:
            return True
    return False


def _api_call_with_retry(func, max_retries=None, base_delay=None):
    if max_retries is None:
        max_retries = API_MAX_RETRIES
    if base_delay is None:
        base_delay = API_BASE_DELAY_SEC
    for attempt in range(max_retries):
        try:
            return func()
        except Exception as e:
            if not _is_retriable_error(str(e)):
                raise
            if _rate_limit_callback:
                _rate_limit_callback(True)
            wait = base_delay * (2 ** attempt) + _random.uniform(0, base_delay)
            _time.sleep(wait)
            if _rate_limit_callback:
                _rate_limit_callback(False)
    return func()


EXPORT_MIME_MAP = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.presentation": "text/plain",
}
TEXT_MIME_TYPES = {
    "text/plain", "text/markdown", "text/csv", "text/html",
    "application/json", "application/xml",
}
SKIP_MIME_TYPES = {
    "application/vnd.google-apps.shortcut",
    "application/vnd.google-apps.form",
    "application/vnd.google-apps.map",
    "application/vnd.google-apps.site",
}


def authenticate():
    """Return an authenticated Drive v3 service."""
    global _credentials
    creds = None
    token_path = Path(TOKEN_PATH)
    if token_path.exists():
        with open(token_path, "rb") as f:
            creds = pickle.load(f)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            _api_call_with_retry(lambda: creds.refresh(Request()))
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
        token_path.parent.mkdir(parents=True, exist_ok=True)
        with open(token_path, "wb") as f:
            pickle.dump(creds, f)

    _credentials = creds
    import httplib2, google_auth_httplib2
    http = google_auth_httplib2.AuthorizedHttp(
        creds, http=httplib2.Http(timeout=DRIVE_DOWNLOAD_TIMEOUT_SEC))
    return build("drive", "v3", http=http)


def _build_drive_service_for_creds(creds):
    import httplib2, google_auth_httplib2
    http = google_auth_httplib2.AuthorizedHttp(
        creds, http=httplib2.Http(timeout=DRIVE_DOWNLOAD_TIMEOUT_SEC))
    return build("drive", "v3", http=http)


def _build_sheets_service_for_creds(creds):
    import httplib2, google_auth_httplib2
    http = google_auth_httplib2.AuthorizedHttp(
        creds, http=httplib2.Http(timeout=DRIVE_DOWNLOAD_TIMEOUT_SEC))
    return build("sheets", "v4", http=http)


def get_sheets_service():
    """Return a thread-local Sheets v4 service.

    googleapiclient service objects share an internal httplib2.Http which is
    not thread-safe. Each worker thread gets its own instance.
    """
    if _credentials is None:
        raise RuntimeError("call authenticate() first")
    svc = getattr(_thread_local, "sheets", None)
    if svc is None:
        svc = _build_sheets_service_for_creds(_credentials)
        _thread_local.sheets = svc
    return svc


def get_drive_service():
    """Return a thread-local Drive v3 service. Workers call this instead of
    reusing the main-thread service returned by authenticate()."""
    if _credentials is None:
        raise RuntimeError("call authenticate() first")
    svc = getattr(_thread_local, "drive", None)
    if svc is None:
        svc = _build_drive_service_for_creds(_credentials)
        _thread_local.drive = svc
    return svc


# ---------------------------------------------------------------------
# Shared drives enumeration
# ---------------------------------------------------------------------
def list_shared_drives(service) -> list[dict]:
    drives = []
    page_token = None
    while True:
        kwargs = {"pageSize": 100, "fields": "nextPageToken, drives(id, name, createdTime)"}
        if page_token:
            kwargs["pageToken"] = page_token
        resp = _api_call_with_retry(lambda: service.drives().list(**kwargs).execute())
        drives.extend(resp.get("drives", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return drives


# ---------------------------------------------------------------------
# File listing / text extraction (single drive)
# ---------------------------------------------------------------------
_FIELDS = ("nextPageToken, files(id, name, mimeType, modifiedTime, "
           "size, quotaBytesUsed, owners, webViewLink, driveId, "
           "parents, permissions(emailAddress, role, type, displayName))")


def count_files_in_drive(service, drive_id: str) -> int:
    """Cheap file count: only requests `id` field, no folder paths or
    permissions, no body. Used by the control API's pre-enable preview
    so the UI can show "this drive has ~N files" before the user enables
    indexing. Uses the same `trashed=false` filter as list_files_in_drive
    so the number matches what an actual build would enumerate.
    """
    total = 0
    page_token = None
    kwargs_base = {
        "q": "trashed = false",
        "spaces": "drive",
        "fields": "files(id), nextPageToken",
        "pageSize": 1000,
        "supportsAllDrives": True,
        "includeItemsFromAllDrives": True,
        "driveId": drive_id,
        "corpora": "drive",
    }
    while True:
        kwargs = dict(kwargs_base)
        if page_token:
            kwargs["pageToken"] = page_token
        resp = _api_call_with_retry(lambda: service.files().list(**kwargs).execute())
        total += len(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return total


def list_files_in_drive(service, drive_id: str) -> list[dict]:
    files: list[dict] = []
    page_token = None
    kwargs_base = {
        "q": "trashed = false",
        "spaces": "drive",
        "fields": _FIELDS,
        "pageSize": 1000,
        "supportsAllDrives": True,
        "includeItemsFromAllDrives": True,
        "driveId": drive_id,
        "corpora": "drive",
    }
    while True:
        kwargs = dict(kwargs_base)
        if page_token:
            kwargs["pageToken"] = page_token
        resp = _api_call_with_retry(lambda: service.files().list(**kwargs).execute())
        files.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return files


def attach_folder_paths(files: list[dict], drive_name: str = "") -> list[dict]:
    FOLDER_MIME = "application/vnd.google-apps.folder"
    folder_map = {
        f["id"]: {"name": f["name"], "parents": f.get("parents", [])}
        for f in files if f.get("mimeType") == FOLDER_MIME
    }
    cache: dict[str, str] = {}

    def _resolve(fid, visited=None):
        if fid in cache:
            return cache[fid]
        if visited is None:
            visited = set()
        if fid in visited:
            return ""
        visited.add(fid)
        info = folder_map.get(fid)
        if not info:
            result = drive_name
        else:
            parents = info.get("parents", [])
            parent_path = _resolve(parents[0], visited) if parents else drive_name
            result = f"{parent_path} / {info['name']}" if parent_path else info["name"]
        cache[fid] = result
        return result

    for f in files:
        parents = f.get("parents", [])
        f["folder_path"] = _resolve(parents[0]) if parents else drive_name
        f["drive_name"] = drive_name
    return files


# ---------------------------------------------------------------------
# Text extraction
# ---------------------------------------------------------------------
def _download_content(service, file_id):
    request = _api_call_with_retry(lambda: service.files().get_media(fileId=file_id))
    content = io.BytesIO()
    downloader = MediaIoBaseDownload(content, request)
    done = False
    retries = 0
    while not done:
        try:
            _, done = downloader.next_chunk()
            retries = 0
        except Exception as e:
            if not _is_retriable_error(str(e)):
                raise
            retries += 1
            if retries > 3:
                raise
            _time.sleep(5 * (2 ** (retries - 1)))
    return content


def _extract_pdf_with_timeout(content, timeout_sec: int = 60):
    import pypdf, threading
    collected: list[str] = []
    error: list = [None]

    def _run():
        try:
            reader = pypdf.PdfReader(content)
            for page in reader.pages:
                collected.append(page.extract_text() or "")
        except Exception as e:
            error[0] = e

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout=timeout_sec)
    is_partial = t.is_alive()
    text = "\n".join(collected)
    if not text.strip():
        return None, False
    return text, is_partial


def extract_text(service, file_info):
    mime_type = file_info["mimeType"]
    file_id = file_info["id"]
    file_name = file_info["name"]

    if mime_type == "application/vnd.google-apps.folder":
        return f"[フォルダ] {file_name}", False
    if mime_type in SKIP_MIME_TYPES:
        return None, False
    if mime_type == "application/vnd.google-apps.spreadsheet":
        return None, False

    if mime_type in EXPORT_MIME_MAP:
        export_mime = EXPORT_MIME_MAP[mime_type]
        try:
            def _do_export():
                request = _api_call_with_retry(
                    lambda: service.files().export_media(fileId=file_id, mimeType=export_mime))
                content = io.BytesIO()
                downloader = MediaIoBaseDownload(content, request)
                done = False
                while not done:
                    _, done = downloader.next_chunk()
                return content
            content = _do_export()
            return content.getvalue().decode("utf-8", errors="replace"), False
        except (socket.timeout, OSError):
            return f"[Doc] {file_name}", True
        except Exception:
            return f"[Doc] {file_name}", True

    if mime_type in TEXT_MIME_TYPES:
        try:
            content = _download_content(service, file_id)
            return content.getvalue().decode("utf-8", errors="replace"), False
        except (socket.timeout, OSError):
            return f"[テキスト] {file_name}", True
        except Exception:
            return f"[テキスト] {file_name}", True

    if mime_type == "application/pdf":
        try:
            content = _download_content(service, file_id)
            content.seek(0)
            try:
                text, is_partial = _extract_pdf_with_timeout(content, 60)
                if text:
                    return text, is_partial
            except ImportError:
                return f"[PDF] {file_name}", False
            return f"[スキャンPDF] {file_name}", False
        except (socket.timeout, OSError):
            return f"[PDF] {file_name}", False
        except Exception:
            return f"[PDF] {file_name}", False

    if mime_type.startswith("image/"):
        if not INDEX_IMAGE_OCR:
            return f"[画像] {file_name}", False
        try:
            content = _download_content(service, file_id)
            return _try_ocr_image(content, file_name), False
        except Exception:
            return f"[画像] {file_name}", False

    if mime_type.startswith("video/"):
        return f"[動画] {file_name}", False
    if mime_type.startswith("audio/"):
        return f"[音声] {file_name}", False

    return f"[ファイル] {file_name}", True


def _try_ocr_image(content, filename):
    try:
        import pytesseract
        from PIL import Image
        content.seek(0)
        img = Image.open(content)
        text = pytesseract.image_to_string(img, lang="jpn+eng")
        if text.strip():
            return f"[画像] {filename}\n{text}"
    except Exception:
        pass
    return f"[画像] {filename}"


def extract_spreadsheet_sheets(file_id: str):
    # Guard Sheets API usage with a process-wide semaphore so a pool of
    # workers can't collectively blow the per-project quota. IMPORTANT:
    # acquire once per individual HTTP call, NOT around the whole
    # spreadsheet. A 30-sheet file previously held the semaphore for
    # (30+1) * round-trip seconds, completely starving other workers.
    service = get_sheets_service()
    try:
        with _sheets_semaphore:
            spreadsheet = _api_call_with_retry(
                lambda: service.spreadsheets().get(spreadsheetId=file_id, includeGridData=False).execute()
            )
    except Exception:
        return []
    sheets = spreadsheet.get("sheets", [])
    results = []
    for sheet in sheets:
        try:
            props = sheet["properties"]
            gid = str(props["sheetId"])
            name = props["title"]
        except (KeyError, TypeError):
            continue
        try:
            sheet_range = f"'{name}'"
            with _sheets_semaphore:
                resp = _api_call_with_retry(
                    lambda sr=sheet_range: service.spreadsheets().values().get(
                        spreadsheetId=file_id, range=sr).execute(),
                    max_retries=API_SHEET_MAX_RETRIES,
                )
            values = resp.get("values", [])
            if not values:
                results.append({"gid": gid, "name": name, "content": None, "failed": False})
                continue
            text = "\n".join("\t".join(str(c) for c in row) for row in values)
            results.append({
                "gid": gid, "name": name,
                "content": text if text.strip() else None, "failed": False})
        except Exception:
            results.append({"gid": gid, "name": name, "content": None, "failed": True})
    return results


# ---------------------------------------------------------------------
# Changes API
# ---------------------------------------------------------------------
_CHANGES_FIELDS = (
    "nextPageToken, newStartPageToken, "
    "changes(fileId, removed, file("
    "id, name, mimeType, modifiedTime, trashed, owners, "
    "webViewLink, driveId, parents, "
    "permissions(emailAddress, role, type, displayName)))"
)


def get_changes_start_token(service, drive_id: str) -> str:
    resp = _api_call_with_retry(
        lambda: service.changes().getStartPageToken(
            supportsAllDrives=True, driveId=drive_id).execute()
    )
    return resp["startPageToken"]


def list_changes(service, page_token: str, drive_id: str):
    """Return (changes, new_page_token) covering everything since page_token."""
    changes: list = []
    while True:
        kwargs = {
            "pageToken": page_token,
            "includeItemsFromAllDrives": True,
            "supportsAllDrives": True,
            "fields": _CHANGES_FIELDS,
            "driveId": drive_id,
        }
        resp = _api_call_with_retry(lambda: service.changes().list(**kwargs).execute())
        changes.extend(resp.get("changes", []))
        if "newStartPageToken" in resp:
            return changes, resp["newStartPageToken"]
        page_token = resp["nextPageToken"]


def get_file_info(service, file_id: str) -> dict | None:
    """Fetch metadata for a single file (used by failed_files retry)."""
    try:
        return _api_call_with_retry(
            lambda: service.files().get(
                fileId=file_id,
                fields=("id, name, mimeType, modifiedTime, owners, webViewLink, "
                        "driveId, parents, trashed, "
                        "permissions(emailAddress, role, type, displayName)"),
                supportsAllDrives=True,
            ).execute()
        )
    except Exception:
        return None
