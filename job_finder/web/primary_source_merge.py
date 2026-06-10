"""Merge authoritative fields from a strict-matched primary posting.

merge_primary_posting_fields is called when resolve_primary_posting produced a
STRICT match — the posting is the same real-world job as the stored row, so
its structured fields (salary metadata, posted date, locations, the ATS URL
itself) are authoritative and worth folding in.

The merge is routed through upsert_job (D-15: source_urls is a parser-owned
column) so every field follows the canonical merge rules — set-union
sources/source_urls, keep-longer description, Remote/Hybrid-first locations,
COALESCE fills — instead of ad-hoc UPDATE bypasses.

Identity is pinned to the EXISTING row (dedup_key/title/company): the ATS
title may normalize to a different dedup_key than the aggregator title did,
and an unpinned upsert would mint a duplicate row for the same job.

Non-destructive by design:
  - salary_min/max: first-seen wins (sent only when the row has neither),
    mirroring ats_scanner._upsert_one_ats_api_job; currency/period ride along
    only when a genuine new salary lands (enforced by the upsert SQL CASE).
  - posted_date: fills a NULL slot only.
  - score / score_breakdown: re-sent from the row — the upsert UPDATE branch
    overwrites them, so omitting them would zero the heuristic score.
  - source_id: separate guarded write — only when the row has none AND no
    other row holds (company_id, source_id) (I-11 partial unique index). A
    conflict means the ATS scanner already ingested this posting under a
    drifted title; it is logged as a retroactive-dedup candidate, never raised.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)


def _parse_posted_date(value: Any) -> datetime | None:
    """Parse a posting's posted_date (ISO string or datetime) — None on failure."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _safe_json_list(raw: Any) -> list:
    """Parse a JSON-array column value, tolerating NULL / junk."""
    if isinstance(raw, list):
        return raw
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    return parsed if isinstance(parsed, list) else []


def _safe_json_dict(raw: Any) -> dict:
    """Parse a JSON-object column value, tolerating NULL / junk."""
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def merge_primary_posting_fields(
    conn: sqlite3.Connection,
    job_row: dict,
    posting: dict,
    *,
    source_tag: str | None = None,
) -> bool:
    """Fold a strict-matched primary posting's fields into the existing row.

    Returns True when the row gained data (upsert kind 'updated' or 'touched'),
    False on a no-op or any failure. Never raises — enrichment must not abort
    because the bonus merge failed.

    source_tag, when given, rides along in the set-union ``sources`` list
    next to the posting's platform label — the resolver passes
    'primary_source_llm' for tie-breaker-upgraded merges so they remain
    auditable in the row itself (pitfall P13).
    """
    dedup_key = job_row.get("dedup_key")
    if not dedup_key or not posting:
        return False

    row = conn.execute(
        "SELECT title, company, company_id, location, salary_min, salary_max, "
        "posted_date, source_id, score, score_breakdown, unresolved_reasons "
        "FROM jobs WHERE dedup_key = ?",
        (dedup_key,),
    ).fetchone()
    if row is None:
        return False
    row = dict(row)

    # First-seen salary wins (mirrors _upsert_one_ats_api_job): only offer the
    # posting's salary when the row has neither bound.
    has_salary = row["salary_min"] is not None or row["salary_max"] is not None
    salary_min = None if has_salary else posting.get("salary_min")
    salary_max = None if has_salary else posting.get("salary_max")

    # posted_date: NULL-fill only — the upsert COALESCE lets a non-NULL
    # incoming value win, so suppress it when the row already has one.
    posted_date = None if row["posted_date"] else _parse_posted_date(posting.get("posted_date"))

    posting_url = posting.get("source_url") or posting.get("url")
    source_label = posting.get("company_source")

    try:
        from job_finder.db import upsert_job
        from job_finder.parsed_job import ParsedJob

        parsed = ParsedJob(
            title=row["title"],
            company=row["company"],
            dedup_key=dedup_key,
            # Fall back to the row's location so an empty incoming location
            # cannot regress the structured-locations derivation in upsert.
            location=posting.get("location") or row["location"] or "",
            locations_structured=posting.get("locations_structured") or [],
            sources=[s for s in (source_label, source_tag) if s],
            source_urls=[posting_url] if posting_url else [],
            salary_min=salary_min,
            salary_max=salary_max,
            salary_currency=posting.get("salary_currency") or "USD",
            salary_period=posting.get("salary_period") or "unknown",
            description=posting.get("description") or None,
            posted_date=posted_date,
            # A canonical change re-applies unresolved_reasons from the parsed
            # object; carry the row's existing flags through so this merge
            # cannot clear a pending /admin/review item.
            unresolved_reasons=_safe_json_list(row["unresolved_reasons"]),
        )
        result = upsert_job(
            conn,
            parsed,
            company_id=row["company_id"],
            score=row["score"] or 0.0,
            score_breakdown=_safe_json_dict(row["score_breakdown"]),
        )
    except Exception as exc:
        logger.warning("primary-posting merge failed for %s: %s", dedup_key, exc)
        return False

    # source_id rides separately — the upsert UPDATE branch never touches it.
    # set_source_id_if_free is the sanctioned single-writer (I-11 guarded).
    from job_finder.db._jobs import set_source_id_if_free

    set_source_id_if_free(conn, dedup_key, row["company_id"], posting.get("source_id"))
    return result.kind in ("updated", "touched")
