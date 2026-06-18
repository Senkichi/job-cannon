"""Sole sanctioned writer of the scoring tuple (Phase 49.04).

``persist_job_assessment`` is the ONLY code path permitted to write the
scoring-owned columns ``(classification, sub_scores_json, fit_analysis,
scoring_provider, scoring_model)``. This is enforced two ways:

  - the CI grep gate ``tests/test_assessment_writer_singleton.py`` fails if any
    ``UPDATE jobs SET <scoring column>`` appears outside this module (and
    ``migrations/``);
  - the m078 I-05 trigger raises if any path writes ``scoring_model`` without a
    ``classification`` (DB-level backstop).

Classification is ALWAYS derived here at persist time via
``derive_classification`` (D-06 / D-17 / anti-pattern 3) — never taken from the
LLM-emitted assessment. ``legitimacy_note`` / ``enrichment_tier`` /
``LENGTH(jd_full)`` are read from the existing row so the rule sees authoritative
inputs.

Extracted from ``_persistence.py`` in Phase 49.04; re-exported there for
back-compat.
"""

from __future__ import annotations

import json
import sqlite3

from ._classification import (
    _SUB_SCORE_KEYS,
    DEFAULT_APPLY_MEAN_FLOOR,
    DEFAULT_APPLY_MIN_STRONG_AXES,
    JobAssessment,
    derive_classification,
)


def persist_job_assessment(
    conn: sqlite3.Connection,
    dedup_key: str,
    assessment: JobAssessment,
    provider: str | None = None,
    model: str | None = None,
    *,
    config: dict | None = None,
) -> str | None:
    """Persist a v3.0 JobAssessment. Replaces persist_haiku_score + persist_sonnet_score.

    Writes classification (derived at persist time), sub_scores_json (JSON),
    fit_analysis (rationale payload — D-08 reuse), scoring_provider, scoring_model.
    Plan 5 (Migration 41) dropped the legacy haiku_score/haiku_summary/sonnet_score
    columns; this function now writes only the v3.0 surface.

    legitimacy_note sourcing (CONTEXT D-07): read from the existing jobs row,
    NOT from the assessment. derive_classification uses this value to compute
    the authoritative classification — any classification field on the passed
    assessment is ignored (anti-pattern 3 defense).

    Phase 2d sub-fix 2-3/4: also reads enrichment_tier and LENGTH(jd_full) from
    the row so derive_classification can compute the low_signal verdict. The
    threshold (default 1500 chars) is sourced from config.scoring.low_signal_jd_chars
    when config is provided; callers that pass config=None get the default,
    preserving back-compat with direct test/script invocations.

    No-op on missing dedup_key (SQLite UPDATE with no matching row is a silent
    no-op; we also short-circuit before the UPDATE to avoid COALESCE no-ops).

    Args:
        conn: Open sqlite3 connection.
        dedup_key: The job's primary key.
        assessment: JobAssessment with sub_scores + rationale.
        provider: Cascade-attribution string; None preserves the existing value.
        model: Model identifier (e.g., "qwen2.5:14b"); None preserves existing.
        config: Optional application config dict. When provided, reads
            scoring.low_signal_jd_chars to set the low_signal threshold and
            scoring.apply_mean_floor / scoring.apply_min_strong_axes to set the
            positive-evidence "apply" thresholds (issue #210); otherwise the
            module defaults are used.

    Returns:
        The Python-derived ``final_classification`` string just written to the
        row, or ``None`` when the dedup_key did not match any row (silent
        no-op path). Lets callers observe the verdict that landed on disk
        without a redundant re-SELECT — used by the orchestrator's per-job
        ``run_events`` ``score`` emission (issue #215). Existing callers that
        ignore the return are unaffected.

    Raises:
        ValueError: Propagated from ``derive_classification`` when
            ``assessment.sub_scores`` is malformed (wrong/missing/extra keys,
            or values not int-in-1..5). Production input is schema-guaranteed
            valid by the cascade dispatcher, so this is unreachable on the hot
            path; it surfaces in tests, the redrive script, and any future
            caller that passes a raw dict.
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT legitimacy_note, enrichment_tier, COALESCE(LENGTH(jd_full), 0) AS jd_len "
        "FROM jobs WHERE dedup_key = ?",
        (dedup_key,),
    )
    row = cur.fetchone()
    if row is None:
        # Silent no-op matches SQLite UPDATE-no-match semantics.
        return None
    legitimacy_note, enrichment_tier, jd_full_length = row[0], row[1], row[2] or 0

    # Resolve low_signal threshold from config (Phase 2d sub-fix 3/4). Default
    # 1500 chars matches scoring.low_signal_jd_chars.example. None config keeps
    # backwards compatibility for tests/scripts that call directly.
    threshold = 1500
    apply_mean_floor = DEFAULT_APPLY_MEAN_FLOOR
    apply_min_strong_axes = DEFAULT_APPLY_MIN_STRONG_AXES
    if config is not None:
        scoring_cfg = config.get("scoring") or {}
        threshold = int(scoring_cfg.get("low_signal_jd_chars", 1500))
        apply_mean_floor = float(scoring_cfg.get("apply_mean_floor", DEFAULT_APPLY_MEAN_FLOOR))
        apply_min_strong_axes = int(
            scoring_cfg.get("apply_min_strong_axes", DEFAULT_APPLY_MIN_STRONG_AXES)
        )

    final_classification = derive_classification(
        assessment.sub_scores,
        legitimacy_note,
        enrichment_tier=enrichment_tier,
        jd_full_length=jd_full_length,
        low_signal_threshold=threshold,
        apply_mean_floor=apply_mean_floor,
        apply_min_strong_axes=apply_min_strong_axes,
    )

    # Serialize sub_scores with stable key order for diff-friendliness.
    ordered_sub_scores = {
        k: assessment.sub_scores[k] for k in _SUB_SCORE_KEYS if k in assessment.sub_scores
    }

    cur.execute(
        """
        UPDATE jobs
           SET classification   = ?,
               sub_scores_json  = ?,
               fit_analysis     = ?,
               scoring_provider = COALESCE(?, scoring_provider),
               scoring_model    = COALESCE(?, scoring_model)
         WHERE dedup_key = ?
        """,
        (
            final_classification,
            json.dumps(ordered_sub_scores),
            json.dumps(assessment.rationale),
            provider or assessment.provider,
            model,
            dedup_key,
        ),
    )
    conn.commit()
    return final_classification
