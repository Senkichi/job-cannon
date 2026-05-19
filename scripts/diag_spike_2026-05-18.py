"""One-shot diagnostic for the 2026-05-18 ingestion spike.

Read-only. Confirms the handoff's job-count claims, then runs the
source/title/company/classification breakdowns from rev 10 step 1.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path


def main() -> int:
    root = Path(os.environ.get("JOB_CANNON_USER_DATA_DIR", os.getcwd()))
    db = root / "jobs.db"
    if not db.exists():
        print(f"no db at {db}", file=sys.stderr)
        return 1
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    def q(label: str, sql: str, params: tuple = ()) -> None:
        c.execute(sql, params)
        rows = c.fetchall()
        print(f"\n--- {label} ---")
        if not rows:
            print("(no rows)")
            return
        cols = rows[0].keys()
        widths = {col: max(len(col), max(len(str(r[col])) for r in rows)) for col in cols}
        print("  ".join(col.ljust(widths[col]) for col in cols))
        for r in rows:
            print("  ".join(str(r[col]).ljust(widths[col]) for col in cols))

    # ---- Top-line counts (handoff verification) ----
    q("totals", """
        SELECT
            (SELECT COUNT(*) FROM jobs) AS total_jobs,
            (SELECT COUNT(*) FROM jobs WHERE sub_scores_json IS NULL OR sub_scores_json = '' OR sub_scores_json = '{}') AS unscored,
            (SELECT COUNT(*) FROM jobs WHERE jd_full IS NULL OR jd_full = '') AS no_jd_full
    """)

    # ---- Daily ingestion rate around the spike ----
    q("daily_first_seen (last 14 days)", """
        SELECT substr(first_seen, 1, 10) AS day, COUNT(*) AS n
        FROM jobs
        WHERE first_seen >= '2026-05-05'
        GROUP BY day
        ORDER BY day
    """)

    # ---- Step 1.1: source breakdown for the spike ----
    q("sources for 2026-05-18 spike", """
        SELECT sources, COUNT(*) AS n
        FROM jobs
        WHERE first_seen LIKE '2026-05-18%'
        GROUP BY sources
        ORDER BY n DESC
        LIMIT 20
    """)

    # ---- Step 1.3: top companies on the spike day ----
    q("top companies on 2026-05-18", """
        SELECT company, COUNT(*) AS n
        FROM jobs
        WHERE first_seen LIKE '2026-05-18%'
        GROUP BY company
        ORDER BY n DESC
        LIMIT 20
    """)

    # ---- Step 1.2: top titles on the spike day ----
    q("top titles on 2026-05-18", """
        SELECT title, COUNT(*) AS n
        FROM jobs
        WHERE first_seen LIKE '2026-05-18%'
        GROUP BY title
        ORDER BY n DESC
        LIMIT 20
    """)

    # ---- Step 1.4: classification distribution among scored spike rows ----
    q("classification on 2026-05-18 spike (scored subset)", """
        SELECT classification, COUNT(*) AS n
        FROM jobs
        WHERE first_seen LIKE '2026-05-18%'
          AND classification IS NOT NULL
        GROUP BY classification
        ORDER BY n DESC
    """)

    # ---- Pipeline state of spike rows (don't prune any in-flight) ----
    q("pipeline_status × user_interest on 2026-05-18", """
        SELECT pipeline_status, user_interest, COUNT(*) AS n
        FROM jobs
        WHERE first_seen LIKE '2026-05-18%'
        GROUP BY pipeline_status, user_interest
        ORDER BY n DESC
    """)

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
