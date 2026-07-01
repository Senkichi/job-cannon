"""Sanctioned single-writer funnel for the canonical location columns.

Design rules this module enforces (cite by ID — see issue #393 / #386):

- **D-5 (Single writer for canonical location).** ``locations_structured`` is
  the canonical location. ``location`` (display string) and
  ``workplace_type`` / ``primary_country_code`` (denormalized filter columns)
  are *derived* from it in exactly one code path. No module outside that path
  writes any location column. Enrichment contributes *observations* through the
  same funnel rather than side-door-writing the ``location`` column (the S4
  wipe: an enrichment-written ``location`` with an empty ``locations_raw`` was
  reverted to ``''`` the next time the crawler re-sighted the job, because the
  upsert UPDATE branch rebuilds ``location`` from ``locations_raw``).

The five canonical location columns this funnel owns are written together,
atomically, so a re-sighting can never erase a subset of them:

    locations_raw, locations_structured, location, workplace_type,
    primary_country_code

This mirrors the existing single-writer patterns in the codebase:
``set_jd_full`` is the sole sanctioned ``jd_full`` writer (CI grep-gated by
``tests/test_jd_full_writers_routed.py``); the assessment writer is the sole
scoring-column writer. ``apply_location_observation`` is the analogous funnel
for location, grep-gated by ``tests/test_location_writers_routed.py``.

Exports
-------
merge_locations_raw(existing, incoming) -> list[str]
    Pure helper: Remote/Hybrid-first set-union of two raw-location lists.
    Shared by ``upsert_job`` (UPDATE branch) and ``apply_location_observation``
    so the merge semantics are defined in exactly one place.

merge_locations_structured(existing, incoming) -> list[JobLocation]
    Pure helper: Union by ``(country_code, region_code, city, workplace_type)``.
    Shared by ``upsert_job`` (UPDATE branch) so the merge semantics are defined
    in exactly one place. Mirrors the design of ``merge_locations_raw``.

apply_location_observation(conn, dedup_key, raw_location, *, source) -> bool
    The single funnel. Merges one observed location string into a job's
    canonical location columns and rewrites all five together in one UPDATE.
    Idempotent, never raises on parse failure.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3

from job_finder.web.location_canonical import (
    JobLocation,
    dedupe_locations,
)
from job_finder.web.location_canonical import (
    to_json as _locations_to_json,
)

_logger = logging.getLogger(__name__)

# Remote/Hybrid raw entries float to the front of locations_raw so the merged
# display string and the dropdown lead with the workplace signal. Mirrors the
# historical inline behavior in upsert_job's UPDATE branch.
_REMOTE_HYBRID_RE = re.compile(r"\b(remote|hybrid)\b", re.IGNORECASE)


def merge_locations_raw(existing: list[str], incoming: list[str]) -> list[str]:
    """Remote/Hybrid-first set-union of two raw-location lists (pure).

    Single source of truth for the ``locations_raw`` merge semantics shared by
    ``upsert_job`` and ``apply_location_observation`` (D-5). Case-insensitive
    de-dup against the existing list; first-seen casing is preserved. A new
    entry containing a standalone ``remote`` / ``hybrid`` token is inserted at
    the front (so the workplace signal leads the display join); everything else
    is appended in arrival order.

    Args:
        existing: The raw-location list already stored on the row.
        incoming: Newly observed raw-location strings to merge in.

    Returns:
        A new list — neither input is mutated (immutability).
    """
    merged: list[str] = [loc for loc in existing if loc]
    seen_keys = {loc.lower() for loc in merged}
    for normalized in incoming:
        if not normalized:
            continue
        key = normalized.lower()
        if key in seen_keys:
            continue
        seen_keys.add(key)
        if _REMOTE_HYBRID_RE.search(normalized):
            merged.insert(0, normalized)
        else:
            merged.append(normalized)
    return merged


def merge_locations_structured(
    existing: list[JobLocation], incoming: list[JobLocation]
) -> list[JobLocation]:
    """Union by ``(country_code, region_code, city, workplace_type)`` (pure).

    Single source of truth for the ``locations_structured`` merge semantics
    shared by ``upsert_job`` (UPDATE branch). Mirrors the design of
    ``merge_locations_raw`` — pure, immutable, single source of truth.

    Deduplication uses the existing ``dedupe_locations`` helper from
    ``location_canonical`` so the semantics are defined in exactly one place.
    The dedup key is ``(country_code, region_code, city, workplace_type)`` —
    two locations that differ only in ``raw`` collapse to the first occurrence.

    Preserves first-seen order: existing entries first (in their stored order),
    then genuinely-new incoming entries appended in arrival order.

    Args:
        existing: The structured-location list already stored on the row.
        incoming: Newly observed structured locations to merge in.

    Returns:
        A new list — neither input is mutated (immutability).
    """
    # Concatenate existing + incoming, then dedupe via the canonical helper.
    # dedupe_locations preserves first-seen order, so existing entries stay
    # at the front and new entries are appended in arrival order.
    combined = existing + incoming
    return dedupe_locations(combined)


def apply_location_observation(
    conn: sqlite3.Connection,
    dedup_key: str,
    raw_location: str,
    *,
    source: str,
) -> bool:
    """Merge a location observation into a job's canonical location columns.

    The single sanctioned write path for the five canonical location columns
    outside ``upsert_job`` (D-5). Pipeline:

        normalize/split incoming string -> split_multi_locations
        -> merge into locations_raw (Remote/Hybrid first, via merge_locations_raw)
        -> parse_locations(merged_raw, jd_full=row.jd_full) -> rewrite
           locations_structured, location (derived join), workplace_type,
           primary_country_code in ONE UPDATE.

    All five columns move together — the invariant that kills the S4 wipe class
    (an enrichment-written ``location`` with empty ``locations_raw`` survives a
    subsequent crawler re-sighting because ``locations_raw`` now carries it too).

    Idempotent: re-applying the same observation is a no-op (the
    case-insensitive de-dup in ``merge_locations_raw`` plus the no-change guard
    below). Never raises on parse failure — logs at WARNING and returns False.

    Args:
        conn: Open sqlite3 connection.
        dedup_key: The job's primary key.
        raw_location: An observed location string (e.g. an LLM-extracted city).
        source: Provenance tag for logging (e.g. ``"llm_extract"``).

    Returns:
        True when at least one canonical location column changed; False on a
        no-op (idempotent re-apply, missing row, empty/unparseable input, or
        parse failure).
    """
    if not dedup_key or not raw_location or not raw_location.strip():
        return False

    # Lazy imports — keep db/ free of a module-load-time db/ -> web/ cycle
    # (same pattern as _jd_full.normalize_jd's deferred import).
    from job_finder.web.location_normalizer import split_multi_locations
    from job_finder.web.location_parser import parse_locations

    try:
        row = conn.execute(
            "SELECT locations_raw, jd_full FROM jobs WHERE dedup_key = ?",
            (dedup_key,),
        ).fetchone()
    except sqlite3.Error as exc:
        # Contract: the funnel never raises — a side-door write must not abort
        # the surrounding enrichment persist (mirrors set_jd_full's soft-fail).
        _logger.warning(
            "apply_location_observation: read failed [source=%s key=%s]: %s",
            source,
            dedup_key,
            exc,
        )
        return False
    if row is None:
        return False

    try:
        existing_raw = json.loads(row["locations_raw"]) if row["locations_raw"] else []
    except (json.JSONDecodeError, TypeError):
        existing_raw = []
    if not isinstance(existing_raw, list):
        existing_raw = [existing_raw] if existing_raw else []
    existing_raw = [loc for loc in existing_raw if loc]

    incoming_raw = split_multi_locations(raw_location)
    if not incoming_raw:
        return False

    merged_raw = merge_locations_raw(existing_raw, incoming_raw)
    if merged_raw == existing_raw:
        # No new raw segment — idempotent re-apply. Nothing to rewrite.
        return False

    try:
        # jd_full is passed as the workplace-type fallback source (#LI-Remote /
        # #LI-Hybrid / #LI-Onsite body hashtags) — same proxy upsert_job uses.
        structured = parse_locations(merged_raw, jd_full=row["jd_full"])
    except Exception as exc:  # funnel must never raise (contract)
        _logger.warning(
            "apply_location_observation: parse failed [source=%s key=%s]: %s",
            source,
            dedup_key,
            exc,
        )
        return False

    locations_json = _locations_to_json(structured) if structured else None
    location_col = ", ".join(dict.fromkeys(merged_raw))
    workplace_type = structured[0].workplace_type if structured else "UNSPECIFIED"
    primary_country_code = structured[0].country_code if structured else None

    # All five columns rewritten together (D-5). workplace_type uses the same
    # COALESCE/NULLIF guard as upsert_job so an UNSPECIFIED observation never
    # downgrades a previously-determined workplace type.
    try:
        conn.execute(
            """UPDATE jobs SET
                locations_raw = ?,
                location = ?,
                locations_structured = ?,
                workplace_type = COALESCE(NULLIF(?, 'UNSPECIFIED'), workplace_type, 'UNSPECIFIED'),
                primary_country_code = COALESCE(?, primary_country_code)
            WHERE dedup_key = ?""",
            (
                json.dumps(merged_raw),
                location_col,
                locations_json,
                workplace_type,
                primary_country_code,
                dedup_key,
            ),
        )
        conn.commit()
    except sqlite3.Error as exc:
        # Contract: the funnel never raises. A trigger rejection or transient DB
        # error rolls back the location write but leaves the caller's other
        # persists (jd_full, salary, enrichment_tier) intact.
        conn.rollback()
        _logger.warning(
            "apply_location_observation: write failed [source=%s key=%s]: %s",
            source,
            dedup_key,
            exc,
        )
        return False
    _logger.info(
        "apply_location_observation: merged %r [source=%s key=%s] -> %d raw segments",
        raw_location,
        source,
        dedup_key,
        len(merged_raw),
    )
    return True
