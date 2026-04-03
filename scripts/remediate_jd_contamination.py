"""Retroactive JD contamination cleanup.

Scans all jobs with jd_full set, re-sanitizes each value using
sanitize_jd_text(), and optionally nulls cross-contaminated salary data.

Phases:
  A. JD sanitization — strip contamination markers, truncate at "Similar jobs", etc.
     - If sanitized value differs from stored: UPDATE jd_full + reset sonnet_score/fit_analysis.
     - If sanitized value is None (all chrome): NULL jd_full + reset enrichment_tier + reset scores.
  B. Salary decontamination — null salary on jobs that had contaminated JDs and
     whose salary came from AI extraction on scraped page text (not a structured source).
  C. Print summary report.

Usage:
    uv run python scripts/remediate_jd_contamination.py          # dry-run (default)
    uv run python scripts/remediate_jd_contamination.py --apply  # write changes
    uv run python scripts/remediate_jd_contamination.py --db-path /path/to/jobs.db
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from pathlib import Path

# Ensure job_finder package is importable when run from the project root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from job_finder.jd_sanitizer import sanitize_jd_text

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

_DEFAULT_DB = Path(__file__).resolve().parent.parent / "instance" / "jobs.db"
_BATCH_SIZE = 100


def _has_ats_hit(conn: sqlite3.Connection, company_id: int | None) -> bool:
    """Return True when the company has a confirmed ATS hit (structured salary source)."""
    if not company_id:
        return False
    row = conn.execute(
        "SELECT ats_probe_status FROM companies WHERE id = ?", (company_id,)
    ).fetchone()
    return bool(row and row["ats_probe_status"] == "hit")


def run(db_path: str, apply: bool) -> dict:
    """Execute the cleanup. Returns summary counts."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    counts = {
        "scanned": 0,
        "already_clean": 0,
        "cleaned": 0,
        "nulled": 0,
        "salary_nulled": 0,
        "salary_kept": 0,
    }

    # Track which dedup_keys were cleaned or nulled (for salary phase).
    cleaned_keys: set[str] = set()
    nulled_keys: set[str] = set()

    # Capture original enrichment_tier per dedup_key BEFORE Phase A writes.
    # Phase B needs the pre-cleanup tier to decide if salary came from a
    # structured source. Re-reading after Phase A would see NULL for nulled rows.
    original_tiers: dict[str, str | None] = {}

    rows = conn.execute(
        "SELECT dedup_key, title, company, jd_full, enrichment_tier, "
        "       salary_min, salary_max, company_id "
        "FROM jobs WHERE jd_full IS NOT NULL"
    ).fetchall()

    # Capture original tiers before any writes.
    for row in rows:
        original_tiers[row["dedup_key"]] = row["enrichment_tier"]

    batch: list[tuple] = []

    for row in rows:
        counts["scanned"] += 1
        dedup_key = row["dedup_key"]
        original = row["jd_full"]
        sanitized = sanitize_jd_text(original)

        if sanitized is None:
            # Entire JD was contamination — null it out and reset tier for re-enrichment
            action = "NULLED"
            nulled_keys.add(dedup_key)
            counts["nulled"] += 1
            if apply:
                batch.append(("null", dedup_key))
        elif sanitized != original:
            action = f"CLEANED ({len(original)} -> {len(sanitized)} chars)"
            cleaned_keys.add(dedup_key)
            counts["cleaned"] += 1
            if apply:
                batch.append(("clean", dedup_key, sanitized))
        else:
            action = "clean"
            counts["already_clean"] += 1

        if action != "clean":
            logger.info("%-10s  %s | %s", action.split()[0], dedup_key[:60], row["title"][:40])

        # Commit in batches
        if apply and len(batch) >= _BATCH_SIZE:
            _flush_batch(conn, batch)
            batch.clear()

    if apply and batch:
        _flush_batch(conn, batch)

    # ---------------------------------------------------------------------------
    # Phase B: Salary decontamination
    # ---------------------------------------------------------------------------
    affected_keys = cleaned_keys | nulled_keys
    if affected_keys:
        salary_rows = conn.execute(
            f"SELECT dedup_key, salary_min, salary_max, company_id "
            f"FROM jobs WHERE dedup_key IN ({','.join('?' * len(affected_keys))})"
            f"  AND (salary_min IS NOT NULL OR salary_max IS NOT NULL)",
            list(affected_keys),
        ).fetchall()

        for row in salary_rows:
            dedup_key = row["dedup_key"]
            # Use the ORIGINAL tier captured before Phase A — Phase A may have
            # nulled enrichment_tier for rows whose entire JD was chrome, making
            # a re-read return NULL even for serpapi-sourced jobs.
            tier = original_tiers.get(dedup_key)
            company_id = row["company_id"]

            # Salary from structured sources is trustworthy — keep it.
            if tier == "serpapi" or _has_ats_hit(conn, company_id):
                counts["salary_kept"] += 1
                logger.info("SALARY-KEPT  %s (structured source: %s)", dedup_key[:60], tier or "ats")
                continue

            # Salary from AI extraction on contaminated text — null it.
            counts["salary_nulled"] += 1
            logger.info("SALARY-NULLED  %s", dedup_key[:60])
            if apply:
                conn.execute(
                    "UPDATE jobs SET salary_min = NULL, salary_max = NULL WHERE dedup_key = ?",
                    (dedup_key,),
                )

        if apply:
            conn.commit()

    conn.close()
    return counts


def _flush_batch(conn: sqlite3.Connection, batch: list) -> None:
    """Write a batch of UPDATE operations to the DB.

    Both null and clean actions reset sonnet_score and fit_analysis so that
    Sonnet scores computed on contaminated JD text are invalidated and the
    job will be re-evaluated with the cleaned (or re-enriched) JD.
    """
    for item in batch:
        action = item[0]
        dedup_key = item[1]
        if action == "null":
            conn.execute(
                "UPDATE jobs SET jd_full = NULL, enrichment_tier = NULL, "
                "sonnet_score = NULL, fit_analysis = NULL WHERE dedup_key = ?",
                (dedup_key,),
            )
        elif action == "clean":
            conn.execute(
                "UPDATE jobs SET jd_full = ?, sonnet_score = NULL, fit_analysis = NULL "
                "WHERE dedup_key = ?",
                (item[2], dedup_key),
            )
    conn.commit()


def _print_summary(counts: dict, apply: bool) -> None:
    mode = "APPLIED" if apply else "DRY RUN (no changes written)"
    print(f"\nJD Remediation Summary [{mode}]:")
    print(f"  Scanned:               {counts['scanned']:>6}")
    print(f"  Already clean:         {counts['already_clean']:>6}")
    print(f"  Cleaned (truncated):   {counts['cleaned']:>6}")
    print(f"  Nulled (all chrome):   {counts['nulled']:>6}")
    print(f"  Salary nulled:         {counts['salary_nulled']:>6}")
    print(f"  Salary kept:           {counts['salary_kept']:>6}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Retroactive JD contamination cleanup.")
    parser.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Write changes to DB (default: dry-run)",
    )
    parser.add_argument(
        "--db-path",
        default=str(_DEFAULT_DB),
        help=f"Path to SQLite DB (default: {_DEFAULT_DB})",
    )
    args = parser.parse_args()

    db_path = args.db_path
    if not Path(db_path).exists():
        # Fallback to jobs.db in project root (common dev layout)
        fallback = Path(__file__).resolve().parent.parent / "jobs.db"
        if fallback.exists():
            db_path = str(fallback)
        else:
            logger.error("DB not found: %s (also tried %s)", args.db_path, fallback)
            sys.exit(1)

    logger.info("DB: %s | mode: %s", db_path, "APPLY" if args.apply else "DRY-RUN")

    counts = run(db_path, apply=args.apply)
    _print_summary(counts, apply=args.apply)


if __name__ == "__main__":
    main()
