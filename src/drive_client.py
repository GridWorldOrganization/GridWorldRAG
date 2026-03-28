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
    GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, TOKEN_PATH,
    INDEX_MY_DRIVE, INDEX_SHARED_DRIVES, load_shared_drives_whitelist,
)

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

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
    初回はブラウザが開き、Google アカウントでログインが必要。
    認証後は token.pickle にトークンをキャッシュする。
    """
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

    return build("drive", "v3", credentials=creds)


def _list_files_in_drive(service, drive_id=None, corpora="user"):
    """指定スコープのファイルを全件取得する内部関数。"""
    files = []
    page_token = None
    query = "trashed = false"

    kwargs = {
        "q": query,
        "spaces": "drive",
        "fields": "nextPageToken, files(id, name, mimeType, modifiedTime, owners, webViewLink, driveId)",
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
        response = service.files().list(**kwargs).execute()

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

    # マイドライブ
    if INDEX_MY_DRIVE:
        print("  [マイドライブ] 取得中...")
        # corpora="user" + 'me' in owners でマイドライブ所有ファイルのみ
        my_files = _list_files_in_drive(service, corpora="user")
        # 「共有アイテム」を除外: owners に自分が含まれるもののみ
        my_files = [f for f in my_files if not f.get("driveId")]
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
                    drive_files = _list_files_in_drive(service, drive_id=drive_id)
                    print(f" {len(drive_files)} ファイル")
                    all_files.extend(drive_files)
                except Exception as e:
                    print(f" エラー: {e}")

    print(f"  合計: {len(all_files)} ファイル")
    return all_files


def _download_content(service, file_id):
    """ファイルをバイナリでダウンロードする。"""
    request = service.files().get_media(fileId=file_id)
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
            content = _download_content(service, file_id)
            return content.getvalue().decode("utf-8", errors="replace")
        except Exception as e:
            print(f"  警告: ダウンロード失敗 [{file_info['name']}]: {e}")
            return None

    # PDF: テキスト抽出
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
            return None
        except Exception as e:
            print(f"  警告: PDF 処理失敗 [{file_info['name']}]: {e}")
            return None

    return None


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
