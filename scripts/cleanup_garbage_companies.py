"""Clean up garbage company records (URLs, HTML fragments, email artifacts).

Scans companies table for records with invalid names (URLs, HTML tags,
LinkedIn email alert artifacts) and unlinks their jobs + deletes them.
Also runs orphan cleanup after.

Usage:
    uv run --active python scripts/cleanup_garbage_companies.py [--dry-run]
"""

import os
import re
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(line_buffering=True)

from job_finder.web.db_helpers import standalone_connection
from job_finder.web.backfill_companies import cleanup_orphan_companies
from job_finder.config import load_config

GARBAGE_PATTERNS = [
    re.compile(r"https?://", re.IGNORECASE),
    re.compile(r"<\s*\w+[^>]*>", re.IGNORECASE),
    re.compile(r"^Edit alert\s", re.IGNORECASE),
]


def find_garbage_companies(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT id, name_raw FROM companies").fetchall()
    garbage = []
    for row in rows:
        name = row["name_raw"]
        for pattern in GARBAGE_PATTERNS:
            if pattern.search(name):
                garbage.append({"id": row["id"], "name_raw": name, "reason": pattern.pattern})
                break
    return garbage


def cleanup_garbage(conn: sqlite3.Connection, garbage: list[dict], dry_run: bool) -> dict:
    jobs_unlinked = 0
    for g in garbage:
        cid = g["id"]
        name = g["name_raw"][:60]

        linked = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE company_id = ?", (cid,)
        ).fetchone()[0]

        print(f"  {'[DRY] ' if dry_run else ''}DELETE company id={cid} ({linked} jobs) — {name}")

        if not dry_run:
            result = conn.execute(
                "UPDATE jobs SET company_id = NULL WHERE company_id = ?", (cid,)
            )
            jobs_unlinked += result.rowcount
            conn.execute("DELETE FROM companies WHERE id = ?", (cid,))

    if not dry_run:
        conn.commit()

    return {"deleted": len(garbage), "jobs_unlinked": jobs_unlinked}


def main():
    dry_run = "--dry-run" in sys.argv
    config = load_config()
    db_path = config["db"]["path"]

    with standalone_connection(db_path) as conn:
        garbage = find_garbage_companies(conn)
        print(f"Found {len(garbage)} garbage company records")

        if not garbage:
            print("Nothing to clean up.")
            return

        result = cleanup_garbage(conn, garbage, dry_run)
        print(f"\n{'[DRY RUN] ' if dry_run else ''}Deleted: {result['deleted']}, Jobs unlinked: {result['jobs_unlinked']}")

        if not dry_run:
            orphan_result = cleanup_orphan_companies(conn)
            print(f"Orphan cleanup: {orphan_result}")


if __name__ == "__main__":
    main()
