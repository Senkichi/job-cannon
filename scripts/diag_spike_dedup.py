"""Targeted diagnostic for the 2026-05-18 spike dedup hypothesis.

Read-only. Checks whether the suspicious mid-tier company counts are
location-multiplication, dedup-key inadequacy, or real volume.

Schema notes: jobs has no `id` column; `dedup_key` is primary key.
URLs are in `source_urls` (JSON). Locations: `location` (canonical),
`locations_raw` (raw multi).
"""
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

    print("--- distinct titles per company on 2026-05-18 (top 30 by row count) ---")
    c.execute("""
        SELECT
            company,
            COUNT(*) AS n_rows,
            COUNT(DISTINCT title) AS n_titles,
            COUNT(DISTINCT dedup_key) AS n_dedup_keys
        FROM jobs
        WHERE first_seen LIKE '2026-05-18%'
        GROUP BY company
        ORDER BY n_rows DESC
        LIMIT 30
    """)
    rows = c.fetchall()
    cols = ["company", "n_rows", "n_titles", "n_dedup_keys"]
    widths = {col: max(len(col), max(len(str(r[col])) for r in rows)) for col in cols}
    print("  ".join(col.ljust(widths[col]) for col in cols))
    for r in rows:
        print("  ".join(str(r[col]).ljust(widths[col]) for col in cols))

    print("\n--- sample EAG Laboratories rows on 2026-05-18 ---")
    c.execute("""
        SELECT title, location, locations_raw, dedup_key, sources, source_urls
        FROM jobs
        WHERE first_seen LIKE '2026-05-18%' AND company = 'EAG Laboratories'
        LIMIT 10
    """)
    for r in c.fetchall():
        print(f"  title={r['title']!r}")
        print(f"     location={r['location']!r}  locations_raw={r['locations_raw']!r}")
        print(f"     dedup_key={r['dedup_key']}")
        print(f"     source_urls={(r['source_urls'] or '')[:200]}")

    print("\n--- sample SpaceX rows on 2026-05-18 ---")
    c.execute("""
        SELECT title, location, locations_raw, dedup_key, source_urls
        FROM jobs
        WHERE first_seen LIKE '2026-05-18%' AND company = 'SpaceX'
        LIMIT 10
    """)
    for r in c.fetchall():
        print(f"  title={r['title']!r}")
        print(f"     location={r['location']!r}  locations_raw={r['locations_raw']!r}")
        print(f"     dedup_key={r['dedup_key']}")

    print("\n--- same-title repetition within a company on 2026-05-18 (top 20) ---")
    c.execute("""
        SELECT company, title, COUNT(*) AS n, COUNT(DISTINCT location) AS n_locs
        FROM jobs
        WHERE first_seen LIKE '2026-05-18%'
        GROUP BY company, title
        HAVING n > 1
        ORDER BY n DESC
        LIMIT 20
    """)
    for r in c.fetchall():
        print(f"  {r['n']:>4}x ({r['n_locs']} locs)  {r['company']} | {r['title']}")

    print("\n--- baseline 2026-05-13: rows / distinct titles per company (top 15) ---")
    c.execute("""
        SELECT
            company,
            COUNT(*) AS n_rows,
            COUNT(DISTINCT title) AS n_titles
        FROM jobs
        WHERE first_seen LIKE '2026-05-13%'
        GROUP BY company
        ORDER BY n_rows DESC
        LIMIT 15
    """)
    for r in c.fetchall():
        print(f"  {r['n_rows']:>4} rows / {r['n_titles']:>4} titles   {r['company']}")

    print("\n--- dedup_key uniqueness on 2026-05-18 ---")
    c.execute("""
        SELECT COUNT(*) AS total_rows, COUNT(DISTINCT dedup_key) AS distinct_keys
        FROM jobs
        WHERE first_seen LIKE '2026-05-18%'
    """)
    r = c.fetchone()
    print(f"  total_rows={r['total_rows']}  distinct_dedup_keys={r['distinct_keys']}  "
          f"diff={r['total_rows'] - r['distinct_keys']}")

    print("\n--- companies whose FIRST EVER row in DB is 2026-05-18 (top 20) ---")
    c.execute("""
        SELECT company, COUNT(*) AS n
        FROM jobs
        WHERE first_seen LIKE '2026-05-18%'
          AND company IN (
              SELECT company
              FROM jobs
              GROUP BY company
              HAVING MIN(first_seen) LIKE '2026-05-18%'
          )
        GROUP BY company
        ORDER BY n DESC
        LIMIT 20
    """)
    rows = c.fetchall()
    print(f"  {len(rows)} of 20 newly-discovered companies")
    for r in rows:
        print(f"  {r['n']:>4}  {r['company']}")

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
