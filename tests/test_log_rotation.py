"""ログローテーションのテスト。

RotatingFileHandler が maxBytes で実際にローテートすることを確認する。
"""

import os
import sys
import tempfile
import logging
from pathlib import Path
from logging.handlers import RotatingFileHandler

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("GRIDWORLDRAG_SKIP_CONFIG", "1")

import sync_rotate


def test_rotating_handler_rolls_over():
    with tempfile.TemporaryDirectory() as tmp:
        log_file = Path(tmp) / "test.log"
        # 小さな maxBytes で即ロール
        handler = RotatingFileHandler(log_file, maxBytes=100, backupCount=2)
        logger = logging.getLogger("test_rotate")
        logger.handlers = [handler]
        logger.setLevel(logging.INFO)

        for i in range(50):
            logger.info("message %03d padding padding padding", i)

        # backup 0 (= .1) が存在するはず
        assert log_file.exists()
        assert (log_file.parent / "test.log.1").exists()
        handler.close()
        logger.handlers = []


def test_sync_rotate_setup_logging_is_idempotent():
    """_setup_logging を2回呼んでもハンドラが重複しない。"""
    # 既存のハンドラをクリア
    sync_rotate.log.handlers = []

    sync_rotate._setup_logging()
    count1 = len(sync_rotate.log.handlers)
    sync_rotate._setup_logging()
    count2 = len(sync_rotate.log.handlers)

    assert count1 == count2, f"handlers doubled: {count1} → {count2}"
    # cleanup
    for h in list(sync_rotate.log.handlers):
        try:
            h.close()
        except Exception:
            pass
    sync_rotate.log.handlers = []


def test_log_dir_is_created():
    """LOG_DIR が setup 時に作成される（既存の場合も問題なし）。"""
    sync_rotate.log.handlers = []
    sync_rotate._setup_logging()
    assert sync_rotate.LOG_DIR.exists()
    # cleanup
    for h in list(sync_rotate.log.handlers):
        try:
            h.close()
        except Exception:
            pass
    sync_rotate.log.handlers = []


if __name__ == "__main__":
    test_rotating_handler_rolls_over()
    test_sync_rotate_setup_logging_is_idempotent()
    test_log_dir_is_created()
    print("All 3 tests passed.")
