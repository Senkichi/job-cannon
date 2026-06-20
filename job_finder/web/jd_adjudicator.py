"""LLM adjudication of the AMBIGUOUS jd-content middle (PR2 of the jd-content contract).

The deterministic contract (``_jd_content_contract``) confidently CLEANs ~73% and
REJECTs ~6% of stored jd_full bodies. The remaining ~21% are AMBIGUOUS — a real JD
that lacks standard headings vs. a chrome / landing / listing page that happens to
mention the role. This module resolves that middle with a cheap local-LLM yes/no,
run by a BACKGROUND job (never on the startup re-sweep or the hot ingest path), so
the contract stays fast and deterministic wherever it can be.

  * ``adjudicate_jd``                -> bool | None   (one row; True = is the JD)
  * ``run_jd_adjudication_backfill`` -> dict          (bounded batch; scheduled entry)

A row the LLM (or the deterministic CLEAN check) vouches for is stamped with the
live ``JD_CONTENT_VERSION`` in ``jd_adjudicated_version`` so it is judged once per
contract version. A row the LLM rejects is cleared + re-queued for enrichment +
quarantined, exactly like a deterministic REJECT in the re-sweep.
"""

from __future__ import annotations

import json
import logging
import sqlite3

from job_finder.db import invalidate_job_score
from job_finder.db._jd_content_contract import (
    JD_CONTENT_VERSION,
    JD_OFFSITE,
    JdVerdict,
    classify_jd_content,
)
from job_finder.web.model_provider import call_model

logger = logging.getLogger(__name__)

#: Chars of the body shown to the judge — enough to decide, bounded for cost/latency.
_PROMPT_JD_CHARS = 3500

_ADJUDICATION_SCHEMA = {
    "type": "object",
    "properties": {
        "is_job_description": {"type": "boolean"},
        "confidence": {"type": "number"},
    },
    "required": ["is_job_description"],
    "additionalProperties": True,
}

_SYSTEM = (
    "You are a strict classifier deciding whether a block of text scraped from a "
    "web page IS the actual job posting / description for a SPECIFIC role at a "
    "SPECIFIC company.\n"
    "Answer is_job_description=true ONLY if the text describes that role's duties, "
    "responsibilities, requirements, or qualifications.\n"
    "Answer is_job_description=false if the text is a different page: a company "
    "About/marketing page, a job-listing index or search-results page, a login / "
    "blocked / captcha page, a cookie-consent notice, an unrelated article (e.g. a "
    "Wikipedia entry), a closed/expired-posting notice, or a posting for a "
    "DIFFERENT role.\n"
    "Respond with JSON only."
)


def adjudicate_jd(
    title: str | None,
    company: str | None,
    jd_full: str | None,
    conn: sqlite3.Connection,
    config: dict,
) -> bool | None:
    """Ask the quick-tier LLM whether *jd_full* is the posting for title@company.

    Returns True (is the JD), False (is not), or None when the call errors or the
    model returns nothing usable — the caller leaves a None row unstamped so it is
    retried on the next backfill pass.
    """
    if not jd_full:
        return None
    body = jd_full.strip()[:_PROMPT_JD_CHARS]
    user_msg = (
        f"TITLE: {title or '(unknown)'}\n"
        f"COMPANY: {company or '(unknown)'}\n"
        f"--- SCRAPED TEXT (first {_PROMPT_JD_CHARS} chars) ---\n{body}"
    )
    try:
        result = call_model(
            tier="quick",
            system=_SYSTEM,
            messages=[{"role": "user", "content": user_msg}],
            conn=conn,
            config=config,
            output_schema=_ADJUDICATION_SCHEMA,
            purpose="jd_content_adjudication",
            max_tokens=128,
        )
        data = result.data
    except Exception as exc:
        logger.warning("adjudicate_jd: call_model failed for %r: %s", (title or "")[:60], exc)
        return None
    if not isinstance(data, dict) or "is_job_description" not in data:
        return None
    return bool(data["is_job_description"])


def _stamp_adjudicated(conn: sqlite3.Connection, dedup_key: str) -> None:
    """Mark a row vouched-for at the current contract version (won't re-select)."""
    conn.execute(
        "UPDATE jobs SET jd_adjudicated_version = ? WHERE dedup_key = ?",
        (JD_CONTENT_VERSION, dedup_key),
    )


def _heal_offsite(
    conn: sqlite3.Connection, dedup_key: str, reason: str, classification: str | None
) -> None:
    """Clear + re-queue + quarantine a rejected row (mirrors the re-sweep heal).

    The jd_full / enrichment_tier / unresolved_reasons write touches no
    scoring-owned column, so it stays clear of the assessment-writer singleton.
    A previously-scored row is declassified through ``invalidate_job_score`` — the
    SOLE sanctioned scoring-tuple unsetter — which nulls the FULL LLM-scoring
    surface (classification, sub_scores_json, fit_analysis, scoring_model) in one
    trigger-safe statement. Clearing only the first three (as this did
    originally) leaves ``scoring_model`` set, tripping the m078 I-04/I-05 triggers
    (``RAISE(ABORT)``); with no try/except around the backfill loop that aborts
    the ENTIRE drain on the first scored reject. Same bug class + fix as the
    ``_post_hooks`` re-sweeps (PR #501).
    """
    row = conn.execute(
        "SELECT unresolved_reasons FROM jobs WHERE dedup_key = ?", (dedup_key,)
    ).fetchone()
    try:
        reasons = json.loads(row[0]) if row and row[0] else []
        if not isinstance(reasons, list):
            reasons = []
    except (TypeError, ValueError):
        reasons = []
    if reason not in reasons:
        reasons.append(reason)
    conn.execute(
        "UPDATE jobs SET jd_full = NULL, enrichment_tier = NULL, unresolved_reasons = ? "
        "WHERE dedup_key = ?",
        (json.dumps(reasons), dedup_key),
    )
    if classification is not None:
        invalidate_job_score(conn, dedup_key)


def run_jd_adjudication_backfill(
    conn: sqlite3.Connection, config: dict, *, limit: int = 200
) -> dict:
    """Adjudicate a bounded batch of AMBIGUOUS jd_full rows (the scheduled entry point).

    Selects rows with a present, non-quarantined jd_full not yet adjudicated at the
    live JD_CONTENT_VERSION (NULL watermark = never judged). Each is classified
    deterministically first, so only the genuinely AMBIGUOUS rows cost an LLM call:
      * CLEAN  -> stamp (vouched; won't re-select)
      * REJECT -> clear + re-queue (defensive; the re-sweep should have caught it)
      * AMBIGUOUS -> LLM: YES stamps, NO clears + re-queues, None leaves it to retry.

    Returns a summary dict (scanned / llm_calls / kept / rejected / undetermined).
    """
    rows = conn.execute(
        "SELECT dedup_key, title, company, jd_full, classification FROM jobs "
        "WHERE jd_full IS NOT NULL AND TRIM(jd_full) != '' "
        "AND COALESCE(unresolved_reasons, '[]') = '[]' "
        "AND (jd_adjudicated_version IS NULL OR jd_adjudicated_version < ?) "
        "ORDER BY (classification IS NOT NULL) DESC, first_seen DESC "
        "LIMIT ?",
        (JD_CONTENT_VERSION, limit),
    ).fetchall()

    scanned = llm_calls = kept = rejected = undetermined = 0
    for dk, title, company, jd_full, classification in rows:
        scanned += 1
        verdict = classify_jd_content(jd_full, title, company)
        if verdict.verdict is JdVerdict.REJECT:
            _heal_offsite(conn, dk, verdict.reason or JD_OFFSITE, classification)
            rejected += 1
            continue
        if verdict.verdict is JdVerdict.CLEAN:
            _stamp_adjudicated(conn, dk)
            kept += 1
            continue
        # AMBIGUOUS -> the LLM tie-breaker (the only path that costs a call).
        llm_calls += 1
        decision = adjudicate_jd(title, company, jd_full, conn, config)
        if decision is None:
            undetermined += 1
            continue  # leave unstamped -> retried next pass
        if decision:
            _stamp_adjudicated(conn, dk)
            kept += 1
        else:
            _heal_offsite(conn, dk, JD_OFFSITE, classification)
            rejected += 1

    conn.commit()
    logger.info(
        "jd adjudication backfill: scanned=%d llm=%d kept=%d rejected=%d undetermined=%d",
        scanned,
        llm_calls,
        kept,
        rejected,
        undetermined,
    )
    return {
        "scanned": scanned,
        "llm_calls": llm_calls,
        "kept": kept,
        "rejected": rejected,
        "undetermined": undetermined,
    }
