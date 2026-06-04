"""Migration 79 — clean HTML-polluted jd_full rows (JD Layer 2 step 2c).

Background: before the JD Layer 2 scanner fixes, Greenhouse stored its
``content`` field (entity-escaped HTML, ``&lt;p&gt;…``) verbatim as the job
description, which was then auto-promoted to ``jd_full``. The scorer therefore
received raw HTML for those rows. The 2026-06-03 investigation measured ~20%
of rows carrying tag/entity bloat in ``jd_full``.

This migration heals existing rows by running the same lossless converter the
scanners now use at ingest (``description_formatter.html_to_plain_text``:
unescape entities → strip tags while preserving section/list structure). It
drops NO content — only markup.

Scope (conservative):
  - Targets only rows whose ``jd_full`` shows a clear HTML signal: an escaped
    tag (``&lt;``), a closing tag (``</``), or a common opening block tag. Plain
    prose that merely contains a stray ``<`` is not matched.
  - Updates only when the cleaned text is non-empty AND actually differs, so a
    row is never blanked.

Re-running is safe: a cleaned row no longer contains tag/entity markup, so it
falls out of the candidate filter — subsequent runs are no-ops.
"""

from __future__ import annotations

import logging
import sqlite3

from job_finder.web.description_formatter import html_to_plain_text
from job_finder.web.migrations.types import Migration, MigrationContext

logger = logging.getLogger(__name__)

# Clear HTML signals. Escaped tags (&lt;) and closing tags (</) are the
# strongest; the opening-block-tag patterns catch real (unescaped) HTML that
# slipped in via other paths. Deliberately NOT a bare '%<%>%' — that would
# match prose like "earn < $100k > target" and strip it.
_HTML_SIGNAL_SQL = (
    "jd_full IS NOT NULL AND ("
    "jd_full LIKE '%&lt;%' "
    "OR jd_full LIKE '%</%' "
    "OR jd_full LIKE '%<p>%' "
    "OR jd_full LIKE '%<p %' "
    "OR jd_full LIKE '%<div%' "
    "OR jd_full LIKE '%<br%' "
    "OR jd_full LIKE '%<li%' "
    "OR jd_full LIKE '%<ul%' "
    "OR jd_full LIKE '%<h1%' "
    "OR jd_full LIKE '%<h2%' "
    "OR jd_full LIKE '%<h3%'"
    ")"
)


def _clean(ctx: MigrationContext) -> None:
    conn: sqlite3.Connection = ctx.conn

    if (
        conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name = 'jobs'").fetchone()
        is None
    ):
        logger.info("m079: jobs table not present, no-op")
        return

    # _HTML_SIGNAL_SQL is a module constant (no user data interpolated).
    candidates = conn.execute(
        f"SELECT dedup_key, jd_full FROM jobs WHERE {_HTML_SIGNAL_SQL}"
    ).fetchall()

    cleaned_count = 0
    for dedup_key, jd_full in candidates:
        cleaned = html_to_plain_text(jd_full)
        # Never blank a row; only write a genuine change.
        if cleaned and cleaned != jd_full:
            conn.execute(
                "UPDATE jobs SET jd_full = ? WHERE dedup_key = ?",
                (cleaned, dedup_key),
            )
            cleaned_count += 1

    logger.info(
        "m079: cleaned HTML markup from %d of %d candidate jd_full row(s)",
        cleaned_count,
        len(candidates),
    )


MIGRATION = Migration(
    version=79,
    description="clean HTML-polluted jd_full rows via lossless html_to_plain_text",
    py=_clean,
)
