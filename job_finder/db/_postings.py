"""Posting sub-entity upsert logic for jobs.postings column.

This module provides the pure ``upsert_posting`` helper that manages the
``jobs.postings`` JSON array. Each posting descriptor is keyed by
``(ats_platform, source_id)`` — re-sighting an existing posting updates it
in place, while a new posting is appended.

The descriptor shape for Phase 1 (#640) is 6-field:
  - ats_platform: str (e.g. "ashby", "lever", "greenhouse")
  - source_id: str (the platform's posting ID)
  - apply_url: str (the canonical apply link)
  - locations_structured: list[dict] (JobLocation serialized via to_json)
  - workplace_type: str (REMOTE/HYBRID/ONSITE/UNSPECIFIED)
  - confidence: str (always "ats" for direct ATS sightings)

Phase 3 will add ``location_fit`` to this descriptor.
"""

from __future__ import annotations

from typing import Any


def upsert_posting(
    existing_postings: list[dict[str, Any]],
    descriptor: dict[str, Any],
) -> list[dict[str, Any]]:
    """Return a NEW list with the posting upserted by (ats_platform, source_id).

    Pure function — never mutates the input list. If a posting with the same
    ``(ats_platform, source_id)`` key exists, it is replaced in place. Otherwise,
    the descriptor is appended to the end of the list.

    Args:
        existing_postings: The current postings list (read from the row).
        descriptor: The new posting descriptor to upsert. Must contain at least
            ``ats_platform`` and ``source_id`` keys.

    Returns:
        A new list with the descriptor upserted. The input list is never mutated.
    """
    ats_platform = descriptor.get("ats_platform")
    source_id = descriptor.get("source_id")

    if not ats_platform or not source_id:
        # Invalid descriptor — return unchanged (should not happen in practice)
        return list(existing_postings)

    # Create a shallow copy of the list to avoid mutating the input
    new_postings = list(existing_postings)

    # Find and replace existing entry by key
    for i, posting in enumerate(new_postings):
        if posting.get("ats_platform") == ats_platform and posting.get("source_id") == source_id:
            # Replace in place
            new_postings[i] = descriptor
            return new_postings

    # Not found — append
    new_postings.append(descriptor)
    return new_postings


def build_posting_descriptor(
    ats_platform: str,
    source_id: str,
    apply_url: str,
    locations_structured: list[dict[str, Any]],
    workplace_type: str,
) -> dict[str, Any]:
    """Build a posting descriptor for Phase 1 (#640).

    The descriptor has exactly 6 fields — ``location_fit`` is added in Phase 3.

    Args:
        ats_platform: The platform key (e.g. "ashby", "lever", "greenhouse").
        source_id: The platform's posting ID.
        apply_url: The canonical apply link (empty string if unavailable).
        locations_structured: Serialized JobLocation list (via to_json).
        workplace_type: The workplace type (REMOTE/HYBRID/ONSITE/UNSPECIFIED).

    Returns:
        A dict with the 6-field descriptor shape.
    """
    return {
        "ats_platform": ats_platform,
        "source_id": source_id,
        "apply_url": apply_url,
        "locations_structured": locations_structured,
        "workplace_type": workplace_type,
        "confidence": "ats",
    }
