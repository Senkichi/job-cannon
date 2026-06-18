"""Sanctioned jd_full write path with content-density gate (I-13).

Single source of truth for the I-13 junk-detection logic; mirrors the
``tg_jobs_jd_full_junk`` DB trigger introduced in m078.

Exports
-------
_JD_JUNK_PREFIXES, _MIN_JD_LENGTH
    Constants — single source of truth imported by m078 trigger builder
    and ``scripts/pre_m078_remediation.py``.

_is_jd_junk(text)
    Pure boolean gate check.  Imported by ``parsed_job.py`` (Phase 46.03)
    and by ``_jobs.py``'s upsert helpers so the same logic applies to every
    jd_full write without duplicating constants.

normalize_jd(text) -> str
    Lossless HTML → plain-text normalization applied to every jd_full write.
    HTML-signal text is passed through ``description_formatter.html_to_plain_text``;
    already-plain text passes through unchanged (idempotent).

build_jd_junk_trigger_sql(col) -> str
    Build the SQL boolean expression for the I-13 trigger WHEN clause from
    the canonical constants. Imported by ``m078_contract_invariants`` so the
    trigger definition stays in sync with the Python gate.

set_jd_full(conn, dedup_key, text, *, source) -> bool
    Gated jd_full DB writer — the ONLY sanctioned UPDATE path for
    ``jobs.jd_full`` outside of the INSERT branch in ``upsert_job``.
    All direct writers in ``job_finder/web/`` must route through here.
"""

from __future__ import annotations

import logging
import re
import sqlite3

logger = logging.getLogger(__name__)

_MIN_JD_LENGTH: int = 200  # characters, post-strip

# Shell / auth-wall prefix patterns mirroring the tg_jobs_jd_full_junk trigger
# (m078).  Applied case-insensitively to the first 200 stripped chars.
# SINGLE SOURCE OF TRUTH — imported by m078_contract_invariants and
# pre_m078_remediation; do NOT duplicate these values elsewhere.
_JD_JUNK_PREFIXES: tuple[str, ...] = (
    "sign in",
    "loading",
    "open roles at",
    "skip to content",
    "cookie",
    "privacy policy",
    "404",
)

# HTML-signal regex: detects escaped tags (&lt;), closing tags (</…>), or
# common opening block tags.  Mirrors the _HTML_SIGNAL_SQL predicate in m079.
# Plain prose that merely contains a stray `<` (e.g. "earn < $100k") is not
# matched because we require a word char or `/` immediately after the `<`.
_HTML_SIGNAL_RE = re.compile(
    r"(&lt;|</([\w]+)>|<p[\s>]|<div|<br|<li|<ul|<h[1-6])",
    re.IGNORECASE,
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


def normalize_jd(text: str) -> str:
    """Lossless HTML → plain-text normalization for jd_full writes.

    If ``text`` contains an HTML signal (escaped tag, closing tag, or a common
    opening block tag), it is passed through
    ``description_formatter.html_to_plain_text`` — the same lossless converter
    used by m079's heal pass.  Plain-text input passes through unchanged
    (idempotent).

    The import of ``description_formatter`` is deferred inside the function to
    avoid a ``db/ → web/`` module-load-time import cycle.  The call is cheap on
    the hot path: ``_HTML_SIGNAL_RE.search`` short-circuits for already-clean
    text so the formatter is only invoked when HTML is actually detected.
    """
    if not text:
        return text
    if not _HTML_SIGNAL_RE.search(text):
        return text
    # Lazy import — keeps db/ independent of web/ at module load time.
    from job_finder.web.description_formatter import html_to_plain_text

    return html_to_plain_text(text)


def build_jd_junk_trigger_sql(col: str) -> str:
    """Return the SQL boolean expression for the I-13 junk trigger WHEN clause.

    ``col`` is the column reference (``NEW.jd_full`` in a trigger context,
    ``jd_full`` in a preflight SELECT).  The expression is true when the
    trimmed, lowercased first 200 chars start with a junk prefix OR the trimmed
    length falls below ``_MIN_JD_LENGTH``.

    Imported by ``m078_contract_invariants._jd_junk_condition`` so the trigger
    DDL is always generated from the same constants as the Python gate.
    """
    likes = "\n            OR ".join(
        f"LOWER(SUBSTR(TRIM({col}), 1, 200)) LIKE '{p}%'" for p in _JD_JUNK_PREFIXES
    )
    return (
        f"{col} IS NOT NULL AND (\n"
        f"            {likes}\n"
        f"            OR LENGTH(TRIM({col})) < {_MIN_JD_LENGTH}\n"
        f"        )"
    )


def set_jd_full(
    conn: sqlite3.Connection,
    dedup_key: str,
    text: str | None,
    *,
    source: str,
) -> bool:
    """Write ``jd_full`` to the DB after normalizing and passing the content-density gate.

    Applies ``normalize_jd`` first (lossless HTML → plain-text conversion) so
    that HTML-bloated text is cleaned before the junk gate and the DB write.
    Already-plain text passes through unchanged (idempotent).

    Returns True if ``jd_full`` was written; False if junk-gated (no write).
    On gate hit, logs at WARN level with the source tag and first 60 chars.

    Score invalidation (#226): when the written text materially differs from
    what was stored (the common case being a NULL→full-body transition during
    enrichment, but also a genuinely changed body on re-fetch), the job's prior
    scoring tuple is stale. As the SOLE sanctioned ``jd_full`` writer, this is the
    single point where that invariant is enforced: ``invalidate_job_score`` clears
    ``classification`` (and the rest of the scoring tuple) so the existing
    Stage-2 sweeps — which select ``classification IS NULL AND jd_full IS NOT
    NULL`` — re-queue the row. A write that does not change the stored text
    (idempotent re-fetch) leaves the score untouched, so trivial re-sightings
    never churn the scorer.

    The caller is responsible for any side effects (e.g. ``enrichment_tier``
    updates); this helper ONLY handles the ``jd_full`` write + score invalidation.
    """
    if not text:
        return False
    text = normalize_jd(text)
    if _is_jd_junk(text):
        logger.warning(
            "set_jd_full: junk-gated [source=%s] prefix=%r",
            source,
            text.strip()[:60],
        )
        return False
    existing_row = conn.execute(
        "SELECT jd_full FROM jobs WHERE dedup_key = ?",
        (dedup_key,),
    ).fetchone()
    existing_jd = existing_row[0] if existing_row is not None else None
    content_changed = text != existing_jd
    conn.execute(
        "UPDATE jobs SET jd_full = ? WHERE dedup_key = ?",
        (text, dedup_key),
    )
    conn.commit()
    if content_changed:
        # Lazy import — keeps this leaf module free of an import-time dependency
        # on the assessment writer (both live in db/, no cycle, but the helper
        # is only needed on the change path).
        from ._assessment_writer import invalidate_job_score

        invalidate_job_score(conn, dedup_key)
    return True
