"""Centralized scoring orchestration for Haiku and Sonnet evaluation.

Consolidates the scoring workflow (cost gate, client creation, profile loading,
API call, borderline re-evaluation, DB persistence) that was previously
duplicated across pipeline_runner, dashboard batch scoring, and jobs blueprint
routes.

Public API:
    score_and_persist_haiku(conn, job_row, config, client, profile,
                            scorer_fn=None) -> dict | None
    score_and_persist_sonnet(conn, job_row, config, client, profile,
                             evaluator_fn=None) -> dict | None
    load_scoring_profile(config) -> dict

These functions handle the core scoring + persistence logic. Callers remain
responsible for:
- Creating and closing DB connections (thread-safety patterns vary by caller)
- Session/batch progress tracking (dashboard-specific concern)
- Activity logging (caller-specific metadata)
- Enrichment (pipeline_runner-specific pre-scoring step)
- Exclusion filtering (caller decides when to filter)

The scorer_fn / evaluator_fn parameters allow callers to pass their own
reference to the scoring function, which preserves mock injection in tests
(tests patch the name in the caller's module namespace).
"""

import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Callable, Optional

from job_finder.config import DEFAULT_BORDERLINE_HIGH, DEFAULT_HAIKU_THRESHOLD
from job_finder.db import persist_haiku_score, persist_sonnet_score, persist_job_expiry_state
from job_finder.web.scoring_types import unwrap_scoring_result

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


def score_and_persist_haiku(
    conn: sqlite3.Connection,
    job_row: dict,
    config: dict,
    client,
    profile: dict,
    scorer_fn: Optional[Callable] = None,
) -> Optional[dict]:
    """Run Haiku scoring with borderline re-evaluation and persist results.

    Calls score_job_haiku for the initial score. If the score falls in the
    borderline band (threshold <= score <= borderline_high), triggers a
    re-evaluation with expanded context (max_chars=4000).

    Persists haiku_score and haiku_summary to the jobs table after each
    scoring call.

    Args:
        conn: Open SQLite connection (caller manages lifecycle).
        job_row: Dict-like job row from the jobs table.
        config: Application config dict.
        client: Anthropic client instance.
        profile: Experience profile dict.
        scorer_fn: Optional scoring function reference. If None, imports
                   score_job_haiku from haiku_scorer. Pass the caller's
                   own reference to support test mock injection.

    Returns:
        The final scoring result dict (with 'score' and 'summary' keys),
        or None if the initial scoring returned no result.
    """
    if scorer_fn is None:
        from job_finder.web.haiku_scorer import score_job_haiku
        scorer_fn = score_job_haiku

    dedup_key = job_row.get("dedup_key", "unknown")

    scoring_result = scorer_fn(client, job_row, profile, conn, config)

    result = unwrap_scoring_result(scoring_result)
    if result is None:
        return None

    score = result.get("score", 0)
    summary_text = result.get("summary", "")

    persist_haiku_score(conn, dedup_key, score, summary_text)

    # --- Borderline re-evaluation band ---
    threshold = config.get("scoring", {}).get("haiku_threshold", DEFAULT_HAIKU_THRESHOLD)
    borderline_high = DEFAULT_BORDERLINE_HIGH
    if threshold <= score <= borderline_high:
        logger.info(
            "Borderline re-eval for '%s' (initial=%d, band=%d-%d)",
            dedup_key, score, threshold, borderline_high,
        )
        reeval_scoring = scorer_fn(
            client, job_row, profile, conn, config,
            max_chars=4000, purpose="haiku_reeval",
        )
        reeval_result = unwrap_scoring_result(reeval_scoring)

        if reeval_result is not None:
            score = reeval_result.get("score", 0)
            summary_text = reeval_result.get("summary", "")
            persist_haiku_score(conn, dedup_key, score, summary_text)
            logger.info(
                "Borderline re-eval result for '%s': %d",
                dedup_key, score,
            )
            result = reeval_result

    return result


def _preflight_sonnet_job(
    conn: sqlite3.Connection,
    job_row: dict,
    liveness_cache: Optional[dict] = None,
) -> Optional[str]:
    """Run liveness preflight before Sonnet evaluation.

    Checks if the job posting URL is still live. Persists the result via
    persist_job_expiry_state(). Returns 'expired' if the job should be
    skipped, None otherwise.

    Args:
        conn: Open SQLite connection.
        job_row: Dict-like job row (must include source_urls, dedup_key).
        liveness_cache: Optional URL→status dict. When provided, cached
            results are reused to avoid re-checking the same URL in a batch.

    Returns:
        'expired' if job is dead (caller should skip Sonnet), None to proceed.
    """
    from job_finder.web.expiry_checker import check_job_liveness, EXPIRED

    # Resolve cache key from first source URL
    source_urls_raw = job_row.get("source_urls", "[]")
    if isinstance(source_urls_raw, str):
        try:
            source_urls = json.loads(source_urls_raw)
        except (json.JSONDecodeError, TypeError):
            source_urls = []
    else:
        source_urls = source_urls_raw or []

    cache_key = source_urls[0] if source_urls else None

    if liveness_cache is not None and cache_key and cache_key in liveness_cache:
        status = liveness_cache[cache_key]
    else:
        status = check_job_liveness(job_row)
        if liveness_cache is not None and cache_key:
            liveness_cache[cache_key] = status

    dedup_key = job_row.get("dedup_key", "unknown")
    now = datetime.now(timezone.utc).isoformat()
    persist_job_expiry_state(conn, dedup_key, status, now)

    if status == EXPIRED:
        logger.info("Sonnet preflight: skipping expired job '%s'", dedup_key)
        return EXPIRED

    return None  # proceed with Sonnet


def score_and_persist_sonnet(
    conn: sqlite3.Connection,
    job_row: dict,
    config: dict,
    client,
    profile: dict,
    evaluator_fn: Optional[Callable] = None,
    liveness_cache: Optional[dict] = None,
) -> Optional[dict]:
    """Run Sonnet evaluation and persist results.

    Runs a liveness preflight before evaluation — returns None if the job
    posting is expired. Calls evaluate_job_sonnet and writes sonnet_score
    and fit_analysis to the jobs table. Returns None if the evaluator
    returns None (budget exceeded, JD missing, etc.).

    Args:
        conn: Open SQLite connection (caller manages lifecycle).
        job_row: Dict-like job row from the jobs table (must have jd_full).
        config: Application config dict.
        client: Anthropic client instance.
        profile: Experience profile dict.
        evaluator_fn: Optional evaluator function reference. If None, imports
                      evaluate_job_sonnet from sonnet_evaluator. Pass the
                      caller's own reference to support test mock injection.
        liveness_cache: Optional URL→status dict for batch deduplication.
            When provided, skip the HTTP check for URLs already resolved
            in this batch run.

    Returns:
        The Sonnet evaluation result dict (with 'score' and 'fit_analysis'),
        or None if evaluation was skipped or returned no result.
    """
    # --- Liveness preflight ---
    preflight = _preflight_sonnet_job(conn, job_row, liveness_cache=liveness_cache)
    if preflight is not None:
        return None

    if evaluator_fn is None:
        from job_finder.web.sonnet_evaluator import evaluate_job_sonnet
        evaluator_fn = evaluate_job_sonnet

    dedup_key = job_row.get("dedup_key", "unknown")

    scoring_result = evaluator_fn(client, job_row, profile, conn, config)

    result = unwrap_scoring_result(scoring_result)
    if result is None:
        return None

    raw_score = result.get("score")
    fit_analysis = json.dumps(result.get("fit_analysis", {}))
    eval_blocks = result.get("eval_blocks")
    eval_blocks_json = json.dumps(eval_blocks) if eval_blocks is not None else None
    provider = result.get("provider")  # Attribution: which provider scored this job

    # Post-hoc isotonic calibration: map raw model score to Opus scale
    # when a calibration table exists for the provider. Uncalibrated providers
    # pass through unchanged.
    # Calibration bypass: when eval_blocks are present, use the raw score
    # directly — structured evaluation criteria make the score self-calibrated.
    from job_finder.web.score_calibration import calibrate_score, has_calibration

    sonnet_score = raw_score
    if eval_blocks is not None:
        logger.debug(
            "Calibration bypass for '%s': eval_blocks present, using raw score %s",
            dedup_key, raw_score,
        )
    elif provider and has_calibration(provider) and raw_score is not None:
        sonnet_score = calibrate_score(raw_score, provider)
        logger.info(
            "Calibrated %s score for '%s': %s -> %s",
            provider, dedup_key, raw_score, sonnet_score,
        )
        result["raw_score"] = raw_score
        result["score"] = sonnet_score

    persist_sonnet_score(
        conn, dedup_key, sonnet_score, fit_analysis,
        provider=provider, eval_blocks=eval_blocks_json,
    )

    return result
