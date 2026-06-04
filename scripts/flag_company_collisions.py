"""One-shot script: surface company-name collisions for human re-linkage.

Phase 49.03 — D-14 / F-07 (see .planning/specs/2026-05-29-ingestion-contract-enforcement.md).

A "company collision" is a ``jobs.company`` string that has been linked to
**more than one** ``company_id`` across different job rows.  The documented
motivating case is ``"eviCore healthcare MSI, LLC"`` being fuzzy-matched into
both Cigna (cid=1397) and GE HealthCare (cid=932) under the old threshold-85
matcher.

This script does NOT re-link jobs or modify ``company_id``.  It only flags the
affected rows by appending ``"company_collision_review"`` to their
``unresolved_reasons`` JSON array so they surface on the ``/admin/review``
page for per-case human judgment.

Usage::

    # Dry-run — see what would be flagged (never writes)
    uv run python scripts/flag_company_collisions.py --audit [--db jobs.db]

    # Apply — write unresolved_reasons flags for affected rows
    uv run python scripts/flag_company_collisions.py --apply [--db jobs.db]

Operational protocol:
  1. Pause enrichment_backfill and agentic_backfill schedulers before running
     ``--apply`` to avoid a race against active upsert_job writers.
  2. Run ``--audit`` to review the collision list.
  3. Run ``--apply`` to flag jobs.
  4. Open ``/admin/review`` in the web UI to process each flagged row.
  5. After all collisions are resolved, the flags are cleared automatically by
     the approve/drop actions in the admin blueprint.

Dependency: requires the ``unresolved_reasons`` column added in migration m078
(Phase 47.04 / PR #79).  The script checks for this column at startup and
exits with a clear error if it is absent.

NG-01: Full company re-linkage is OUT of scope — each collision is a per-case
       judgment call that may require inspecting the company's ATS tenant,
       its acquisition history, or the specific job posting context.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

_REASON_CODE = "company_collision_review"


# ---------------------------------------------------------------------------
# Schema preflight
# ---------------------------------------------------------------------------


def _check_unresolved_reasons_column(conn: sqlite3.Connection) -> None:
    """Raise SystemExit(1) if the unresolved_reasons column is absent.

    The column was added by migration m078 (Phase 47.04). If it is missing,
    run the migrations first::

        uv run python -m job_finder db-migrate  # or restart the app once
    """
    cols = {
        row[1]
        for row in conn.execute("PRAGMA table_info(jobs)").fetchall()
    }
    if "unresolved_reasons" not in cols:
        logger.error(
            "Column 'unresolved_reasons' not found in jobs table. "
            "Run migration m078 (Phase 47.04) first: "
            "'uv run python -m job_finder db-migrate' or start the app once."
        )
        sys.exit(1)


# ---------------------------------------------------------------------------
# Collision detection
# ---------------------------------------------------------------------------


def _find_collisions(conn: sqlite3.Connection) -> list[dict]:
    """Return all jobs.company strings linked to >1 distinct company_id.

    A collision means the same ``company`` string appears in job rows that
    have been linked to different company records — a symptom of either
    fuzzy-match false positives or manual re-linkage inconsistencies.

    Returns:
        List of dicts, each with keys:
          - "company"       (str)  raw company name
          - "company_ids"   (list[int])  distinct company_id values
          - "dedup_keys"    (list[str])  all job dedup_keys affected
          - "job_count"     (int)  total affected rows
    """
    # Step 1: find company strings with >1 distinct company_id
    collision_rows = conn.execute(
        """
        SELECT company, COUNT(DISTINCT company_id) AS cid_count
        FROM jobs
        WHERE company IS NOT NULL AND company_id IS NOT NULL
        GROUP BY company
        HAVING COUNT(DISTINCT company_id) > 1
        ORDER BY cid_count DESC, company
        """
    ).fetchall()

    results = []
    for row in collision_rows:
        company = row["company"] if hasattr(row, "__getitem__") else row[0]
        cid_count = row["cid_count"] if hasattr(row, "__getitem__") else row[1]

        # Fetch the distinct company_ids and affected job dedup_keys
        detail_rows = conn.execute(
            "SELECT dedup_key, company_id FROM jobs "
            "WHERE company = ? AND company_id IS NOT NULL",
            (company,),
        ).fetchall()

        dedup_keys = [r["dedup_key"] for r in detail_rows]
        cids = sorted({r["company_id"] for r in detail_rows})

        results.append(
            {
                "company": company,
                "company_ids": cids,
                "dedup_keys": dedup_keys,
                "job_count": len(dedup_keys),
            }
        )

    return results


# ---------------------------------------------------------------------------
# Audit mode (dry-run)
# ---------------------------------------------------------------------------


def _run_audit(conn: sqlite3.Connection) -> list[dict]:
    """Print collision summary without modifying the database."""
    collisions = _find_collisions(conn)

    if not collisions:
        print("No company collisions detected.")
        return collisions

    print(f"Found {len(collisions)} company collision(s):\n")
    for i, c in enumerate(collisions, 1):
        cids_str = ", ".join(str(cid) for cid in c["company_ids"])
        print(
            f"  {i:3d}. {c['company']!r}\n"
            f"       company_ids: [{cids_str}]  "
            f"({len(c['company_ids'])} distinct)  "
            f"jobs: {c['job_count']}"
        )
    print(
        f"\n{'─' * 60}\n"
        f"Total: {len(collisions)} collision(s) covering "
        f"{sum(c['job_count'] for c in collisions)} job row(s).\n"
        f"Run with --apply to flag these rows for /admin/review."
    )
    return collisions


# ---------------------------------------------------------------------------
# Apply mode (write flags)
# ---------------------------------------------------------------------------


def _append_reason(current_json: str, reason: str) -> str:
    """Return updated JSON array with reason appended if not already present."""
    try:
        reasons: list = json.loads(current_json) if current_json else []
    except (ValueError, TypeError):
        reasons = []
    if reason not in reasons:
        reasons.append(reason)
    return json.dumps(reasons)


def _run_apply(conn: sqlite3.Connection) -> None:
    """Flag all collision-affected job rows by appending _REASON_CODE to
    their unresolved_reasons JSON array.

    Runs each row update in its own commit so a partial failure preserves
    prior progress (idempotent on re-run).

    Does NOT modify company_id (NG-01).
    """
    collisions = _find_collisions(conn)

    if not collisions:
        print("No company collisions detected — nothing to flag.")
        return

    print(f"Flagging {len(collisions)} collision(s) ({sum(c['job_count'] for c in collisions)} rows)…")

    total_flagged = 0
    total_already = 0

    for c in collisions:
        for dedup_key in c["dedup_keys"]:
            row = conn.execute(
                "SELECT unresolved_reasons FROM jobs WHERE dedup_key = ?",
                (dedup_key,),
            ).fetchone()

            if row is None:
                logger.warning("dedup_key not found: %s — skipping", dedup_key)
                continue

            current = row["unresolved_reasons"] or "[]"
            updated = _append_reason(current, _REASON_CODE)

            if updated == current:
                total_already += 1
                continue

            conn.execute(
                "UPDATE jobs SET unresolved_reasons = ? WHERE dedup_key = ?",
                (updated, dedup_key),
            )
            conn.commit()
            total_flagged += 1

    print(
        f"Done.  "
        f"Newly flagged: {total_flagged}, "
        f"already flagged: {total_already}.\n"
        f"Open /admin/review in the web UI to process each row."
    )
    if total_flagged > 0:
        logger.info(
            "flag_company_collisions --apply: flagged %d rows, %d already had reason",
            total_flagged,
            total_already,
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--db",
        default="jobs.db",
        help="Path to the SQLite database (default: jobs.db)",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--audit",
        action="store_true",
        help="Dry-run: print collisions without modifying the database.",
    )
    group.add_argument(
        "--apply",
        action="store_true",
        help="Write unresolved_reasons flags for collision-affected rows.",
    )
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    _check_unresolved_reasons_column(conn)

    try:
        if args.audit:
            _run_audit(conn)
        else:
            _run_apply(conn)
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
