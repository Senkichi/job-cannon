"""Company backfill script for job-finder.

Links all jobs with NULL company_id to company records using fuzzy matching.
Creates new company records for unmatched names, then triggers ATS probing
on newly created records.

Purpose:
    Jobs with NULL company_id are linked to existing or newly-created
    company records:
    1. Normalizes company names from unlinked jobs
    2. Fuzzy-matches against existing company records (threshold=85)
    3. Creates new company records for unmatched names (via upsert_company)
    4. Links all jobs to their company_id
    5. Runs ATS probing on newly created companies

Usage:
    python -m job_finder.web.backfill_companies

Exports:
    main: CLI entry point.
    cleanup_denylist_companies: Remove denylist placeholder company records.
    find_duplicate_companies: Find companies sharing the same normalized name.
    find_fuzzy_false_positives: Find high-scoring cross-name company pairs for review.
    fuzzy_match_company: Fuzzy-match a raw name against existing companies.
    link_jobs_to_companies: Link all unlinked jobs to company records.
    run_ats_probing: Run ATS probing on pending companies.
    verify_homepage_urls: Check reachability of company homepage URLs.
    verify_all_linkable_jobs_linked: Verify all non-denylist jobs have company links.
"""

import logging
import sqlite3

from thefuzz import fuzz

from job_finder.config import COMPANY_DENYLIST, get_company_denylist, load_config
from job_finder.web import company_resolver as _company_resolver
from job_finder.web.ats_scanner import probe_ats_slugs
from job_finder.web.db_helpers import standalone_connection
from job_finder.web.dedup_normalizer import normalize_company

logger = logging.getLogger(__name__)

# Fuzzy match threshold (0–100). Score >= this means a match.
_FUZZY_THRESHOLD = 85

# Minimum normalized name length for fuzzy matching (short names are unreliable)
_MIN_NAME_LEN = 4

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def fuzzy_match_company(
    raw_name: str,
    existing_companies: list[tuple[int, str]],
    threshold: int = _FUZZY_THRESHOLD,
) -> tuple[int | None, int]:
    """Fuzzy-match a raw company name against existing company records.

    Normalizes raw_name via normalize_company(). If the normalized name is
    shorter than _MIN_NAME_LEN characters, returns (None, 0) immediately
    (short names produce too many false positives).

    Uses fuzz.token_set_ratio() to handle word-order variations
    (e.g. "Inc Stripe" vs "Stripe Inc").

    Args:
        raw_name: Raw company name string to match.
        existing_companies: List of (company_id, normalized_name) tuples.
        threshold: Minimum score to accept as a match (default 85).

    Returns:
        Tuple of (best_company_id, best_score). Returns (None, 0) if no
        match meets the threshold.
    """
    normalized = normalize_company(raw_name)

    # Guard: skip fuzzy matching for very short names (too unreliable)
    if len(normalized) < _MIN_NAME_LEN:
        return None, 0

    best_id: int | None = None
    best_score: int = 0

    for company_id, company_name in existing_companies:
        score = fuzz.token_set_ratio(normalized, company_name)
        if score > best_score:
            best_score = score
            best_id = company_id

    if best_score >= threshold:
        return best_id, best_score

    return None, 0


def cleanup_denylist_companies(conn: sqlite3.Connection, config: dict | None = None) -> dict:
    """Remove denylist placeholder company records and unlink their jobs.

    Queries the companies table for any row whose LOWER(name) matches an
    entry in COMPANY_DENYLIST (e.g. "Medical jobs", "Mercor"). For each
    found company, sets company_id = NULL on all linked jobs, then deletes
    the company record.

    Args:
        conn: Open SQLite connection (all migrations applied).
        config: Optional full config dict. If provided, merges config.yaml
                filters.company_denylist entries with hardcoded defaults.
                If None, only the hardcoded COMPANY_DENYLIST is used.

    Returns:
        Dict with keys "companies_deleted" (int) and "jobs_unlinked" (int).
    """
    denylist = get_company_denylist(config) if config else COMPANY_DENYLIST
    placeholders = ", ".join("?" * len(denylist))
    denylist_entries = list(denylist)

    rows = conn.execute(
        f"SELECT id, name FROM companies WHERE LOWER(name) IN ({placeholders})",
        denylist_entries,
    ).fetchall()

    if not rows:
        return {"companies_deleted": 0, "jobs_unlinked": 0}

    ids_to_delete = [row["id"] for row in rows]
    jobs_unlinked = 0

    for company_id in ids_to_delete:
        result = conn.execute(
            "UPDATE jobs SET company_id = NULL WHERE company_id = ?",
            (company_id,),
        )
        jobs_unlinked += result.rowcount

    id_placeholders = ", ".join("?" * len(ids_to_delete))
    conn.execute(
        f"DELETE FROM companies WHERE id IN ({id_placeholders})",
        ids_to_delete,
    )
    conn.commit()

    companies_deleted = len(ids_to_delete)
    logger.info(
        "Denylist cleanup: deleted %d companies, unlinked %d jobs",
        companies_deleted,
        jobs_unlinked,
    )

    return {"companies_deleted": companies_deleted, "jobs_unlinked": jobs_unlinked}


def find_duplicate_companies(
    conn: sqlite3.Connection,
) -> list[tuple[int, int, str]]:
    """Find companies that share the same normalized name.

    Queries all companies and groups them by normalize_company(name). Any
    group with more than one entry represents duplicate company records.

    Args:
        conn: Open SQLite connection (all migrations applied).

    Returns:
        List of tuples (id_a, id_b, normalized_name) for each duplicate pair.
        Returns empty list if no duplicates exist.
    """
    rows = conn.execute("SELECT id, name, name_raw FROM companies").fetchall()

    groups: dict[str, list[int]] = {}
    for row in rows:
        norm = normalize_company(row["name"])
        groups.setdefault(norm, []).append(row["id"])

    duplicates: list[tuple[int, int, str]] = []
    for norm_name, ids in groups.items():
        if len(ids) > 1:
            # Emit all pairs for this normalized name
            for i in range(len(ids)):
                for j in range(i + 1, len(ids)):
                    duplicates.append((ids[i], ids[j], norm_name))

    return duplicates


def find_fuzzy_false_positives(
    conn: sqlite3.Connection,
    threshold: int = 85,
) -> list[dict]:
    """Find company pairs with high fuzzy scores but different normalized names.

    These are candidates for false-positive merges — unrelated companies whose
    names score >= threshold under fuzz.token_set_ratio but are NOT duplicates
    (different normalized names). Should be reviewed to confirm no incorrect
    merges occurred during backfill.

    O(n^2) comparison, but with ~325 companies (~52k pairs) runs in < 2 seconds.

    Args:
        conn: Open SQLite connection (all migrations applied).
        threshold: Minimum fuzz.token_set_ratio score to flag a pair (default 85).

    Returns:
        List of dicts: {"id_a": int, "name_a": str, "id_b": int, "name_b": str, "score": int}
        Only pairs with id_a < id_b are returned (no symmetric duplicates).
    """
    rows = conn.execute("SELECT id, name, name_raw FROM companies").fetchall()
    companies = [(row["id"], row["name"], row["name_raw"]) for row in rows]

    results: list[dict] = []
    n = len(companies)

    # Pre-compute normalizations to reduce O(n^2) normalize_company calls to O(n)
    normalized = [normalize_company(c[1]) for c in companies]

    for i in range(n):
        id_a, name_a, name_raw_a = companies[i]
        norm_a = normalized[i]

        for j in range(i + 1, n):
            id_b, name_b, name_raw_b = companies[j]
            norm_b = normalized[j]

            # Only flag pairs with DIFFERENT normalized names
            # (same normalized name = duplicate, handled by find_duplicate_companies)
            if norm_a == norm_b:
                continue

            score = fuzz.token_set_ratio(norm_a, norm_b)
            if score >= threshold:
                results.append(
                    {
                        "id_a": id_a,
                        "name_a": name_raw_a or name_a,
                        "id_b": id_b,
                        "name_b": name_raw_b or name_b,
                        "score": score,
                    }
                )

    return results


def verify_homepage_urls(conn: sqlite3.Connection) -> list[dict]:
    """Check reachability of DDG-populated homepage URLs in the companies table.

    Queries all companies with a non-null homepage_url and performs a HEAD
    request to each URL to verify it is reachable (HTTP 200-399).

    Args:
        conn: Open SQLite connection (all migrations applied).

    Returns:
        List of dicts: {"id": int, "name_raw": str, "homepage_url": str, "reachable": bool}
        Returns empty list if no companies have a homepage_url.
    """
    import requests

    rows = conn.execute(
        "SELECT id, name_raw, homepage_url FROM companies WHERE homepage_url IS NOT NULL"
    ).fetchall()

    results: list[dict] = []
    reachable_count = 0

    for row in rows:
        company_id = row["id"]
        name_raw = row["name_raw"]
        homepage_url = row["homepage_url"]

        try:
            response = requests.head(homepage_url, timeout=5, allow_redirects=True)
            reachable = 200 <= response.status_code < 400
        except Exception:
            reachable = False

        if reachable:
            reachable_count += 1

        results.append(
            {
                "id": company_id,
                "name_raw": name_raw,
                "homepage_url": homepage_url,
                "reachable": reachable,
            }
        )

    logger.info(
        "verify_homepage_urls: %d companies with URLs, %d reachable",
        len(results),
        reachable_count,
    )

    return results


def verify_all_linkable_jobs_linked(conn: sqlite3.Connection) -> dict:
    """Verify that all non-denylist jobs with a company name are linked to a company record.

    Queries jobs with company_id IS NULL AND company IS NOT NULL. For each,
    checks whether the company name is in COMPANY_DENYLIST. Returns counts and
    details for review.

    This function is read-only — it does NOT modify the database.

    Args:
        conn: Open SQLite connection (all migrations applied).

    Returns:
        Dict with:
        - "unlinked_non_denylist": int — count of unlinked jobs with non-denylist company names
        - "unlinked_denylist": int — count of unlinked jobs with denylist company names
        - "unlinked_details": list[dict] — each dict has "dedup_key", "company", "is_denylist"
    """
    rows = conn.execute(
        "SELECT dedup_key, company FROM jobs WHERE company_id IS NULL AND company IS NOT NULL"
    ).fetchall()

    unlinked_non_denylist = 0
    unlinked_denylist = 0
    unlinked_details: list[dict] = []

    for row in rows:
        dedup_key = row["dedup_key"]
        company = row["company"]
        normalized = normalize_company(company).lower()
        is_denylist = normalized in COMPANY_DENYLIST

        if is_denylist:
            unlinked_denylist += 1
        else:
            unlinked_non_denylist += 1

        unlinked_details.append(
            {
                "dedup_key": dedup_key,
                "company": company,
                "is_denylist": is_denylist,
            }
        )

    return {
        "unlinked_non_denylist": unlinked_non_denylist,
        "unlinked_denylist": unlinked_denylist,
        "unlinked_details": unlinked_details,
    }


def link_jobs_to_companies(
    conn: sqlite3.Connection,
) -> tuple[int, list[int], int]:
    """Link all unlinked jobs to company records using fuzzy matching.

    For each distinct company name from unlinked jobs:
    - Skip if normalized name is in COMPANY_DENYLIST
    - Try fuzzy_match_company against existing company records
    - If match found: use existing company_id (increment matched_count)
    - If no match: call upsert_company to create new record, append to
      existing_companies list so subsequent jobs with similar names match
      to it instead of creating duplicates
    - UPDATE all jobs with that company name to set company_id

    Args:
        conn: Open SQLite connection (all migrations applied).

    Returns:
        Tuple of (linked_count, new_company_ids, matched_count).
        linked_count: Total number of jobs updated with a company_id.
        new_company_ids: List of IDs for newly created company records.
        matched_count: Number of company names that fuzzy-matched existing records.
    """
    # Load all existing company records
    existing_rows = conn.execute("SELECT id, name FROM companies").fetchall()
    existing_companies: list[tuple[int, str]] = [(row["id"], row["name"]) for row in existing_rows]

    # Get all distinct company names from unlinked jobs
    unlinked_rows = conn.execute(
        "SELECT DISTINCT company FROM jobs WHERE company_id IS NULL AND company IS NOT NULL"
    ).fetchall()
    distinct_names: list[str] = [row["company"] for row in unlinked_rows]

    logger.info(
        "link_jobs_to_companies: %d distinct company names from %d unlinked job groups",
        len(distinct_names),
        len(distinct_names),
    )

    linked_count = 0
    new_company_ids: list[int] = []
    matched_count = 0

    for raw_name in distinct_names:
        normalized = normalize_company(raw_name).lower()

        # Skip denylist names
        if normalized in COMPANY_DENYLIST:
            logger.debug("Skipping denylist company: %s", raw_name)
            continue

        # Try fuzzy match against existing companies
        matched_id, score = fuzzy_match_company(raw_name, existing_companies)

        if matched_id is not None:
            # Fuzzy match found — use existing company_id
            company_id = matched_id
            matched_count += 1
            logger.debug(
                "Fuzzy match: '%s' -> company_id=%d (score=%d)", raw_name, company_id, score
            )
        else:
            # No match — create new company record
            company_id = _company_resolver.upsert_company(conn, raw_name)
            if company_id is None:
                logger.warning("upsert_company returned None for '%s' — skipping", raw_name)
                continue

            new_company_ids.append(company_id)

            # Append to existing_companies list so subsequent jobs with similar names
            # fuzzy-match to this new record (prevents duplicate company creation)
            normalized_new = normalize_company(raw_name)
            existing_companies.append((company_id, normalized_new))

            logger.debug("Created new company: '%s' -> id=%d", raw_name, company_id)

        # UPDATE all matching unlinked jobs
        result = conn.execute(
            "UPDATE jobs SET company_id = ? WHERE company = ? AND company_id IS NULL",
            (company_id, raw_name),
        )
        linked_count += result.rowcount

    conn.commit()

    logger.info(
        "link_jobs_to_companies complete: linked=%d, new_companies=%d, matched=%d",
        linked_count,
        len(new_company_ids),
        matched_count,
    )

    return linked_count, new_company_ids, matched_count


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


def cleanup_orphan_companies(conn: sqlite3.Connection) -> dict:
    """Delete companies with no linked jobs and no scan history.

    Also recalibrates jobs_found_total to actual linked job count for all
    remaining companies.

    Returns:
        Dict with "orphans_deleted" (int) and "recalibrated_total" (int).
    """
    # Find orphans: no linked jobs AND no scan history
    orphan_ids = conn.execute(
        """SELECT c.id FROM companies c
           WHERE NOT EXISTS (SELECT 1 FROM jobs j WHERE j.company_id = c.id)
             AND NOT EXISTS (SELECT 1 FROM company_scan_log s WHERE s.company_id = c.id)"""
    ).fetchall()
    orphan_id_list = [r[0] for r in orphan_ids]

    deleted = 0
    for oid in orphan_id_list:
        conn.execute("DELETE FROM companies WHERE id = ?", (oid,))
        deleted += 1
    conn.commit()

    # Recalibrate jobs_found_total for all remaining companies in a single
    # correlated UPDATE. Acquires the SQLite writer once instead of once per
    # company row (~thousands of writer-lock acquires under heavy registries).
    conn.execute(
        """
        UPDATE companies
           SET jobs_found_total = (
               SELECT COUNT(*) FROM jobs WHERE jobs.company_id = companies.id
           )
        """
    )
    recalibrated = conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
    conn.commit()

    return {"orphans_deleted": deleted, "recalibrated_total": recalibrated}


def run_orphan_cleanup(db_path: str, config: dict) -> dict:
    """Wrapper for cleanup_orphan_companies that opens its own connection."""
    with standalone_connection(db_path) as conn:
        return cleanup_orphan_companies(conn)


def run_company_linkage(db_path: str, config: dict) -> dict:
    """Link unlinked jobs to company records. Returns summary dict."""
    with standalone_connection(db_path) as conn:
        linked_count, _new_ids, _matched = link_jobs_to_companies(conn)
        return {"linked": linked_count}


def cleanup_invalid_company_data(
    conn: sqlite3.Connection,
    config: dict | None = None,
) -> dict:
    """Null company_id for jobs linked to denylist companies; re-normalize linkable ones.

    Never mutates jobs.company (the raw name). Only modifies company_id.
    Idempotent.

    Returns:
        Dict with "normalized" (int) count of jobs re-linked.
    """
    denylist = get_company_denylist(config or {})
    normalized_count = 0

    # Phase 1: Null out company_id for jobs linked to denylist companies.
    # Collect dedup_keys first, then issue chunked UPDATEs so the writer is
    # acquired once per chunk instead of once per row.
    rows = conn.execute(
        """SELECT j.dedup_key, c.name
           FROM jobs j
           JOIN companies c ON j.company_id = c.id"""
    ).fetchall()
    denylist_keys = [row[0] for row in rows if row[1] in denylist]

    _BATCH = 500
    for i in range(0, len(denylist_keys), _BATCH):
        chunk = denylist_keys[i : i + _BATCH]
        placeholders = ", ".join("?" * len(chunk))
        conn.execute(
            f"UPDATE jobs SET company_id = NULL WHERE dedup_key IN ({placeholders})",
            chunk,
        )

    # Phase 2: Re-normalize unlinked jobs. Each row may require a new company
    # upsert, so writes stay per-row, but they're committed once at the end so
    # the writer is held continuously instead of acquired+released per row.
    unlinked = conn.execute(
        "SELECT dedup_key, company FROM jobs WHERE company_id IS NULL AND company IS NOT NULL"
    ).fetchall()

    existing = conn.execute("SELECT id, name FROM companies").fetchall()
    existing_map = {r[1]: r[0] for r in existing}

    for row in unlinked:
        dedup_key, raw_name = row[0], row[1]
        norm = normalize_company(raw_name)
        if not norm or norm in denylist:
            continue

        if norm in existing_map:
            company_id = existing_map[norm]
        else:
            company_id = _company_resolver.upsert_company(conn, raw_name)
            existing_map[norm] = company_id

        conn.execute(
            "UPDATE jobs SET company_id = ? WHERE dedup_key = ?",
            (company_id, dedup_key),
        )
        normalized_count += 1

    conn.commit()
    return {"normalized": normalized_count}


def run_registry_hygiene(db_path: str, config: dict) -> dict:
    """Orchestrator: cleanup denylist companies, repair links, then orphan cleanup.

    Returns:
        Dict with "companies_denylist_deleted", "jobs_denylist_unlinked",
        "jobs_normalized", "orphans_deleted" (all int).
    """
    with standalone_connection(db_path) as conn:
        denylist_result = cleanup_denylist_companies(conn, config)
        repair_result = cleanup_invalid_company_data(conn, config)
        orphan_result = cleanup_orphan_companies(conn)

    return {
        "companies_denylist_deleted": denylist_result.get("companies_deleted", 0),
        "jobs_denylist_unlinked": denylist_result.get("jobs_unlinked", 0),
        "jobs_normalized": repair_result.get("normalized", 0),
        "orphans_deleted": orphan_result["orphans_deleted"],
    }


def main() -> None:
    """CLI entry point for company backfill.

    Loads config, opens its own sqlite3 connection (WAL-safe, not Flask g.db),
    prints initial state, runs two phases:
    1. link_jobs_to_companies — fuzzy match + create + link
    2. run_ats_probing — probe ATS APIs for new companies

    Prints final summary with all metrics.
    """
    config = load_config()
    db_path = config["db"]["path"]

    with standalone_connection(db_path) as conn:
        # Print initial state
        null_count = conn.execute("SELECT COUNT(*) FROM jobs WHERE company_id IS NULL").fetchone()[
            0
        ]
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

        # Final summary
        null_after = conn.execute("SELECT COUNT(*) FROM jobs WHERE company_id IS NULL").fetchone()[
            0
        ]
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


if __name__ == "__main__":
    main()
