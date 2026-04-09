"""Company backfill orchestration for job-finder.

Batch orchestration layer — runs company linkage, ATS probing, DDG enrichment,
and orphan cleanup. Individual resolution functions live in company_resolver.py.

Purpose:
    Orchestrates all company-related backfill operations:
    1. Fuzzy-match and link jobs to company records
    2. Run ATS probing on newly created companies
    3. Run DuckDuckGo enrichment on newly created companies
    4. Clean up orphan company records nightly
    5. Registry hygiene (denylist + orphan cleanup, monthly)

Usage:
    python -m job_finder.web.backfill_companies

Exports:
    main: CLI entry point.
    run_company_linkage: Scheduler-compatible linkage wrapper.
    run_ats_probing: Scheduler-compatible ATS probe wrapper.
    run_scheduled_enrichment: Scheduler-compatible enrichment wrapper.
    run_orphan_cleanup: Scheduler-compatible orphan cleanup wrapper.
    run_registry_hygiene: Scheduler-compatible hygiene wrapper (monthly).
    cleanup_invalid_company_data: One-time linkage-repair backfill.
    cleanup_orphan_companies: Orphan detection and deletion.

    # Re-exported from company_resolver for backwards compatibility:
    cleanup_denylist_companies, find_duplicate_companies,
    find_fuzzy_false_positives, fuzzy_match_company, link_jobs_to_companies,
    run_ddg_enrichment, verify_homepage_urls, verify_all_linkable_jobs_linked
"""

import logging
import sqlite3

from job_finder.config import get_company_denylist, load_config
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
    "cleanup_invalid_company_data",
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
    "run_registry_hygiene",
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


def cleanup_invalid_company_data(
    conn: sqlite3.Connection,
    config: dict,
    dry_run: bool = False,
) -> dict:
    """Repair company linkage for invalid/garbage company names.

    Scans distinct jobs.company values through the shared sanitizer. For
    normalizable values, finds/creates the correct company record and updates
    jobs.company_id. For rejected values, nulls jobs.company_id.

    NEVER modifies jobs.company — that field is the raw source-of-truth.
    Only jobs.company_id linkage is repaired. After linkage repair, runs
    orphan cleanup to remove dead company rows.

    This function is idempotent: running it twice produces no new repair work
    for already-handled rows.

    Args:
        conn: Open SQLite connection.
        config: Application config dict (for denylist/allowlist enforcement).
        dry_run: If True, log what would happen but make no changes.

    Returns:
        Dict with normalized (int), rejected (int), orphans_deleted (int) counts.
    """
    from job_finder.web.ats_company import classify_company_name, upsert_company

    normalized_count = 0
    rejected_count = 0

    # Scan all distinct jobs.company values with NULL company_id (unlinked)
    rows = conn.execute(
        "SELECT DISTINCT company FROM jobs WHERE company IS NOT NULL"
    ).fetchall()
    distinct_names = [r["company"] for r in rows]

    logger.info(
        "cleanup_invalid_company_data: scanning %d distinct company values%s",
        len(distinct_names),
        " (dry_run)" if dry_run else "",
    )

    for raw_name in distinct_names:
        decision = classify_company_name(raw_name, config=config)

        if decision.action == "reject":
            rejected_count += 1
            logger.warning(
                "cleanup: nulling company_id for rejected company '%s' (reason=%s)",
                raw_name[:60], decision.reason,
            )
            if not dry_run:
                conn.execute(
                    "UPDATE jobs SET company_id = NULL WHERE company = ? AND company_id IS NOT NULL",
                    (raw_name,),
                )

        elif decision.action in ("accept", "normalize"):
            # Find or create the correct company record under the cleaned name
            if not dry_run:
                company_id = upsert_company(conn, raw_name)
                if company_id is not None:
                    conn.execute(
                        "UPDATE jobs SET company_id = ? WHERE company = ? AND company_id IS NULL",
                        (company_id, raw_name),
                    )
                    normalized_count += 1
            else:
                normalized_count += 1

    if not dry_run:
        conn.commit()

    # Run orphan cleanup after linkage repair
    orphan_result = {"orphans_deleted": 0, "recalibrated_total": 0}
    if not dry_run:
        orphan_result = cleanup_orphan_companies(conn)

    result = {
        "normalized": normalized_count,
        "rejected": rejected_count,
        "orphans_deleted": orphan_result["orphans_deleted"],
    }
    logger.info("cleanup_invalid_company_data complete: %s", result)
    return result


def run_scheduled_enrichment(db_path: str, config: dict) -> dict:
    """Scheduler-compatible wrapper: enrich up to 200 companies with retry backoff.

    Selects companies where at least one metadata field is missing
    (company_size IS NULL OR industry IS NULL), backoff window has elapsed,
    and the company is not on the denylist. Orders by oldest eligible work first.

    Opens its own connection (thread-safe for APScheduler).

    Args:
        db_path: Absolute path to the SQLite database file.
        config: Application config dict (for denylist enforcement).

    Returns:
        Dict with checked, enriched, empty_result, error counts.
    """
    denylist = get_company_denylist(config)
    # Build parameterized NOT IN clause; use a sentinel that matches nothing when denylist is empty
    if denylist:
        denylist_placeholders = ", ".join("?" * len(denylist))
        denylist_params: list = list(denylist)
    else:
        denylist_placeholders = "?"
        denylist_params = ["__never_match__"]

    with standalone_connection(db_path) as conn:
        rows = conn.execute(
            f"""SELECT id FROM companies
               WHERE (company_size IS NULL OR industry IS NULL)
                 AND (enrichment_backoff_until IS NULL
                      OR enrichment_backoff_until <= datetime('now'))
                 AND LOWER(name) NOT IN ({denylist_placeholders})
               ORDER BY enrichment_last_attempted_at ASC NULLS FIRST,
                        enrichment_backoff_until ASC NULLS FIRST,
                        id ASC
               LIMIT 200""",
            denylist_params,
        ).fetchall()
        company_ids = [r["id"] for r in rows]
        ddg_result = run_ddg_enrichment(conn, company_ids)

    result = {
        "checked": len(company_ids),
        "enriched": ddg_result["enriched"],
        "empty_result": ddg_result["empty_result"],
        "error": ddg_result["error"],
    }
    logger.info("run_scheduled_enrichment: %s", result)
    return result


def run_registry_hygiene(db_path: str, config: dict) -> dict:
    """Monthly registry hygiene: denylist cleanup then orphan cleanup.

    Runs in a single standalone connection (thread-safe for APScheduler).
    Order is critical: denylist cleanup unlinking jobs before orphan cleanup
    ensures denylist-matched company rows become orphans and are deleted.

    Args:
        db_path: Absolute path to the SQLite database file.
        config: Application config dict (for denylist enforcement).

    Returns:
        Dict with companies_denylist_deleted, jobs_denylist_unlinked,
        orphans_deleted counts.
    """
    with standalone_connection(db_path) as conn:
        denylist_result = cleanup_denylist_companies(conn, config)
        orphan_result = cleanup_orphan_companies(conn)

    result = {
        "companies_denylist_deleted": denylist_result["companies_deleted"],
        "jobs_denylist_unlinked": denylist_result["jobs_unlinked"],
        "orphans_deleted": orphan_result["orphans_deleted"],
    }
    logger.info("run_registry_hygiene: %s", result)
    return result


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
        ddg_result = run_ddg_enrichment(conn, new_company_ids)
        ddg_count = ddg_result["enriched"] if isinstance(ddg_result, dict) else ddg_result

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
