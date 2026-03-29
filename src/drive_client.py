"""Google Drive API クライアント。

認証・ファイル一覧取得・テキスト抽出を提供する。
build_index.py, watcher.py など複数スクリプトから利用される。
"""

import io
import pickle

from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

from src.config import (
    GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_EMAIL, TOKEN_PATH,
    INDEX_MY_DRIVE, INDEX_SHARED_DRIVES, load_shared_drives_whitelist,
)

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

# authenticate() で保持する認証情報（Sheets API 等で再利用）
_credentials = None

# Google API レート制限対策: リトライ付き実行
import time as _time
import random as _random

def _api_call_with_retry(func, max_retries=5):
    """Google API 呼び出しをレート制限対応のリトライ付きで実行する。"""
    for attempt in range(max_retries):
        try:
            return func()
        except Exception as e:
            error_str = str(e)
            if "rate limit" in error_str.lower() or "429" in error_str or "quota" in error_str.lower():
                wait = (2 ** attempt) + _random.uniform(0, 1)
                _time.sleep(wait)
                continue
            raise
    return func()  # 最後の1回

# Google Docs 系の MIME タイプとエクスポート形式
# ※ スプレッドシートは Sheets API でシート別取得するため、ここには含めない
EXPORT_MIME_MAP = {
    "application/vnd.google-apps.document": "text/plain",
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

# スキップする MIME タイプ（テキスト抽出不可かつメタデータも不要なもの）
SKIP_MIME_TYPES = {
    "application/vnd.google-apps.shortcut",
    "application/vnd.google-apps.form",
    "application/vnd.google-apps.map",
    "application/vnd.google-apps.site",
}


def authenticate():
    """Google Drive API の認証を行い、サービスオブジェクトを返す。

    config.env の CLIENT_ID / CLIENT_SECRET を使って OAuth 認証する。
    初回はブラウザが開き、Google アカウントでログインが必要。
    認証後は token.pickle にトークンをキャッシュする。
    """
    global _credentials
    creds = None

    if TOKEN_PATH.exists():
        with open(TOKEN_PATH, "rb") as f:
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
        with open(TOKEN_PATH, "wb") as f:
            pickle.dump(creds, f)

    _credentials = creds
    return build("drive", "v3", credentials=creds)


_sheets_service_cache = None

def get_sheets_service():
    """Sheets API サービスオブジェクトを返す（drive.readonly スコープで動作）。"""
    global _sheets_service_cache
    if _sheets_service_cache is not None:
        return _sheets_service_cache
    if _credentials is None:
        raise RuntimeError("authenticate() を先に呼んでください")
    _sheets_service_cache = build("sheets", "v4", credentials=_credentials)
    return _sheets_service_cache


def extract_spreadsheet_sheets(file_id):
    """スプレッドシートの全シートをシート別に取得する。

    Returns:
        list[dict]: [{"gid": str, "name": str, "content": str}, ...]
    """
    service = get_sheets_service()
    try:
        spreadsheet = _api_call_with_retry(lambda: service.spreadsheets().get(
            spreadsheetId=file_id,
            includeGridData=False,
        ).execute())
    except Exception as e:
        print(f"  警告: スプレッドシート取得失敗: {e}")
        return []

    sheets = spreadsheet.get("sheets", [])
    results = []

    for sheet in sheets:
        props = sheet["properties"]
        gid = str(props["sheetId"])
        name = props["title"]

        try:
            sheet_range = f"'{name}'"
            values_response = _api_call_with_retry(lambda sr=sheet_range: service.spreadsheets().values().get(
                spreadsheetId=file_id,
                range=sr,
            ).execute())
            values = values_response.get("values", [])
            if not values:
                continue
            text = "\n".join("\t".join(str(cell) for cell in row) for row in values)
            if text.strip():
                results.append({"gid": gid, "name": name, "content": text})
        except Exception as e:
            print(f"  警告: シート '{name}' の取得失敗: {e}")

    return results


def list_files_in_drive(service, drive_id=None, corpora="user"):
    """指定スコープのファイルを全件取得する。"""
    files = []
    page_token = None
    query = "trashed = false"

    kwargs = {
        "q": query,
        "spaces": "drive",
        "fields": "nextPageToken, files(id, name, mimeType, modifiedTime, owners, webViewLink, driveId, permissions(emailAddress, role, type, displayName))",
        "pageSize": 1000,
        "supportsAllDrives": True,
        "includeItemsFromAllDrives": True,
    }

    if drive_id:
        kwargs["driveId"] = drive_id
        kwargs["corpora"] = "drive"
    else:
        kwargs["corpora"] = corpora

    while True:
        if page_token:
            kwargs["pageToken"] = page_token
        response = _api_call_with_retry(lambda: service.files().list(**kwargs).execute())

        batch = response.get("files", [])
        files.extend(batch)

        page_token = response.get("nextPageToken")
        if not page_token:
            break

    return files


def list_all_files(service):
    """設定に基づいて対象ファイルを取得する。

    config.env の INDEX_MY_DRIVE / INDEX_SHARED_DRIVES と
    shared_drives_whitelist.txt に基づきスコープを制御する。
    「共有アイテム」（他人から共有されたファイル）は常に対象外。
    """
    all_files = []

    # マイドライブ（自分が所有するファイルのみ、共有アイテムは除外）
    if INDEX_MY_DRIVE:
        print("  [マイドライブ] 取得中...")
        my_files = list_files_in_drive(service, corpora="user")
        # 共有アイテム除外: 自分がオーナーかつ共有ドライブ外のファイルのみ
        my_files = [
            f for f in my_files
            if not f.get("driveId")
            and f.get("owners")
            and any(o.get("emailAddress") == GOOGLE_EMAIL for o in f["owners"])
        ]
        print(f"  [マイドライブ] {len(my_files)} ファイル")
        all_files.extend(my_files)

    # 共有ドライブ
    if INDEX_SHARED_DRIVES:
        whitelist = load_shared_drives_whitelist()
        if not whitelist:
            print("  [共有ドライブ] ホワイトリストが空のためスキップ")
        else:
            # 共有ドライブの名前を取得
            drives_response = service.drives().list(pageSize=100).execute()
            drive_names = {d["id"]: d["name"] for d in drives_response.get("drives", [])}

            for drive_id in whitelist:
                drive_name = drive_names.get(drive_id, drive_id)
                print(f"  [共有ドライブ] {drive_name} 取得中...", end="")
                try:
                    drive_files = list_files_in_drive(service, drive_id=drive_id)
                    print(f" {len(drive_files)} ファイル")
                    all_files.extend(drive_files)
                except Exception as e:
                    print(f" エラー: {e}")

    print(f"  合計: {len(all_files)} ファイル")
    return all_files


def _download_content(service, file_id):
    """ファイルをバイナリでダウンロードする。"""
    request = _api_call_with_retry(lambda: service.files().get_media(fileId=file_id))
    content = io.BytesIO()
    downloader = MediaIoBaseDownload(content, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return content


def extract_text(service, file_info):
    """ファイルからテキストを抽出する。

    対応形式:
    - Google Docs / Sheets / Slides（エクスポート）
    - テキスト系ファイル（直接ダウンロード）
    - PDF（pypdf でテキスト抽出）
    """
    mime_type = file_info["mimeType"]
    file_id = file_info["id"]

    # フォルダ: フォルダ名をメタデータとして返す
    if mime_type == "application/vnd.google-apps.folder":
        return f"[フォルダ] {file_info['name']}"

    if mime_type in SKIP_MIME_TYPES:
        return None

    # スプレッドシートは extract_spreadsheet_sheets() で処理する
    if mime_type == "application/vnd.google-apps.spreadsheet":
        return None

    # Google Docs 系: エクスポート
    if mime_type in EXPORT_MIME_MAP:
        export_mime = EXPORT_MIME_MAP[mime_type]
        try:
            request = _api_call_with_retry(lambda: service.files().export_media(fileId=file_id, mimeType=export_mime))
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
            content = _download_content(service, file_id)
            return content.getvalue().decode("utf-8", errors="replace")
        except Exception as e:
            print(f"  警告: ダウンロード失敗 [{file_info['name']}]: {e}")
            return None

    # PDF: テキスト抽出（テキストベースPDFのみ、スキャンPDFは対象外）
    if mime_type == "application/pdf":
        try:
            content = _download_content(service, file_id)
            content.seek(0)
            try:
                import pypdf
                reader = pypdf.PdfReader(content)
                text = "\n".join(page.extract_text() or "" for page in reader.pages)
                if text.strip():
                    return text
            except ImportError:
                print("  警告: pypdf がインストールされていません。PDF はスキップします。")
                return f"[PDF] {file_info['name']}"
            return f"[スキャンPDF] {file_info['name']}"
        except Exception as e:
            print(f"  警告: PDF 処理失敗 [{file_info['name']}]: {e}")
            return f"[PDF] {file_info['name']}"

    # 画像: OCR 試行 → メタデータ
    if mime_type.startswith("image/"):
        try:
            content = _download_content(service, file_id)
            return _try_ocr_image(content, file_info["name"])
        except Exception as e:
            print(f"  警告: 画像処理失敗 [{file_info['name']}]: {e}")
            return f"[画像] {file_info['name']}"

    # 動画: メタデータのみ
    if mime_type.startswith("video/"):
        return f"[動画] {file_info['name']}"

    # 音声: メタデータのみ
    if mime_type.startswith("audio/"):
        return f"[音声] {file_info['name']}"

    return None


def _try_ocr_image(content, filename):
    """画像から OCR テキスト抽出を試行する。"""
    try:
        import pytesseract
        from PIL import Image
        content.seek(0)
        img = Image.open(content)
        text = pytesseract.image_to_string(img, lang="jpn+eng")
        if text.strip():
            return f"[画像] {filename}\n{text}"
    except ImportError:
        pass
    except Exception:
        pass
    return f"[画像] {filename}"


def get_changes_start_token(service):
    """Changes API の開始トークンを取得する。"""
    response = service.changes().getStartPageToken().execute()
    return response["startPageToken"]


def list_changes(service, page_token):
    """前回トークン以降の変更ファイルを取得する。"""
    changes = []
    while True:
        response = service.changes().list(
            pageToken=page_token,
            spaces="drive",
            includeItemsFromAllDrives=True,
            supportsAllDrives=True,
            fields="nextPageToken, newStartPageToken, changes(fileId, removed, file(id, name, mimeType, modifiedTime, owners, webViewLink))",
        ).execute()

        changes.extend(response.get("changes", []))

        if "newStartPageToken" in response:
            new_token = response["newStartPageToken"]
            return changes, new_token

        page_token = response["nextPageToken"]
