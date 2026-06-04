"""Flag company-name collisions for human re-linkage review.

A "company collision" is a case where the same raw company string in ``jobs``
is linked to MORE THAN ONE distinct ``company_id``.  This means different jobs
with the exact same ``company`` value point to different company records — an
identity inconsistency.

The canonical example (Phase 49.03 plan, §13, D-14):

    ``eviCore healthcare MSI, LLC`` -> jobs linked to BOTH:
        * Cigna (company_id = 1397) — correct (eviCore was a Cigna subsidiary)
        * GE HealthCare (company_id = 932) — incorrect (cross-company false merge)

This script does NOT re-link any jobs (NG-01).  It surfaces the affected rows
on ``/admin/review`` by appending ``"company_collision_review"`` to the JSON
array in ``jobs.unresolved_reasons``.  A human operator must then review each
collision and manually correct the ``company_id`` as appropriate.

Operational protocol
--------------------
1. Run ``--audit`` first to see the collision list (dry-run, no DB changes).
2. If the list looks correct, run ``--apply`` to write the ``unresolved_reasons``
   flags to the database.
3. Open ``/admin/review`` in the running Flask app to see the flagged jobs.
4. For each job:
   a. Verify which company_id is correct.
   b. Use the admin UI or a manual SQL UPDATE to correct the company_id.
   c. Remove the ``"company_collision_review"`` reason code once resolved.
5. Re-run ``--audit`` after corrections to confirm the collision list is empty.

Prerequisite
------------
The ``unresolved_reasons`` column must exist (added by migration m078).  If it
is missing the script exits with a clear error pointing to Phase 47.04.

Usage
-----
Dry-run (print collisions, no DB changes)::

    uv run python scripts/flag_company_collisions.py --audit [--db jobs.db]

Apply (write unresolved_reasons flags)::

    uv run python scripts/flag_company_collisions.py --apply [--db jobs.db]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

_COLLISION_REASON = "company_collision_review"
_DEFAULT_DB = "jobs.db"


# ---------------------------------------------------------------------------
# Preflight checks
# ---------------------------------------------------------------------------


def _check_unresolved_reasons_column(conn: sqlite3.Connection) -> None:
    """Raise SystemExit if the unresolved_reasons column does not exist.

    The column is added by migration m078 (Phase 47.04).  If it is missing the
    user needs to run the migration first.
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
    if "unresolved_reasons" not in cols:
        print(
            "ERROR: jobs.unresolved_reasons column not found.\n"
            "       Run migration m078 (Phase 47.04) first:\n"
            "           uv run job-cannon --migrate-only\n"
            "       Then re-run this script.",
            file=sys.stderr,
        )
        sys.exit(1)


# ---------------------------------------------------------------------------
# Collision detection
# ---------------------------------------------------------------------------


def find_collisions(conn: sqlite3.Connection) -> list[dict]:
    """Return a list of company-name collision records.

    A collision is any ``jobs.company`` value that appears with more than one
    distinct non-null ``company_id``.

    Each returned dict has:
    * ``company``     — the raw company string from ``jobs``
    * ``company_ids`` — sorted list of distinct company_id values
    * ``company_names`` — list of company names for those IDs (from companies table)
    * ``job_count``   — number of affected job rows
    * ``dedup_keys``  — list of dedup_key values for the affected jobs

    Returns:
        List of collision dicts (empty list if none found).
    """
    # Find company strings that link to more than one distinct company_id
    rows = conn.execute(
        """
        SELECT
            company,
            COUNT(DISTINCT company_id) AS id_count,
            COUNT(*)                   AS job_count
        FROM jobs
        WHERE company IS NOT NULL
          AND company_id IS NOT NULL
        GROUP BY company
        HAVING COUNT(DISTINCT company_id) > 1
        ORDER BY id_count DESC, job_count DESC
        """
    ).fetchall()

    collisions: list[dict] = []
    for row in rows:
        company_str = row["company"]
        job_count = row["job_count"]

        # Fetch all distinct company_ids for this company string
        id_rows = conn.execute(
            "SELECT DISTINCT company_id FROM jobs WHERE company = ?",
            (company_str,),
        ).fetchall()
        company_ids = sorted(r["company_id"] for r in id_rows if r["company_id"] is not None)

        # Look up company names for those IDs
        placeholders = ", ".join("?" * len(company_ids))
        name_rows = conn.execute(
            f"SELECT id, COALESCE(name_raw, name) AS display_name FROM companies WHERE id IN ({placeholders})",
            company_ids,
        ).fetchall()
        id_to_name = {r["id"]: r["display_name"] for r in name_rows}
        company_names = [id_to_name.get(cid, f"<unknown id={cid}>") for cid in company_ids]

        # Fetch dedup_keys for affected jobs
        dk_rows = conn.execute(
            "SELECT dedup_key, company_id FROM jobs WHERE company = ? AND company_id IS NOT NULL",
            (company_str,),
        ).fetchall()
        dedup_keys = [r["dedup_key"] for r in dk_rows]

        collisions.append(
            {
                "company": company_str,
                "company_ids": company_ids,
                "company_names": company_names,
                "job_count": job_count,
                "dedup_keys": dedup_keys,
            }
        )

    return collisions


# ---------------------------------------------------------------------------
# Audit (dry-run) mode
# ---------------------------------------------------------------------------


def print_audit(collisions: list[dict]) -> None:
    """Print the collision list to stdout (dry-run, no DB changes)."""
    if not collisions:
        print("No company-name collisions found.  Database is clean.")
        return

    print(f"Found {len(collisions)} company-name collision(s):\n")
    for i, c in enumerate(collisions, start=1):
        id_pairs = ", ".join(
            f"{cid} ({name})"
            for cid, name in zip(c["company_ids"], c["company_names"], strict=True)
        )
        print(f"  {i:2d}. '{c['company']}'")
        print(f"       -> {len(c['company_ids'])} company_ids: {id_pairs}")
        print(f"       -> {c['job_count']} affected job(s)")
        print()

    print(
        "To flag these jobs for review, re-run with --apply.\n"
        "NOTE: --apply does NOT change company_id; it only surfaces jobs on /admin/review."
    )


# ---------------------------------------------------------------------------
# Apply mode
# ---------------------------------------------------------------------------


def apply_flags(conn: sqlite3.Connection, collisions: list[dict]) -> int:
    """Append 'company_collision_review' to unresolved_reasons for affected jobs.

    Only appends the reason code if it is not already present (idempotent).
    Does NOT modify company_id.

    Args:
        conn: Open SQLite connection with m078 migration applied.
        collisions: List of collision dicts from find_collisions().

    Returns:
        Number of job rows updated.
    """
    updated = 0
    for collision in collisions:
        for dedup_key in collision["dedup_keys"]:
            row = conn.execute(
                "SELECT unresolved_reasons FROM jobs WHERE dedup_key = ?",
                (dedup_key,),
            ).fetchone()
            if row is None:
                continue

            try:
                reasons: list[str] = json.loads(row["unresolved_reasons"] or "[]")
            except (json.JSONDecodeError, TypeError):
                reasons = []

            if _COLLISION_REASON in reasons:
                continue  # already flagged — skip

            reasons.append(_COLLISION_REASON)
            conn.execute(
                "UPDATE jobs SET unresolved_reasons = ? WHERE dedup_key = ?",
                (json.dumps(reasons), dedup_key),
            )
            updated += 1

    conn.commit()
    return updated


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
        default=_DEFAULT_DB,
        help=f"Path to SQLite database (default: {_DEFAULT_DB})",
    )
    mode_group = parser.add_mutually_exclusive_group(required=True)
    mode_group.add_argument(
        "--audit",
        action="store_true",
        help="Print collisions without modifying the database (dry-run)",
    )
    mode_group.add_argument(
        "--apply",
        action="store_true",
        help="Append company_collision_review to unresolved_reasons for affected jobs",
    )
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"ERROR: database not found: {db_path}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    # Preflight: verify m078 column exists
    _check_unresolved_reasons_column(conn)

    # Find collisions
    print(f"Scanning {db_path} for company-name collisions ...")
    collisions = find_collisions(conn)

    if args.audit:
        print_audit(collisions)
        return 0

    # --apply
    if not collisions:
        print("No collisions found — nothing to flag.")
        return 0

    print_audit(collisions)
    updated = apply_flags(conn, collisions)
    print(
        f"\n[APPLY] Flagged {updated} job row(s) with '{_COLLISION_REASON}' in unresolved_reasons."
    )
    print("        Open /admin/review to inspect and re-link the affected jobs.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
