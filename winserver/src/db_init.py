"""One-shot DB initializer. Creates the target database and applies schema.sql."""
from __future__ import annotations

import sys

from src import db


def main() -> int:
    print(f"[db_init] target: host={db.PG_HOST} port={db.PG_PORT} "
          f"user={db.PG_USER} db={db.PG_DATABASE}")
    try:
        db.ensure_database()
    except Exception as e:
        print(f"[db_init] ensure_database failed: {e}", file=sys.stderr)
        return 1

    conn = db.connect()
    try:
        db.apply_global_schema(conn)
        created = db.seed_default_mcp_users(conn)
        if created:
            print(f"[db_init] seeded MCP users: {', '.join(created)} (pw=admin)")
    finally:
        conn.close()
    print("[db_init] done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
