"""Sanctioned jd_full write path with content-density gate (I-13).

Single source of truth for the I-13 junk-detection logic; mirrors the
``tg_jobs_jd_full_junk`` DB trigger introduced in m078.

Exports
-------
_is_jd_junk(text)
    Pure boolean gate check.  Imported by ``parsed_job.py`` (Phase 46.03)
    and by ``_jobs.py``'s upsert helpers so the same logic applies to every
    jd_full write without duplicating constants.

set_jd_full(conn, dedup_key, text, *, source) -> bool
    Gated jd_full DB writer — the ONLY sanctioned UPDATE path for
    ``jobs.jd_full`` outside of the INSERT branch in ``upsert_job``.
    All direct writers in ``job_finder/web/`` must route through here.
"""

from __future__ import annotations

import logging
import sqlite3

logger = logging.getLogger(__name__)

_MIN_JD_LENGTH: int = 200  # characters, post-strip

# Shell / auth-wall prefix patterns mirroring the tg_jobs_jd_full_junk trigger
# (m078).  Applied case-insensitively to the first 200 stripped chars.
_JD_JUNK_PREFIXES: tuple[str, ...] = (
    "sign in",
    "loading",
    "open roles at",
    "skip to content",
    "cookie",
    "privacy policy",
    "404",
)


def _is_jd_junk(text: str) -> bool:
    """Return True if jd_full content fails the I-13 density gate.

    Two failure modes:
    - Text shorter than ``_MIN_JD_LENGTH`` after stripping whitespace.
    - Text whose first 200 chars (lowercased) start with a junk prefix.
    """
    stripped = text.strip()
    if len(stripped) < _MIN_JD_LENGTH:
        return True
    prefix = stripped[:200].lower()
    return any(prefix.startswith(p) for p in _JD_JUNK_PREFIXES)


def set_jd_full(
    conn: sqlite3.Connection,
    dedup_key: str,
    text: str | None,
    *,
    source: str,
) -> bool:
    """Write ``jd_full`` to the DB after passing the content-density gate.

    Returns True if ``jd_full`` was written; False if junk-gated (no write).
    On gate hit, logs at WARN level with the source tag and first 60 chars.

    The caller is responsible for any side effects (e.g. ``enrichment_tier``
    updates); this helper ONLY handles the ``jd_full`` write.
    """
    if not text:
        return False
    if _is_jd_junk(text):
        logger.warning(
            "set_jd_full: junk-gated [source=%s] prefix=%r",
            source,
            text.strip()[:60],
        )
        return False
    conn.execute(
        "UPDATE jobs SET jd_full = ? WHERE dedup_key = ?",
        (text, dedup_key),
    )
    conn.commit()
    return True
