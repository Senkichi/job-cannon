"""Sanctioned direct_url write paths.

set_direct_url is the ONLY writer for jobs.direct_url / direct_url_confidence,
with no-downgrade precedence (highest wins, ties do not overwrite):
    strict  — overwrites a NULL or an existing 'loose' link (upgrade); never
              overwrites an existing 'strict' link (stable).
    loose   — fills a NULL slot only; never overwrites any existing link.

Empty URL or a confidence outside {'strict','loose'} is a no-op.

stamp_direct_url_checks is the ONLY writer for the m092 resolution-state
columns (direct_url_checked_at / direct_url_attempts). Attempts are owned
exclusively by the scheduled resolver — one increment per board-match attempt.
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


def stamp_direct_url_checks(
    conn: sqlite3.Connection,
    dedup_keys: list[str],
    now_iso: str,
) -> None:
    """Record one resolution attempt for each given job (single writer, m092).

    Sets direct_url_checked_at and increments direct_url_attempts. Called by
    the primary-source resolver after a board-match attempt — whether or not
    the job resolved (a resolved row leaves the candidate pool via its
    non-NULL direct_url, so the attempt count is only consulted for misses).
    Commits once for the batch.
    """
    if not dedup_keys:
        return
    conn.executemany(
        "UPDATE jobs SET direct_url_checked_at = ?, "
        "direct_url_attempts = COALESCE(direct_url_attempts, 0) + 1 "
        "WHERE dedup_key = ?",
        [(now_iso, key) for key in dedup_keys],
    )
    conn.commit()
