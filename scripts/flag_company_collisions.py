"""One-shot script to surface company-name collisions onto /admin/review.

Operational protocol
--------------------
A "company-name collision" is a case where a single ``jobs.company`` string
(case-insensitive) is linked to more than one distinct ``company_id`` in the
database.  This can happen because the fuzzy matcher ran at different times
with different threshold settings, or because two legitimate companies share a
common name fragment.

This script identifies such collisions, then either reports them (``--audit``)
or appends the string ``"company_collision_review"`` to the
``unresolved_reasons`` JSON array of every affected job row (``--apply``).

Appending to ``unresolved_reasons`` causes the affected rows to surface on
``/admin/review`` so a human can verify the correct ``company_id`` linkage.

**Scope**: surfaces for review only — does NOT re-link or modify ``company_id``.
Re-linkage is a per-case judgment call (NG-01).

Usage
-----
Dry-run (print collisions, make no changes)::

    uv run --active python scripts/flag_company_collisions.py --audit

Apply (write unresolved_reasons for affected rows)::

    uv run --active python scripts/flag_company_collisions.py --apply

Options
-------
--audit     Dry-run: print collisions to stdout, exit 0.
--apply     Write unresolved_reasons entries, then print a summary.
--db PATH   Path to the SQLite database (default: reads from config.yaml, or
            falls back to job_finder.db in the current directory).
--limit N   Maximum number of collision groups to surface (default: 15).
"""

import argparse
import json
import logging
import sqlite3
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

# Sentinel value written to unresolved_reasons to flag collision rows.
COLLISION_TAG = "company_collision_review"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_db_path(cli_path: str | None) -> Path:
    """Return the SQLite DB path, preferring the CLI override."""
    if cli_path:
        return Path(cli_path)
    # Try reading from config.yaml via the app's config loader
    try:
        from job_finder.config import load_config

        cfg = load_config()
        db_path = cfg.get("db", {}).get("path")
        if db_path:
            return Path(db_path)
    except Exception as exc:
        logger.debug("Could not load config.yaml: %s — falling back to job_finder.db", exc)
    return Path("job_finder.db")


def _check_unresolved_reasons_column(conn: sqlite3.Connection) -> bool:
    """Return True if the unresolved_reasons column exists in the jobs table.

    Phase 47.04 (m078) adds this column.  If it is absent the --apply mode
    cannot function; the script exits with code 1 and instructions.
    """
    rows = conn.execute("PRAGMA table_info(jobs)").fetchall()
    return any(row["name"] == "unresolved_reasons" for row in rows)


def _find_collision_groups(
    conn: sqlite3.Connection, limit: int
) -> list[dict]:
    """Return up to ``limit`` company-name collision groups.

    A collision group is a set of rows where ``LOWER(jobs.company)`` maps to
    more than one distinct ``company_id``.

    Returns a list of dicts, each with:
      - ``company_str``   : the raw company string (representative)
      - ``company_lower`` : the lowercased key
      - ``company_ids``   : list of distinct company_id values
      - ``job_count``     : total jobs affected
      - ``job_dedup_keys``: list of dedup_keys for affected rows
    """
    # Step 1: find company strings that map to >1 distinct company_id
    collision_keys = conn.execute(
        """
        SELECT LOWER(company) AS company_lower,
               COUNT(DISTINCT company_id) AS num_companies,
               COUNT(*) AS job_count
          FROM jobs
         WHERE company IS NOT NULL
           AND company != ''
           AND company_id IS NOT NULL
         GROUP BY company_lower
        HAVING num_companies > 1
         ORDER BY num_companies DESC, job_count DESC
         LIMIT ?
        """,
        (limit,),
    ).fetchall()

    groups = []
    for row in collision_keys:
        company_lower = row["company_lower"]

        # Fetch the distinct company_ids and a representative raw string
        id_rows = conn.execute(
            """
            SELECT company_id, company, dedup_key
              FROM jobs
             WHERE LOWER(company) = ?
               AND company_id IS NOT NULL
            """,
            (company_lower,),
        ).fetchall()

        company_ids = sorted({r["company_id"] for r in id_rows})
        representative = id_rows[0]["company"] if id_rows else company_lower
        dedup_keys = [r["dedup_key"] for r in id_rows if r["dedup_key"]]

        groups.append(
            {
                "company_str": representative,
                "company_lower": company_lower,
                "company_ids": company_ids,
                "job_count": row["job_count"],
                "job_dedup_keys": dedup_keys,
            }
        )
    return groups


def _print_audit(groups: list[dict]) -> None:
    """Print collision groups to stdout in a human-readable format."""
    if not groups:
        print("No company-name collisions found.")
        return

    print(f"\n{'=' * 72}")
    print(f"  Company-name collision audit — {len(groups)} group(s) found")
    print(f"{'=' * 72}\n")

    for i, g in enumerate(groups, 1):
        print(f"[{i:02d}] Company string : {g['company_str']!r}")
        print(f"      Lower key      : {g['company_lower']!r}")
        print(f"      company_ids    : {g['company_ids']}")
        print(f"      Jobs affected  : {g['job_count']}")
        print()


def _apply_collision_flags(
    conn: sqlite3.Connection, groups: list[dict]
) -> dict[str, int]:
    """Append COLLISION_TAG to unresolved_reasons for all affected job rows.

    Only appends if the tag is not already present (idempotent).

    Returns:
        Dict with "rows_updated" and "rows_skipped" counts.
    """
    rows_updated = 0
    rows_skipped = 0

    for g in groups:
        company_lower = g["company_lower"]
        job_rows = conn.execute(
            """
            SELECT dedup_key, unresolved_reasons
              FROM jobs
             WHERE LOWER(company) = ?
               AND company_id IS NOT NULL
               AND dedup_key IS NOT NULL
            """,
            (company_lower,),
        ).fetchall()

        for row in job_rows:
            dedup_key = row["dedup_key"]
            raw_reasons = row["unresolved_reasons"]

            # Parse existing reasons list (may be NULL, empty, or JSON)
            try:
                reasons: list = json.loads(raw_reasons) if raw_reasons else []
                if not isinstance(reasons, list):
                    reasons = []
            except (json.JSONDecodeError, TypeError):
                reasons = []

            if COLLISION_TAG in reasons:
                rows_skipped += 1
                continue

            reasons.append(COLLISION_TAG)
            conn.execute(
                "UPDATE jobs SET unresolved_reasons = ? WHERE dedup_key = ?",
                (json.dumps(reasons), dedup_key),
            )
            rows_updated += 1

    conn.commit()
    return {"rows_updated": rows_updated, "rows_skipped": rows_skipped}


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--audit",
        action="store_true",
        help="Dry-run: print collisions to stdout, make no changes.",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help="Write unresolved_reasons entries for affected rows.",
    )
    parser.add_argument(
        "--db",
        metavar="PATH",
        default=None,
        help="Path to the SQLite database (default: from config.yaml or job_finder.db).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=15,
        help="Maximum number of collision groups to surface (default: 15).",
    )
    args = parser.parse_args()

    db_path = _resolve_db_path(args.db)
    if not db_path.exists():
        logger.error("Database not found: %s", db_path)
        sys.exit(1)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    # Preflight: verify the unresolved_reasons column exists
    if not _check_unresolved_reasons_column(conn):
        logger.error(
            "Column 'unresolved_reasons' not found in the jobs table.\n"
            "  → Run Phase 47.04 migrations first:\n"
            "      uv run --active python -m job_finder migrate\n"
            "  Then retry this script."
        )
        conn.close()
        sys.exit(1)

    groups = _find_collision_groups(conn, limit=args.limit)

    if args.audit:
        _print_audit(groups)
        logger.info("Audit complete — %d collision group(s). No changes made.", len(groups))
        conn.close()
        sys.exit(0)

    # --apply
    if not groups:
        logger.info("No collisions found — nothing to apply.")
        conn.close()
        sys.exit(0)

    _print_audit(groups)  # show what will be flagged

    result = _apply_collision_flags(conn, groups)
    logger.info(
        "--apply complete: %d row(s) updated, %d already-tagged row(s) skipped.",
        result["rows_updated"],
        result["rows_skipped"],
    )
    conn.close()


if __name__ == "__main__":
    main()
