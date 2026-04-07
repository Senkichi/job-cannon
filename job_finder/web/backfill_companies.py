"""Company backfill orchestration for job-finder.

Batch orchestration layer — runs company linkage, ATS probing, DDG enrichment,
and orphan cleanup. Individual resolution functions live in company_resolver.py.

Purpose:
    Orchestrates all company-related backfill operations:
    1. Fuzzy-match and link jobs to company records
    2. Run ATS probing on newly created companies
    3. Run DuckDuckGo enrichment on newly created companies
    4. Clean up orphan company records nightly

Usage:
    python -m job_finder.web.backfill_companies

Exports:
    main: CLI entry point.
    run_company_linkage: Scheduler-compatible linkage wrapper.
    run_ats_probing: Scheduler-compatible ATS probe wrapper.
    run_scheduled_enrichment: Scheduler-compatible enrichment wrapper.
    run_orphan_cleanup: Scheduler-compatible orphan cleanup wrapper.
    cleanup_orphan_companies: Orphan detection and deletion.

    # Re-exported from company_resolver for backwards compatibility:
    cleanup_denylist_companies, find_duplicate_companies,
    find_fuzzy_false_positives, fuzzy_match_company, link_jobs_to_companies,
    run_ddg_enrichment, verify_homepage_urls, verify_all_linkable_jobs_linked
"""

import logging
import sqlite3

from job_finder.config import load_config
from job_finder.web.db_helpers import standalone_connection
from job_finder.web.ats_scanner import probe_ats_slugs
from job_finder.web.company_resolver import (
    cleanup_denylist_companies,
    find_duplicate_companies,
    find_fuzzy_false_positives,
    fuzzy_match_company,
    link_jobs_to_companies,
    run_ddg_enrichment,
    verify_homepage_urls,
    verify_all_linkable_jobs_linked,
)

logger = logging.getLogger(__name__)

# Re-export everything from company_resolver so existing importers don't break
__all__ = [
    "cleanup_denylist_companies",
    "cleanup_orphan_companies",
    "find_duplicate_companies",
    "find_fuzzy_false_positives",
    "fuzzy_match_company",
    "link_jobs_to_companies",
    "main",
    "run_ats_probing",
    "run_company_linkage",
    "run_ddg_enrichment",
    "run_orphan_cleanup",
    "run_scheduled_enrichment",
    "verify_all_linkable_jobs_linked",
    "verify_homepage_urls",
]


# ---------------------------------------------------------------------------
# Orphan cleanup (lives here — no individual-record logic)
# ---------------------------------------------------------------------------


def cleanup_orphan_companies(conn: sqlite3.Connection) -> dict:
    """Delete orphan companies and recalibrate jobs_found_total.

    Orphan = no linked jobs AND no scan log entries. Safe to delete because
    they have zero production value and no historical data.

    Also recalibrates jobs_found_total for all remaining companies from
    actual linked job counts (fixes any drift from earlier scans).

    Args:
        conn: Open SQLite connection with row_factory set.

    Returns:
        Dict with orphans_deleted and recalibrated_total (number of company
        rows touched by the recalibration UPDATE, including rows whose value
        did not change).
    """
    orphan_rows = conn.execute(
        """SELECT c.id FROM companies c
           WHERE c.id NOT IN (
               SELECT DISTINCT company_id FROM jobs WHERE company_id IS NOT NULL
           )
           AND c.id NOT IN (
               SELECT DISTINCT company_id FROM company_scan_log
           )"""
    ).fetchall()
    orphan_ids = [row[0] for row in orphan_rows]
    orphan_count = len(orphan_ids)

    if orphan_ids:
        placeholders = ",".join("?" * len(orphan_ids))
        conn.execute(
            f"DELETE FROM companies WHERE id IN ({placeholders})",
            orphan_ids,
        )

    recalibrated_total = conn.execute(
        """UPDATE companies SET jobs_found_total = (
            SELECT COUNT(*) FROM jobs WHERE company_id = companies.id
        )"""
    ).rowcount

    conn.commit()
    return {"orphans_deleted": orphan_count, "recalibrated_total": recalibrated_total}


# ---------------------------------------------------------------------------
# Scheduler-compatible orchestration wrappers
# ---------------------------------------------------------------------------


def run_company_linkage(db_path: str, config: dict) -> dict:
    """Scheduler-compatible wrapper for link_jobs_to_companies().

    Opens its own connection (thread-safe for APScheduler). Does NOT run ATS
    probing or DDG enrichment — those are separate scheduled jobs.

    Args:
        db_path: Absolute path to the SQLite database file.
        config: Application config dict (unused, kept for scheduler signature compatibility).

    Returns:
        Dict with linked, new_companies, matched counts.
    """
    with standalone_connection(db_path) as conn:
        linked, new_ids, matched = link_jobs_to_companies(conn)
    return {"linked": linked, "new_companies": len(new_ids), "matched": matched}


def run_ats_probing(db_path: str, config: dict) -> dict:
    """Run ATS probing on companies with pending probe status.

    Calls probe_ats_slugs() which opens its own sqlite3 connection
    (thread-safe pattern). Prints results to stdout.

    Args:
        db_path: Absolute path to the SQLite database file.
        config: Application config dict. If config['TESTING'] is True,
                probe_ats_slugs returns early without API calls.

    Returns:
        Dict with probed, hits, misses counts.
    """
    print("\n--- ATS Probing ---")
    print("Probing ATS APIs for pending companies...")

    result = probe_ats_slugs(db_path, config)

    print(
        f"ATS probe complete: probed={result.get('probed', 0)}, "
        f"hits={result.get('hits', 0)}, misses={result.get('misses', 0)}"
    )

    return result


def run_scheduled_enrichment(db_path: str, config: dict) -> dict:
    """Scheduler-compatible wrapper: enrich up to 50 unenriched companies.

    Selects companies with no company_size AND no industry set.
    Opens its own connection (thread-safe for APScheduler).

    Args:
        db_path: Absolute path to the SQLite database file.
        config: Application config dict (unused, kept for scheduler signature).

    Returns:
        Dict with checked (int) and enriched (int) counts.
    """
    with standalone_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT id FROM companies WHERE company_size IS NULL AND industry IS NULL LIMIT 50"
        ).fetchall()
        company_ids = [r["id"] for r in rows]
        enriched = run_ddg_enrichment(conn, company_ids)
    return {"checked": len(company_ids), "enriched": enriched}


def run_orphan_cleanup(db_path: str, config: dict) -> dict:
    """Scheduler-compatible wrapper for cleanup_orphan_companies().

    Args:
        db_path: Absolute path to the SQLite database file.
        config: Application config dict (unused, kept for scheduler signature).

    Returns:
        Dict with orphans_deleted and recalibrated_total counts.
    """
    with standalone_connection(db_path) as conn:
        return cleanup_orphan_companies(conn)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """CLI entry point for company backfill.

    Loads config, opens its own sqlite3 connection (WAL-safe, not Flask g.db),
    prints initial state, runs all three phases:
    1. link_jobs_to_companies — fuzzy match + create + link
    2. run_ats_probing — probe ATS APIs for new companies
    3. run_ddg_enrichment — enrich new companies with DDG data

    Prints final summary with all metrics.
    """
    config = load_config()
    db_path = config["db"]["path"]

    with standalone_connection(db_path) as conn:
        # Print initial state
        null_count = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE company_id IS NULL"
        ).fetchone()[0]
        total_count = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        company_count = conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]

        print("=== Company Backfill ===")
        print(f"Initial state: {null_count}/{total_count} jobs have NULL company_id")
        print(f"Existing company records: {company_count}")
        print()

        # Phase 1: Link jobs to companies
        print("--- Phase 1: Linking jobs to company records ---")
        linked_count, new_company_ids, matched_count = link_jobs_to_companies(conn)

        # Phase 2: ATS probing
        ats_result = run_ats_probing(db_path, config)

        # Phase 3: DDG enrichment
        ddg_count = run_ddg_enrichment(conn, new_company_ids)

        # Final summary
        null_after = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE company_id IS NULL"
        ).fetchone()[0]
        company_count_after = conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]

        print("\n=== Final Summary ===")
        print(f"Jobs linked:             {linked_count}")
        print(f"Companies created:       {len(new_company_ids)}")
        print(f"Companies matched:       {matched_count}")
        print(f"Jobs still unlinked:     {null_after}")
        print(f"Total company records:   {company_count_after}")
        print(f"ATS probed:              {ats_result.get('probed', 0)}")
        print(f"ATS hits:                {ats_result.get('hits', 0)}")
        print(f"ATS misses:              {ats_result.get('misses', 0)}")
        print(f"DDG enriched:            {ddg_count}")


if __name__ == "__main__":
    main()
