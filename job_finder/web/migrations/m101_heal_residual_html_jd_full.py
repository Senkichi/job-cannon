"""Migration 101 — heal residual HTML-polluted jd_full rows (post-F2).

Background: m079 (PR #273) ran a one-time pass that cleaned HTML-bloated
``jd_full`` via the lossless ``html_to_plain_text`` converter, and F2 (also
#273) added the ``normalize_jd`` boundary so every *new* jd_full write is
cleaned at the door. A 2026-06-10 read-only scan of the live DB still found
**335 / 13,305 rows (2.5%)** carrying HTML markup in ``jd_full`` — residue
that predates the F2 boundary or arrived via a write path m079 missed. 213 of
those rows sit on the user-facing actionable lists (``apply`` / ``consider``),
so the scorer read raw markup for them.

This migration is the optional heal the #262 triage approved: re-clean those
residual rows **through the same F2 boundary cleaner** the live write path now
uses (``job_finder.db._jd_full.normalize_jd``) so healed rows are byte-identical
to what the boundary would produce today. Single source of truth — it does NOT
hand-roll an HTML stripper.

Scope (conservative, mirrors m079):
  - Candidate predicate is m079's ``_HTML_SIGNAL_SQL``: an escaped tag
    (``&lt;``), a closing tag (``</``), or a common opening block tag. Plain
    prose with a stray ``<`` is not matched.
  - ``normalize_jd`` is itself gated on the same HTML signal, so it is a no-op
    for any candidate that the cheap SQL filter false-positives on.
  - Writes only when the cleaned text is non-empty AND actually differs — a row
    is never blanked.

Idempotent on both empty and populated DBs: a cleaned row no longer carries
tag/entity markup, so it drops out of the candidate filter and subsequent runs
are no-ops. No-op if the ``jobs`` table is absent (fresh install).
"""

from __future__ import annotations

import logging
import sqlite3

from job_finder.db._jd_full import normalize_jd
from job_finder.web.migrations.types import Migration, MigrationContext

logger = logging.getLogger(__name__)

# Same candidate predicate as m079 (_HTML_SIGNAL_SQL). Module constant; no user
# data interpolated. Deliberately NOT a bare '%<%>%' — that would strip prose
# like "earn < $100k > target".
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


def _heal(ctx: MigrationContext) -> None:
    conn: sqlite3.Connection = ctx.conn

    if (
        conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name = 'jobs'").fetchone()
        is None
    ):
        logger.info("m101: jobs table not present, no-op")
        return

    # _HTML_SIGNAL_SQL is a module constant (no user data interpolated).
    candidates = conn.execute(
        f"SELECT dedup_key, jd_full FROM jobs WHERE {_HTML_SIGNAL_SQL}"
    ).fetchall()

    healed_count = 0
    for dedup_key, jd_full in candidates:
        # normalize_jd is the F2 boundary cleaner — the SAME function every live
        # jd_full write routes through — so healed rows match the boundary's
        # output exactly. It is itself HTML-signal gated, so clean text passes
        # through unchanged.
        cleaned = normalize_jd(jd_full)
        # Never blank a row; only write a genuine change.
        if cleaned and cleaned != jd_full:
            conn.execute(
                "UPDATE jobs SET jd_full = ? WHERE dedup_key = ?",
                (cleaned, dedup_key),
            )
            healed_count += 1

    logger.info(
        "m101: healed residual HTML markup from %d of %d candidate jd_full row(s)",
        healed_count,
        len(candidates),
    )


MIGRATION = Migration(
    version=101,
    description="heal residual HTML-polluted jd_full rows via the F2 normalize_jd boundary cleaner",
    py=_heal,
)
