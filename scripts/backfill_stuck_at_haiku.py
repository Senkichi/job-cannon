#!/usr/bin/env python3
"""One-shot backfill: reset enrichment_tier for rows stuck on a stale tier
so they re-flow through the new (synthesis-free) cascade.

Targets rows with enrichment_tier IN ('haiku', 'free', 'ddg') AND short jd_full.
After this script runs, the next enrichment cycle picks them up at NULL
(start of cascade) and fetches a real JD if available, or honestly marks
them exhausted.

Usage:
    uv run python scripts/backfill_stuck_at_haiku.py [--dry-run]
"""

import argparse
import sqlite3
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="jobs.db")
    parser.add_argument(
        "--threshold",
        type=int,
        default=1500,
        help="JD-length threshold; rows below this with stale tier get reset",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Report what would change without writing"
    )
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        """
        SELECT enrichment_tier, COUNT(*) AS n
        FROM jobs
        WHERE enrichment_tier IN ('haiku', 'free', 'ddg')
          AND length(jd_full) < ?
        GROUP BY enrichment_tier
    """,
        (args.threshold,),
    ).fetchall()

    total = sum(r["n"] for r in rows)
    print(f"Rows to reset (jd_full < {args.threshold} chars):")
    for r in rows:
        print(f"  {r['enrichment_tier']:<10} {r['n']:>5}")
    print(f"  {'total':<10} {total:>5}")

    if args.dry_run:
        print("\n--dry-run: no changes written.")
        return 0

    if total == 0:
        print("\nNo rows to reset; exiting.")
        return 0

    confirm = input(f"\nReset enrichment_tier=NULL for {total} rows? [yes/N] ")
    if confirm.strip().lower() != "yes":
        print("Aborted.")
        return 1

    cur = conn.execute(
        """
        UPDATE jobs
        SET enrichment_tier = NULL
        WHERE enrichment_tier IN ('haiku', 'free', 'ddg')
          AND length(jd_full) < ?
    """,
        (args.threshold,),
    )
    conn.commit()
    print(f"Reset {cur.rowcount} rows.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
