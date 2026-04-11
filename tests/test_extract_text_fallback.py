"""extract_text が未対応 MIME 種別に対してフォールバックを返すことを保証する。

db 4 ビルド時のバグ: 未対応 MIME (.docx, .pptx, .zip 等) で extract_text が None を
返し、_process_file が silently skip。整合性チェックで「N 件不足」警告が出ていた。

CLAUDE.md の原則「処理失敗時でもファイル名は記録される」を全ての非 SKIP_MIME_TYPES に
適用することを検証する。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("GRIDWORLDRAG_SKIP_CONFIG", "1")

from src import drive_client


class _MockService:
    """extract_text は未対応 MIME パスでサービスを使わないのでスタブで十分。"""


def _extract(mime, name="test_file"):
    return drive_client.extract_text(
        _MockService(),
        {"id": "abc123", "name": name, "mimeType": mime},
    )


# ---------------------------------------------------------------------------
# 未対応 MIME はフォールバックを返す（以前は None で silently skip されていた）
# ---------------------------------------------------------------------------

def test_office_docx_falls_back_to_filename():
    text, partial = _extract(
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "仕様書.docx",
    )
    assert text == "[ファイル] 仕様書.docx"
    assert partial is True


def test_office_pptx_falls_back_to_filename():
    text, partial = _extract(
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "提案.pptx",
    )
    assert text == "[ファイル] 提案.pptx"
    assert partial is True


def test_legacy_doc_falls_back():
    text, partial = _extract("application/msword", "legacy.doc")
    assert text == "[ファイル] legacy.doc"
    assert partial is True


def test_octet_stream_falls_back():
    text, partial = _extract("application/octet-stream", "binary.bin")
    assert text == "[ファイル] binary.bin"
    assert partial is True


def test_zip_falls_back():
    text, partial = _extract("application/zip", "archive.zip")
    assert text == "[ファイル] archive.zip"
    assert partial is True


def test_postscript_falls_back():
    text, partial = _extract("application/postscript", "design.eps")
    assert text == "[ファイル] design.eps"
    assert partial is True


# ---------------------------------------------------------------------------
# 既存の metadata-only ハンドラは変更なし (回帰防止)
# ---------------------------------------------------------------------------

def test_folder_still_returns_folder_label():
    text, partial = _extract(
        "application/vnd.google-apps.folder", "MyFolder"
    )
    assert text == "[フォルダ] MyFolder"
    assert partial is False  # フォルダはインテンショナルな metadata なので partial でない


def test_video_still_returns_video_label():
    text, partial = _extract("video/mp4", "movie.mp4")
    assert text == "[動画] movie.mp4"
    assert partial is False


def test_audio_still_returns_audio_label():
    text, partial = _extract("audio/mpeg", "song.mp3")
    assert text == "[音声] song.mp3"
    assert partial is False


# ---------------------------------------------------------------------------
# SKIP_MIME_TYPES と spreadsheet は引き続き None を返す (外部処理用)
# ---------------------------------------------------------------------------

def test_shortcut_returns_none():
    result = _extract("application/vnd.google-apps.shortcut", "link")
    assert result == (None, False)


def test_form_returns_none():
    result = _extract("application/vnd.google-apps.form", "form")
    assert result == (None, False)


def test_spreadsheet_returns_none():
    # スプレッドシートは extract_spreadsheet_sheets() で処理するため None
    result = _extract("application/vnd.google-apps.spreadsheet", "sheet")
    assert result == (None, False)


if __name__ == "__main__":
    test_office_docx_falls_back_to_filename()
    test_office_pptx_falls_back_to_filename()
    test_legacy_doc_falls_back()
    test_octet_stream_falls_back()
    test_zip_falls_back()
    test_postscript_falls_back()
    test_folder_still_returns_folder_label()
    test_video_still_returns_video_label()
    test_audio_still_returns_audio_label()
    test_shortcut_returns_none()
    test_form_returns_none()
    test_spreadsheet_returns_none()
    print("All 12 tests passed.")
