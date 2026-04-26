"""WinServerRAG — pg_dump backup with rotation.

Python port of the legacy `scripts/backup.bat`. Runs `pg_dump` against the
configured PostgreSQL instance, drops the dump in
`<install-root>/backups/daily/`, and copies it to `weekly/` on Sundays.
Retention: 7 most recent daily, 4 most recent weekly.

Run via Task Scheduler nightly (or manually). Exit code 0 on success, 1 on
pg_dump failure, 2 on missing pg_dump.

Configuration:
    PG_BIN              defaults to "C:\\Program Files\\PostgreSQL\\17\\bin"
                        (override with WINSRV_PG_BIN env var)
    PGHOST/PGPORT/PGUSER/PGPASSWORD/PGDATABASE are the standard libpq env
                        vars; set them to override the bundled defaults.
    WINSRV_BACKUP_DIR   defaults to <CWD>/backups (or override).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path


def _bin_dir() -> Path:
    """Locate pg_dump.exe. Honor WINSRV_PG_BIN, then standard PG 17 path."""
    env = os.environ.get("WINSRV_PG_BIN")
    if env:
        return Path(env)
    return Path(r"C:\Program Files\PostgreSQL\17\bin")


def _backup_dir() -> Path:
    env = os.environ.get("WINSRV_BACKUP_DIR")
    if env:
        return Path(env)
    # Default: <CWD>/backups (matches the bat's `%~dp0\..\backups` behavior
    # when called from scripts/, with the exe living in the install root).
    return Path.cwd() / "backups"


def _set_default_pg_env() -> None:
    """Populate libpq env vars with our defaults if the operator hasn't set them."""
    os.environ.setdefault("PGHOST", "localhost")
    os.environ.setdefault("PGPORT", "5432")
    os.environ.setdefault("PGUSER", "postgres")
    os.environ.setdefault("PGPASSWORD", "winserverrag")
    os.environ.setdefault("PGDATABASE", "winserverrag")


def main() -> int:
    _set_default_pg_env()

    pg_bin = _bin_dir()
    pg_dump = pg_bin / "pg_dump.exe"
    if not pg_dump.exists():
        print(f"[backup] pg_dump.exe not found at {pg_dump}", file=sys.stderr)
        print("[backup] set WINSRV_PG_BIN to your PostgreSQL bin/ directory.",
              file=sys.stderr)
        return 2

    backup_root = _backup_dir()
    daily_dir = backup_root / "daily"
    weekly_dir = backup_root / "weekly"
    daily_dir.mkdir(parents=True, exist_ok=True)
    weekly_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now()
    stamp = now.strftime("%Y-%m-%d_%H%M")
    db = os.environ["PGDATABASE"]
    daily_path = daily_dir / f"{db}_{stamp}.dump"

    print(f"[backup] starting pg_dump to {daily_path}")
    cmd = [
        str(pg_dump),
        "-h", os.environ["PGHOST"],
        "-p", os.environ["PGPORT"],
        "-U", os.environ["PGUSER"],
        "-F", "c",   # custom format
        "-Z", "6",   # compression
        "-f", str(daily_path),
        db,
    ]
    rc = subprocess.run(cmd, env=os.environ.copy()).returncode
    if rc != 0:
        print(f"[backup] pg_dump FAILED (exit {rc})", file=sys.stderr)
        return 1
    print("[backup] OK")

    # Sunday → weekly snapshot. Python's weekday(): Monday=0, Sunday=6.
    if now.weekday() == 6:
        weekly_path = weekly_dir / daily_path.name
        shutil.copy2(daily_path, weekly_path)
        print(f"[backup] weekly snapshot copied to {weekly_path}")

    # Retention.
    _prune(daily_dir, keep=7)
    _prune(weekly_dir, keep=4)
    print("[backup] retention applied")
    return 0


def _prune(directory: Path, *, keep: int) -> None:
    dumps = sorted(directory.glob("*.dump"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    for old in dumps[keep:]:
        try:
            old.unlink()
            print(f"[backup] pruned {old.name}")
        except OSError as e:
            print(f"[backup] could not prune {old}: {e}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
