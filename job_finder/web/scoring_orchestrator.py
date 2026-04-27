"""Scoring orchestration -- v3.0 unified entry (Phase 34 Plan 4).

Consolidates the scoring workflow (cost gate, profile loading, persistence)
for the v3.0 unified scorer. The legacy two-tier (Haiku + Sonnet) entry
points were removed in Plan 4 Commit E once all callers migrated to
score_and_persist_job.

Public API:
    score_and_persist_job(job, conn, config, client=None,
                          scorer_fn=None) -> ScoringResult | None
    load_scoring_profile(config) -> dict

These functions handle the core scoring + persistence logic. Callers remain
responsible for:
- Creating and closing DB connections (thread-safety patterns vary by caller)
- Session/batch progress tracking (dashboard-specific concern)
- Activity logging (caller-specific metadata)
- Enrichment (pipeline_runner-specific pre-scoring step)
- Exclusion filtering (caller decides when to filter)

The scorer_fn parameter allows callers to pass their own reference to the
scoring function, which preserves mock injection in tests (tests patch the
name in the caller's module namespace).
"""

import logging
import sqlite3
from collections.abc import Callable
from typing import Any

from job_finder.db import persist_job_assessment

logger = logging.getLogger(__name__)


def load_scoring_profile(config: dict) -> dict:
    """Load experience profile from disk via the canonical loader.

    Resolves the profile path from config (scoring.profile_path or
    top-level profile_path) and delegates to profile_schema.load_profile()
    for actual I/O and error handling.

    Args:
        config: Application config dict. Reads scoring.profile_path,
                then profile_path, defaulting to "experience_profile.json".

    Returns:
        Profile dict, or empty structure if file not found or invalid.
    """
    from job_finder.web.profile_schema import load_profile

    profile_path = (
        config.get("scoring", {}).get("profile_path")
        or config.get("profile_path")
        or "experience_profile.json"
    )
    return load_profile(profile_path)


def _resolve_scoring_model(config: dict, provider: str | None) -> str | None:
    """Pull the active model ID for the scoring tier from config.

    Reads providers.scoring.model per Phase 34 CONTEXT D-01 / D-10. Falls back
    to None when the block is absent — persist writes NULL, which COALESCE
    preserves any previously-captured model in the column.
    """
    providers_cfg = config.get("providers") or {}
    scoring_cfg = providers_cfg.get("scoring") or {}
    return scoring_cfg.get("model")


def score_and_persist_job(
    job: dict,
    conn: sqlite3.Connection,
    config: dict,
    client: Any | None = None,
    scorer_fn: Callable | None = None,
):
    """Unified v3.0 scoring entry point.

    - scorer_fn: defaults to job_scorer.score_job. Injection point preserved
      for tests — pass your own reference to support mock injection.
    - Persists: classification (Python-derived), sub_scores_json,
      fit_analysis (rationale payload), scoring_provider, scoring_model.
    - Returns the underlying ScoringResult (status='ok'/'skipped'/'error')
      or None if the scorer returned nothing. Missing dedup_key rows are
      silent no-ops (matches SQLite UPDATE-no-match semantics).

    Plan 4 Commit E removed the legacy haiku_score / sonnet_score /
    haiku_summary dual-write shim now that all readers consume
    classification + sub_scores_json + fit_analysis directly.
    """
    # Lazy import avoids a top-level cycle: scoring_orchestrator is imported
    # by scoring_runner, and job_scorer imports from db/model_provider which
    # already carries orchestrator-adjacent surface area.
    if scorer_fn is None:
        from job_finder.web.job_scorer import score_job as _default_scorer

        scorer_fn = _default_scorer

    dedup_key = job.get("dedup_key")
    result = scorer_fn(job, conn, config, client=client)

    if result is None:
        logger.info("score_and_persist_job: no result for dedup_key=%s", dedup_key)
        return None

    # Pass-through for skipped / error envelopes — no DB write, no raise.
    if getattr(result, "status", None) != "ok" or result.data is None:
        logger.info(
            "score_and_persist_job: skip dedup_key=%s status=%s error=%s",
            dedup_key,
            getattr(result, "status", None),
            getattr(result, "error", None),
        )
        return result

    assessment = result.data
    provider = result.provider
    model = _resolve_scoring_model(config, provider)

    persist_job_assessment(
        conn,
        dedup_key,
        assessment,
        provider=provider,
        model=model,
    )
    conn.commit()
    return result
