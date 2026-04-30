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
    candidate_context: str | None = None,
):
    """Unified v3.0 scoring entry point.

    - scorer_fn: defaults to job_scorer.score_job. Injection point preserved
      for tests — pass your own reference to support mock injection.
    - candidate_context: Optional prompt-ready candidate-context block built
      by ``build_candidate_context``. Forwarded to scorer_fn so the scoring
      system prompt can splice it per spec D-2.1. Defaults to None for
      callers that have not yet been wired (Phase 2a sub-fix 3/3 wires
      batch_scoring; other call sites remain default-safe pending the
      relevant blueprint update).
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
    result = scorer_fn(job, conn, config, client=client, candidate_context=candidate_context)

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
        config=config,
    )
    conn.commit()
    return result


def build_candidate_context(config: dict, profile: dict) -> str:
    """Merge config.yaml [profile] (targeting) and experience_profile.json
    (resume) into a prompt-ready candidate-context string.

    Returns a structured-text block ~400-500 tokens that gets spliced into
    the scoring system prompt between FIELD_REINFORCEMENT and FEWSHOT_EXAMPLES
    per spec D-2.1. Output stays under ~600 tokens (~2400 chars) via top-30
    skills + first-6 positions truncation.

    Args:
        config: Application config dict. Reads ``config["profile"]`` for
            targeting fields (target_titles, target_locations, min_salary,
            industries, exclusions).
        profile: Experience profile dict (typically loaded via
            load_scoring_profile). Reads positions, skills, education.

    Returns:
        A multi-section markdown string with "## Candidate context" header.
        Always returns a non-empty string even when both inputs are empty
        (uses "Not specified" / "No positions" sentinels).
    """
    cfg_profile = config.get("profile") or {}

    # Targeting block
    target_titles = cfg_profile.get("target_titles") or []
    target_locations = cfg_profile.get("target_locations") or []
    min_salary = cfg_profile.get("min_salary")
    industries = cfg_profile.get("industries") or []
    exclusions = cfg_profile.get("exclusions") or {}
    excl_companies = exclusions.get("companies") or []

    parts: list[str] = ["## Candidate context", "", "### Targeting"]
    parts.append(
        f"- Target titles: {', '.join(target_titles) if target_titles else 'Not specified'}"
    )
    if target_titles:
        parts.append(
            "  (These are exemplars of the candidate's role-function intent, not an "
            "exhaustive whitelist. Near-variants — same role function with adjacent "
            "wording, e.g. 'Lead Data Analyst' for 'Lead Analyst', or 'Senior/Staff "
            "Data Scientist' for 'Senior Data Scientist' — count as title matches "
            "and should score title_fit >= 4. Score 5 only for exact-or-stronger matches.)"
        )
    parts.append(
        f"- Target locations: {', '.join(target_locations) if target_locations else 'Not specified'}"
    )
    if target_locations:
        parts.append(
            "  (A JD location is a match if it appears in this list, OR if it is "
            "fully remote when 'Remote' is in the list. On-site/hybrid in a "
            "listed geography is a match — geography membership overrides on-site "
            "penalty for location_fit.)"
        )
    parts.append(
        f"- Compensation floor: ${min_salary:,}"
        if min_salary
        else "- Compensation floor: Not specified"
    )
    parts.append(
        f"- Target industries: {', '.join(industries) if industries else 'Not specified'}"
    )
    if excl_companies:
        parts.append(f"- Exclusions: companies {excl_companies}")

    # Resume block
    parts += ["", "### Background"]
    positions = profile.get("positions") or []
    if not positions:
        parts.append("- No positions in profile")
    else:
        for p in positions[:6]:  # cap at 6 most recent
            title = p.get("title", "?")
            company = p.get("company", "?")
            start = p.get("start_date", "?")
            end = p.get("end_date") or "present"
            parts.append(f"- {title} @ {company} ({start}-{end})")

    skills = profile.get("skills") or []
    if skills:
        parts.append(f"- Top skills: {', '.join(skills[:30])}")

    education = profile.get("education") or []
    for e in education[:3]:
        deg = e.get("degree") or "?"
        inst = e.get("institution") or "?"
        grad = e.get("graduation") or ""
        parts.append(f"- {deg} ({inst}{', ' + str(grad) if grad else ''})")

    return "\n".join(parts)
