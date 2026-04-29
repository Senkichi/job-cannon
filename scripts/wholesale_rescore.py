#!/usr/bin/env python3
"""Wholesale re-score: nullify v3.0 scoring columns across all jobs.

After this runs, the existing batch scorer (web UI "Score all" button or
POST /dashboard/batch-score/start) will pick up every row whose
classification IS NULL and re-score it under the current production
prompt + enrichment pipeline (Phase 4 v4_finalist + Phase 2 fixes).

The five columns nullified mirror exactly what persist_job_assessment
writes (job_finder/db.py): classification, sub_scores_json, fit_analysis,
scoring_provider, scoring_model. The legacy v2.0 columns (score,
score_breakdown, opus_score) are intentionally left intact — they're
historical artifacts from prior pipelines and are not re-derived by v3.0.

gold_* columns (user-authored labels) are NOT touched.

Usage:
    uv run python scripts/wholesale_rescore.py [--db jobs.db] [--dry-run]

After completion, kick the batch scorer:
    1. Open localhost:5000, click "Score all" on the dashboard, OR
    2. POST /dashboard/batch-score/start (returns an HTMX progress fragment)

Estimated wall time: ~6s/job * ~5300 unscored rows ≈ 9 hours overnight on
Ollama qwen2.5:14b. The 30-minute UI timeout in the batch-status route is a
display-only safety net — the background worker thread does not check it
and runs to completion regardless.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from contextlib import closing


def count_classified(db_path: str) -> int:
    """Return the number of rows currently carrying a non-null classification."""
    with closing(sqlite3.connect(db_path)) as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE classification IS NOT NULL"
        ).fetchone()[0]


def nullify_classifications(
    db_path: str,
    *,
    dry_run: bool = False,
    confirm: str = "no",
) -> int:
    """Nullify the five v3.0 scoring columns on every classified row.

    Args:
        db_path: Path to the SQLite database.
        dry_run: If True, report the count and return 0 without writing.
        confirm: Must be the exact string ``"yes"`` for a real run. Any
            other value raises ``SystemExit(2)`` (per the script-rules
            convention: exit 2 = bad/missing usage). ``dry_run=True``
            bypasses the confirmation requirement.

    Returns:
        Rows affected by the UPDATE (0 for a dry-run, or the SQLite
        rowcount on a real run).
    """
    total = count_classified(db_path)
    print(f"Would nullify classification on {total} rows.")

    if dry_run:
        print("--dry-run: no changes written.")
        return 0

    if confirm.lower() != "yes":
        print(f"Aborting: confirmation '{confirm}' is not 'yes'.", file=sys.stderr)
        sys.exit(2)

    with closing(sqlite3.connect(db_path)) as conn:
        cur = conn.execute(
            """
            UPDATE jobs
               SET classification   = NULL,
                   sub_scores_json  = NULL,
                   fit_analysis     = NULL,
                   scoring_provider = NULL,
                   scoring_model    = NULL
             WHERE classification IS NOT NULL
                OR sub_scores_json IS NOT NULL
                OR fit_analysis    IS NOT NULL
                OR scoring_provider IS NOT NULL
                OR scoring_model   IS NOT NULL
            """
        )
        conn.commit()
        rowcount = cur.rowcount
    print(f"Nullified {rowcount} rows.")
    return rowcount


def main() -> int:
    parser = argparse.ArgumentParser(
        description="One-shot wholesale re-score nullifier.",
    )
    parser.add_argument("--db", default="jobs.db", help="Path to jobs.db (default: jobs.db)")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report counts without writing.",
    )
    args = parser.parse_args()

    if args.dry_run:
        nullify_classifications(args.db, dry_run=True)
        return 0

    print()
    print("This will nullify the five v3.0 scoring columns on every classified row")
    print(f"in {args.db}:")
    print("  classification, sub_scores_json, fit_analysis, scoring_provider, scoring_model")
    print()
    print("After this completes, run the batch scorer (web UI 'Score all' button or")
    print("POST /dashboard/batch-score/start) to re-score everything.")
    print()
    print("Estimated re-score time: ~6s/job × N rows ≈ overnight on Ollama qwen2.5:14b.")
    confirm = input("\nProceed? Type 'yes' to confirm: ").strip()
    nullify_classifications(args.db, dry_run=False, confirm=confirm)
    return 0


if __name__ == "__main__":
    sys.exit(main())
