"""Enrichment orchestration helpers: fragment resolution, persistence, and URL utilities.

These are internal helpers used by data_enricher.enrich_job(). They handle:
  - Fragment resolution: mapping collected text fragments to DB columns
  - Persistence: atomically writing enriched fields + tier to SQLite
  - URL utilities: parsing source_urls JSON, merging ATS apply URLs
  - Text composition: assembling fragment text for AI extraction tiers
  - Tier index: computing resume point based on current enrichment_tier
"""

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Allowlist of jobs table columns that _persist() may write. Prevents AI-extracted
# dict keys from injecting arbitrary column names into dynamic SQL SET clauses.
_ENRICHABLE_COLUMNS = frozenset({"jd_full", "salary_min", "salary_max", "location"})

# Minimum character length for jd_full to be considered a real job description.
_MIN_JD_LENGTH = 200


def find_missing_fields(job_row: dict, is_stub_jd_fn) -> list:
    """Return list of missing scoring-relevant field names.

    A job needs enrichment if any of these are missing:
    - jd_full: full job description (needed for Sonnet). Stubs (title
      restatements < 200 chars) are treated as missing.
    - salary_min: minimum salary

    Args:
        job_row: Job record dict.
        is_stub_jd_fn: Callable matching is_stub_jd(jd_text, title, company) -> bool.

    Returns:
        Empty list if all fields are present (no enrichment needed).
    """
    missing = []
    if is_stub_jd_fn(job_row.get("jd_full"), job_row.get("title", ""), job_row.get("company", "")):
        missing.append("jd_full")
    if job_row.get("salary_min") is None:
        missing.append("salary_min")
    return missing


def filter_non_none(d: dict) -> dict:
    """Return a new dict with None values removed."""
    return {k: v for k, v in d.items() if v is not None}


def start_tier_index(current_tier: str | None, tier_order: list) -> int:
    """Return the index in tier_order to start from based on current_tier.

    If current_tier is None or not found, start from 0 (beginning).
    Otherwise, start from the tier AFTER current_tier.

    Args:
        current_tier: The enrichment_tier value from the job row.
        tier_order: Ordered list of tier name strings.

    Returns:
        Index into tier_order to start enrichment from.
    """
    if current_tier is None:
        return 0
    try:
        idx = tier_order.index(current_tier)
        return idx + 1  # Resume from NEXT tier
    except ValueError:
        return 0


def parse_source_urls(source_urls_json: str | None) -> list:
    """Parse source_urls JSON field into a list of URL strings.

    Args:
        source_urls_json: JSON string like '["https://..."]' or None.

    Returns:
        List of URL strings. Empty list if None or unparseable.
    """
    if not source_urls_json:
        return []
    try:
        urls = json.loads(source_urls_json)
        return [u for u in urls if isinstance(u, str)]
    except (json.JSONDecodeError, TypeError):
        return []


def compose_fragment_text(fragments: dict, title: str, company: str) -> str | None:
    """Compose a single text string from all accumulated fragments.

    Args:
        fragments: Dict of fragment texts collected from prior tiers.
        title: Job title for fallback context.
        company: Company name for fallback context.

    Returns:
        Aggregated text string for use in Haiku/Sonnet extraction,
        or None if no meaningful fragments exist (prevents AI tiers from
        hallucinating JDs from titles alone).
    """
    parts = []
    for _key, text in fragments.items():
        if text and isinstance(text, str):
            parts.append(str(text)[:1000])

    if parts:
        return "\n\n".join(parts)
    return None


def resolve_from_fragments(
    fragments: dict,
    missing: list,
    job_row: dict,
    is_stub_jd_fn,
) -> dict:
    """Build an enriched dict from fragments for the fields that are missing.

    Looks for direct matches: fragments['jd_full'] -> jd_full,
    fragments['url_jd'] -> jd_full, fragments['salary_min'] -> salary_min, etc.

    Rejects stub JDs (title restatements) via is_stub_jd_fn check.

    Args:
        fragments: Dict of collected data from free-tier sources.
        missing: List of field names that are still missing.
        job_row: Original job row for reference.
        is_stub_jd_fn: Callable matching is_stub_jd(jd_text, title, company) -> bool.

    Returns:
        Dict of {field: value} for fields that fragments can satisfy.
    """
    title = job_row.get("title", "")
    company = job_row.get("company", "")
    enriched = {}
    for field in missing:
        # Direct key match
        if field in fragments and fragments[field] is not None:
            # Reject stub jd_full values
            if field == "jd_full" and is_stub_jd_fn(fragments[field], title, company):
                continue
            enriched[field] = fragments[field]
        # url_jd maps to jd_full
        elif field == "jd_full" and fragments.get("url_jd"):
            if is_stub_jd_fn(fragments["url_jd"], title, company):
                continue
            enriched["jd_full"] = fragments["url_jd"]

    return filter_non_none(enriched)


def merge_apply_urls(conn: Any, dedup_key: str, apply_urls: list) -> None:
    """Read-merge-write source_urls JSON column with new ATS URLs from SerpAPI apply_options.

    Designed to be called AFTER persist() succeeds so both writes succeed or
    both fail together (best-effort atomicity — SQLite has no multi-statement
    rollback across these two UPDATE calls, but failure of the second is non-critical).

    Args:
        conn: Open SQLite connection.
        dedup_key: Job dedup key to update.
        apply_urls: New URL strings to merge in (deduplicated against existing list).
    """
    if not conn or not dedup_key or not apply_urls:
        return
    try:
        existing_row = conn.execute(
            "SELECT source_urls FROM jobs WHERE dedup_key = ?", (dedup_key,)
        ).fetchone()
        existing_json = existing_row["source_urls"] if existing_row else None
        try:
            existing_list = json.loads(existing_json) if existing_json else []
            if not isinstance(existing_list, list):
                existing_list = []
        except (json.JSONDecodeError, TypeError):
            existing_list = []
        merged = existing_list + [u for u in apply_urls if u not in existing_list]
        url_cursor = conn.execute(
            "UPDATE jobs SET source_urls = ? WHERE dedup_key = ?",
            (json.dumps(merged), dedup_key),
        )
        if url_cursor.rowcount == 0:
            logger.warning(
                "source_urls UPDATE matched 0 rows for dedup_key=%s "
                "(job deleted between enrich_job start and write?)",
                dedup_key,
            )
        conn.commit()
        logger.debug(
            "source_urls: merged %d new ATS URL(s) for dedup_key=%s",
            len(merged) - len(existing_list),
            dedup_key,
        )
    except Exception as exc:
        logger.debug("merge_apply_urls failed for dedup_key=%s: %s", dedup_key, exc)


def persist(conn: Any, job_row: dict, enriched: dict, tier_name: str) -> None:
    """Persist enriched fields + enrichment_tier atomically in a single UPDATE.

    Only writes to DB if conn is provided. If enriched is empty, still
    updates enrichment_tier to track progress (unless conn is None).

    Args:
        conn: Open SQLite connection. If None, skip persistence.
        job_row: Job row dict (must have 'dedup_key').
        enriched: Dict of {column_name: value} to update.
        tier_name: The enrichment tier name to record.
    """
    if conn is None:
        return

    dedup_key = job_row.get("dedup_key")
    if not dedup_key:
        return

    try:
        if enriched:
            # Filter to allowlisted columns only — prevents AI-extracted keys from
            # injecting arbitrary column names into the dynamic SQL SET clause.
            safe_enriched = {k: v for k, v in enriched.items() if k in _ENRICHABLE_COLUMNS}
            if safe_enriched != enriched:
                unknown = set(enriched) - _ENRICHABLE_COLUMNS
                logger.warning("persist: dropping non-allowlisted columns: %s", unknown)
        else:
            safe_enriched = {}

        if safe_enriched:
            set_clauses = ", ".join(f"{k} = ?" for k in safe_enriched)
            set_clauses += ", enrichment_tier = ?"
            values = list(safe_enriched.values()) + [tier_name, dedup_key]
            conn.execute(
                f"UPDATE jobs SET {set_clauses} WHERE dedup_key = ?",
                values,
            )
        else:
            conn.execute(
                "UPDATE jobs SET enrichment_tier = ? WHERE dedup_key = ?",
                (tier_name, dedup_key),
            )
        conn.commit()
    except Exception as e:
        logger.warning("Failed to persist enrichment for '%s': %s", dedup_key, e)
