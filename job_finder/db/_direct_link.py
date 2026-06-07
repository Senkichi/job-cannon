"""Sanctioned direct_url write path with no-downgrade precedence.

set_direct_url is the ONLY writer for jobs.direct_url / direct_url_confidence.
Confidence precedence (highest wins, ties do not overwrite):
    strict  — overwrites a NULL or an existing 'loose' link (upgrade); never
              overwrites an existing 'strict' link (stable).
    loose   — fills a NULL slot only; never overwrites any existing link.

Empty URL or a confidence outside {'strict','loose'} is a no-op.
"""

from __future__ import annotations

import logging
import sqlite3

logger = logging.getLogger(__name__)

_VALID_CONFIDENCE = ("strict", "loose")


def set_direct_url(
    conn: sqlite3.Connection,
    dedup_key: str,
    url: str | None,
    confidence: str,
) -> bool:
    """Write the direct company-posting link if precedence permits.

    Returns True if a write happened, False otherwise (gated, missing row,
    or invalid input). Commits on write.
    """
    if not url or confidence not in _VALID_CONFIDENCE:
        return False

    row = conn.execute(
        "SELECT direct_url_confidence FROM jobs WHERE dedup_key = ?",
        (dedup_key,),
    ).fetchone()
    if row is None:
        return False

    existing = row[0]
    if existing is not None:
        if confidence == "loose":
            return False  # never overwrite an existing link with a loose one
        if existing == "strict":
            return False  # strict slot is stable

    conn.execute(
        "UPDATE jobs SET direct_url = ?, direct_url_confidence = ? WHERE dedup_key = ?",
        (url, confidence, dedup_key),
    )
    conn.commit()
    return True
