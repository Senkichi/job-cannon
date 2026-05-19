"""Post-prune verification — read-only state check."""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path


def main() -> int:
    root = Path(os.environ.get("JOB_CANNON_USER_DATA_DIR", os.getcwd()))
    db = root / "jobs.db"
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    c.execute("SELECT COUNT(*) AS n FROM jobs")
    print(f"total_jobs: {c.fetchone()['n']}")
    c.execute("SELECT COUNT(*) AS n FROM jobs WHERE sub_scores_json IS NULL "
              "OR sub_scores_json = '' OR sub_scores_json = '{}'")
    print(f"unscored: {c.fetchone()['n']}")
    c.execute("SELECT COUNT(*) AS n FROM jobs WHERE jd_full IS NULL OR jd_full = ''")
    print(f"no_jd_full: {c.fetchone()['n']}")
    c.execute("SELECT COUNT(*) AS n FROM jobs WHERE first_seen LIKE '2026-05-18%'")
    print(f"2026-05-18 rows remaining: {c.fetchone()['n']}")

    print("\n--- top companies on 2026-05-18 (post-prune) ---")
    for r in c.execute("""
        SELECT company, COUNT(*) AS n
        FROM jobs
        WHERE first_seen LIKE '2026-05-18%'
        GROUP BY company
        ORDER BY n DESC
        LIMIT 15
    """).fetchall():
        print(f"  {r['n']:>4}  {r['company']}")

    print("\n--- daily first_seen (last 14 days) ---")
    for r in c.execute("""
        SELECT substr(first_seen, 1, 10) AS day, COUNT(*) AS n
        FROM jobs
        WHERE first_seen >= '2026-05-05'
        GROUP BY day
        ORDER BY day
    """).fetchall():
        print(f"  {r['day']}  {r['n']}")

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
