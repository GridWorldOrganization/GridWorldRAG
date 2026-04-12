"""Google Drive API クライアント。

認証・ファイル一覧取得・テキスト抽出を提供する。
build_parallel.py, sync.py, gridworld-rag-mcp/server.py から利用される。
"""

import io
import pickle
import socket

from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

from src.config import (
    GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_EMAIL, TOKEN_PATH,
    INDEX_MY_DRIVE, INDEX_SHARED_DRIVES, load_shared_drives_whitelist,
    DRIVE_DOWNLOAD_TIMEOUT_SEC,
)

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

# authenticate() で保持する認証情報（Sheets API 等で再利用）
_credentials = None

# Google API レート制限対策: リトライ付き実行
import time as _time
import random as _random

_rate_limit_callback = None  # レート制限発生時のコールバック

def set_rate_limit_callback(callback):
    """レート制限発生時に呼ばれるコールバックを設定する。callback(waiting: bool)"""
    global _rate_limit_callback
    _rate_limit_callback = callback

def _is_retriable_error(error_str):
    """Google API エラーが一時的で再試行すべきかを判定する。

    - Rate limit / quota: 429 相当
    - Server errors: 500/502/503/504 (Internal Error / Bad Gateway /
      Service Unavailable / Gateway Timeout)
    - Connection errors: 接続リセット、タイムアウトなど

    db4 ビルド時に HTTP 500 (Internal Error) で GW_PJ 10,769 ファイルが
    silently 欠落する事故があり、5xx もリトライ対象に追加した。
    """
    s = error_str.lower()
    if "rate limit" in s or "429" in s or "quota" in s:
        return True
    # HTTP 5xx server errors
    for code in ("500", "502", "503", "504"):
        if code in error_str:
            return True
    if "internal error" in s or "bad gateway" in s or "service unavailable" in s:
        return True
    if "gateway timeout" in s or "backenderror" in s:
        return True
    # Connection level
    if "connection reset" in s or "connection aborted" in s:
        return True
    return False


def _api_call_with_retry(func, max_retries=6, base_delay=5):
    """Google API 呼び出しをレート制限・一時的サーバエラー対応のリトライ付きで実行する。

    リトライ対象:
    - 429 Rate Limit / Quota Exceeded
    - HTTP 5xx (500 Internal Error, 502 Bad Gateway, 503 Service Unavailable,
      504 Gateway Timeout)
    - Connection reset / aborted
    _is_retriable_error() を参照。

    Sheets API の per-minute クォータ (60req/min) 回復には最大 60 秒かかるため、
    バックオフの累計待ち時間が 60 秒を超えるように設計する:
        base_delay * (2^0 + 2^1 + ... + 2^5) = 5 * 63 = 315 秒 (理論最大)
    実際は途中で成功するので平均的な待ち時間は数十秒〜1分程度。

    Args:
        func: 実行する関数（引数なしの callable）
        max_retries: 最大試行回数（デフォルト 6）
        base_delay: バックオフの基準遅延秒（デフォルト 5 秒）
    """
    for attempt in range(max_retries):
        try:
            return func()
        except Exception as e:
            error_str = str(e)
            if _is_retriable_error(error_str):
                if _rate_limit_callback:
                    _rate_limit_callback(True)
                # 指数バックオフ + ジッター: base_delay * 2^attempt + [0, base_delay) ランダム
                wait = base_delay * (2 ** attempt) + _random.uniform(0, base_delay)
                _time.sleep(wait)
                if _rate_limit_callback:
                    _rate_limit_callback(False)
                continue
            raise
    return func()  # 最後の1回（例外は呼び出し元に伝搬）

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


def _extract_pdf_with_timeout(content, timeout_sec=60):
    """PDFテキストをタイムアウト付きで1ページずつ抽出する。

    threading ベース（SIGALRM は非メインスレッドで使えないため）。

    Returns:
        (text, is_partial): text は抽出テキスト（Noneの場合は抽出不可）、
                            is_partial は途中でタイムアウトした場合 True。
    """
    import pypdf, threading
    collected = []
    error = [None]

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
    is_partial = t.is_alive()  # まだ動いている = タイムアウト

    text = "\n".join(collected)
    if not text.strip():
        return None, False
    return text, is_partial


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
    # httplib2 にソケットタイムアウトを設定（SSL ハング防止）
    # daemon スレッドでのタイムアウト制御は SSL double-free を引き起こすため、
    # ソケットレベルでタイムアウトさせる
    import httplib2, google_auth_httplib2
    authorized_http = google_auth_httplib2.AuthorizedHttp(
        creds, http=httplib2.Http(timeout=DRIVE_DOWNLOAD_TIMEOUT_SEC))
    return build("drive", "v3", http=authorized_http)


_sheets_service_cache = None

def get_sheets_service():
    """Sheets API サービスオブジェクトを返す（drive.readonly スコープで動作）。"""
    global _sheets_service_cache
    if _sheets_service_cache is not None:
        return _sheets_service_cache
    if _credentials is None:
        raise RuntimeError("authenticate() を先に呼んでください")
    import httplib2, google_auth_httplib2
    authorized_http = google_auth_httplib2.AuthorizedHttp(
        _credentials, http=httplib2.Http(timeout=DRIVE_DOWNLOAD_TIMEOUT_SEC))
    _sheets_service_cache = build("sheets", "v4", http=authorized_http)
    return _sheets_service_cache


def extract_spreadsheet_sheets(file_id):
    """スプレッドシートの全シートをシート別に取得する。

    シート値の取得に失敗した場合も "failed": True でシート名を返す（partial保存用）。

    Returns:
        list[dict]: [{"gid": str, "name": str, "content": str|None, "failed": bool}, ...]
    """
    service = get_sheets_service()
    # メタデータ取得（シート名一覧）はリトライあり
    try:
        spreadsheet = _api_call_with_retry(lambda: service.spreadsheets().get(
            spreadsheetId=file_id,
            includeGridData=False,
        ).execute())
    except Exception as e:
        print(f"  警告: スプレッドシート取得失敗: {e}", flush=True)
        return []

    sheets = spreadsheet.get("sheets", [])
    results = []

    for sheet in sheets:
        try:
            props = sheet["properties"]
            gid = str(props["sheetId"])
            name = props["title"]
        except (KeyError, TypeError) as e:
            print(f"  警告: シートメタデータ不正（スキップ）: {e}", flush=True)
            continue

        try:
            # シート値取得: デフォルトの max_retries=6 でバックオフ累計 >60 秒までリトライ
            # 60 秒経っても 429 が続くなら諦めて partial として保存
            sheet_range = f"'{name}'"
            values_response = _api_call_with_retry(
                lambda sr=sheet_range: service.spreadsheets().values().get(
                    spreadsheetId=file_id,
                    range=sr,
                ).execute(),
            )
            values = values_response.get("values", [])
            if not values:
                # 空シート: コンテンツなしだが取得成功（失敗ではない）
                results.append({"gid": gid, "name": name, "content": None, "failed": False})
                continue
            text = "\n".join("\t".join(str(cell) for cell in row) for row in values)
            results.append({"gid": gid, "name": name, "content": text if text.strip() else None, "failed": False})
        except Exception as e:
            print(f"  警告: シート '{name}' の取得失敗: {e}", flush=True)
            # 取得失敗: シート名だけ記録してpartialフラグ
            results.append({"gid": gid, "name": name, "content": None, "failed": True})

    return results


def list_files_in_drive(service, drive_id=None, corpora="user"):
    """指定スコープのファイルを全件取得する。"""
    files = []
    page_token = None
    query = "trashed = false"

    kwargs = {
        "q": query,
        "spaces": "drive",
        "fields": "nextPageToken, files(id, name, mimeType, modifiedTime, owners, webViewLink, driveId, parents, permissions(emailAddress, role, type, displayName))",
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
    """ファイルをバイナリでダウンロードする。
    タイムアウトは httplib2 のソケットタイムアウトで制御。"""
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
    - PDF（pypdf でテキスト抽出、タイムアウト付き）
    - その他: ファイル名をメタデータとして返す（未対応 MIME 種別のフォールバック）

    Returns:
        (text, is_partial): text は抽出テキスト。
                            extract_text 単体で Noneを返すのは、呼び出し側が別系統で
                            処理すべき場合のみ（スプレッドシートは extract_spreadsheet_sheets
                            で処理、SKIP_MIME_TYPES は明示的に無視）。
                            is_partial は PDF が途中でタイムアウトした場合 True、
                            未対応 MIME / 失敗フォールバックでメタデータのみ保存する場合も True。
    """
    mime_type = file_info["mimeType"]
    file_id = file_info["id"]
    file_name = file_info["name"]

    # フォルダ: フォルダ名をメタデータとして返す
    if mime_type == "application/vnd.google-apps.folder":
        return f"[フォルダ] {file_name}", False

    if mime_type in SKIP_MIME_TYPES:
        return None, False

    # スプレッドシートは extract_spreadsheet_sheets() で処理する
    if mime_type == "application/vnd.google-apps.spreadsheet":
        return None, False

    # Google Docs 系: エクスポート
    if mime_type in EXPORT_MIME_MAP:
        export_mime = EXPORT_MIME_MAP[mime_type]
        try:
            def _do_export():
                request = _api_call_with_retry(lambda: service.files().export_media(fileId=file_id, mimeType=export_mime))
                content = io.BytesIO()
                downloader = MediaIoBaseDownload(content, request)
                done = False
                while not done:
                    _, done = downloader.next_chunk()
                return content
            content = _do_export()
            return content.getvalue().decode("utf-8", errors="replace"), False
        except (socket.timeout, OSError) as e:
            print(f"  警告: エクスポートタイムアウト [{file_name}]: {e}", flush=True)
            return f"[Doc] {file_name}", True
        except Exception as e:
            print(f"  警告: エクスポート失敗 [{file_name}]: {e}", flush=True)
            return f"[Doc] {file_name}", True

    # テキスト系ファイル: ダウンロード
    if mime_type in TEXT_MIME_TYPES:
        try:
            content = _download_content(service, file_id)
            return content.getvalue().decode("utf-8", errors="replace"), False
        except (socket.timeout, OSError) as e:
            print(f"  警告: ダウンロードタイムアウト [{file_name}]: {e}", flush=True)
            return f"[テキスト] {file_name}", True
        except Exception as e:
            print(f"  警告: ダウンロード失敗 [{file_name}]: {e}", flush=True)
            return f"[テキスト] {file_name}", True

    # PDF: タイムアウト付きテキスト抽出
    #   - さくっと読めた → (text, False)
    #   - タイムアウト・一部抽出 → (partial_text, True)
    #   - まったく読めない / タイムアウト前に0ページ → ([PDF] meta, False)
    if mime_type == "application/pdf":
        try:
            content = _download_content(service, file_id)
            content.seek(0)
            try:
                text, is_partial = _extract_pdf_with_timeout(content, timeout_sec=60)
                if text:
                    if is_partial:
                        print(f"  警告: PDF タイムアウト（部分データ保存）[{file_info['name']}]", flush=True)
                    return text, is_partial
            except ImportError:
                print("  警告: pypdf がインストールされていません。PDF はスキップします。", flush=True)
                return f"[PDF] {file_info['name']}", False
            return f"[スキャンPDF] {file_info['name']}", False
        except (socket.timeout, OSError) as e:
            print(f"  警告: PDF ダウンロードタイムアウト [{file_info['name']}]: {e}", flush=True)
            return f"[PDF] {file_info['name']}", False
        except Exception as e:
            print(f"  警告: PDF 処理失敗 [{file_info['name']}]: {e}", flush=True)
            return f"[PDF] {file_info['name']}", False

    # 画像: OCR 試行 → メタデータ
    if mime_type.startswith("image/"):
        try:
            content = _download_content(service, file_id)
            return _try_ocr_image(content, file_info["name"]), False
        except (socket.timeout, OSError) as e:
            print(f"  警告: 画像ダウンロードタイムアウト [{file_info['name']}]: {e}", flush=True)
            return f"[画像] {file_info['name']}", False
        except Exception as e:
            print(f"  警告: 画像処理失敗 [{file_info['name']}]: {e}", flush=True)
            return f"[画像] {file_info['name']}", False

    # 動画: メタデータのみ
    if mime_type.startswith("video/"):
        return f"[動画] {file_name}", False

    # 音声: メタデータのみ
    if mime_type.startswith("audio/"):
        return f"[音声] {file_name}", False

    # 未対応 MIME タイプ: ファイル名をメタデータとして返す (partial=True でマーク)。
    # これにより Office系 (.docx/.pptx)、アーカイブ (.zip/.epub)、バイナリ (application/
    # octet-stream) 等もファイル名検索で見つかる。integrity check の missing 誤検知も解消。
    # CLAUDE.md の原則「処理失敗時でもファイル名は記録される」を全 MIME に適用。
    return f"[ファイル] {file_name}", True


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


def attach_folder_paths(files, drive_name=""):
    """ファイルリスト内のフォルダエントリを使ってフォルダパスを解決し、
    各 file_info に folder_path と drive_name を追加する。

    Drive API の追加コールは行わず、取得済みリストのみで完結する。
    drive_name を prefix として付ける（例: "GW_LIB / 2024 / 月次"）。
    """
    FOLDER_MIME = "application/vnd.google-apps.folder"

    # {folder_id: {"name": str, "parents": [id, ...]}} のマップを構築
    folder_map = {
        f["id"]: {"name": f["name"], "parents": f.get("parents", [])}
        for f in files
        if f.get("mimeType") == FOLDER_MIME
    }

    # 解決済みパスのキャッシュ {folder_id: "path string"}
    path_cache = {}

    def _resolve(folder_id, visited=None):
        if folder_id in path_cache:
            return path_cache[folder_id]
        if visited is None:
            visited = set()
        if folder_id in visited:
            return ""
        visited.add(folder_id)

        info = folder_map.get(folder_id)
        if not info:
            # ドライブルート（フォルダ一覧に含まれないID）
            result = drive_name
        else:
            parents = info.get("parents", [])
            parent_path = _resolve(parents[0], visited) if parents else drive_name
            name = info["name"]
            result = f"{parent_path} / {name}" if parent_path else name

        path_cache[folder_id] = result
        return result

    for f in files:
        parents = f.get("parents", [])
        if parents:
            f["folder_path"] = _resolve(parents[0])
        else:
            f["folder_path"] = drive_name
        f["drive_name"] = drive_name

    return files


def resolve_folder_path_api(service, file_info, cache=None):
    """Drive API を呼び出してフォルダパスを解決する（sync.py 用）。

    cache: {folder_id: {"name": str, "parents": [...]}} の共有辞書。
    戻り値: "FolderA / SubFolder" 形式の文字列（ドライブ名は含まない）。
    """
    if cache is None:
        cache = {}

    parents = file_info.get("parents", [])
    if not parents:
        return ""

    path_parts = []
    current_id = parents[0]
    drive_id = file_info.get("driveId")
    visited = set()

    while current_id:
        if current_id in visited or current_id == drive_id:
            break
        visited.add(current_id)

        if current_id not in cache:
            try:
                result = _api_call_with_retry(lambda fid=current_id: service.files().get(
                    fileId=fid,
                    fields="id,name,parents,driveId",
                    supportsAllDrives=True,
                ).execute())
                cache[current_id] = result
            except Exception:
                break

        info = cache.get(current_id, {})
        name = info.get("name", "")
        if name:
            path_parts.append(name)

        next_parents = info.get("parents", [])
        current_id = next_parents[0] if next_parents else None

    path_parts.reverse()
    return " / ".join(path_parts)


# Changes API の fields 文字列は drive_id の有無に関わらず統一する
_CHANGES_FIELDS = (
    "nextPageToken, newStartPageToken, "
    "changes(fileId, removed, file("
    "id, name, mimeType, modifiedTime, trashed, owners, "
    "webViewLink, driveId, parents, "
    "permissions(emailAddress, role, type, displayName)))"
)


def get_changes_start_token(service, drive_id=None):
    """Changes API の開始トークンを取得する。

    drive_id を指定すると特定の共有ドライブのトークンを取得する。
    """
    kwargs = {"supportsAllDrives": True}
    if drive_id:
        kwargs["driveId"] = drive_id
    response = _api_call_with_retry(
        lambda: service.changes().getStartPageToken(**kwargs).execute()
    )
    return response["startPageToken"]


def list_changes(service, page_token, drive_id=None):
    """前回トークン以降の変更ファイルを取得する。

    drive_id を指定すると特定の共有ドライブに限定した変更のみ返す。
    """
    changes = []
    while True:
        kwargs = {
            "pageToken": page_token,
            "includeItemsFromAllDrives": True,
            "supportsAllDrives": True,
            "fields": _CHANGES_FIELDS,
        }
        if drive_id:
            kwargs["driveId"] = drive_id
        else:
            kwargs["spaces"] = "drive"

        response = _api_call_with_retry(
            lambda: service.changes().list(**kwargs).execute()
        )

        changes.extend(response.get("changes", []))

        if "newStartPageToken" in response:
            new_token = response["newStartPageToken"]
            return changes, new_token

        page_token = response["nextPageToken"]


