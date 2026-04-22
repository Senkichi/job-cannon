"""Unified v3.0 scoring module — single-tier ordinal rubric.

Replaces the Phase 1/2 two-tier (Haiku + Sonnet) scoring split. Emits a
JobAssessment (6 ordinal 1-5 sub-scores + 4-list rationale); classification
is Python-derived at persist time (see derive_classification in db.py).

This module is a pure-function addition in Phase 34 Plan 1 — no callers
land until Plan 2's orchestrator wires score_and_persist_job through it.

Routes through shared call_model(tier="scoring", ...) per CONTEXT D-09.
Does NOT instantiate its own provider or duplicate schema-retry/cascade
logic. Inherits ~250 lines of battle-tested dispatcher behavior.

D-28 note: byte-identical determinism is not achievable on the local
Ollama + CUDA stack (non-deterministic reductions below Ollama). The
success criterion is ordinal stability — axis rankings preserved
across repeated invocations. No byte-identical test here; rescore
gates (Plan 4 G1-G4) capture the same intent via G3 correlation.
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from typing import Any

from job_finder.db import JobAssessment
from job_finder.web.model_provider import call_model
from job_finder.web.scoring_prompts.v3_scoring_prompt import (
    V3_SCORING_PROMPT,
    JOB_ASSESSMENT_SCHEMA,
    FEWSHOT_EXAMPLES,
    FIELD_REINFORCEMENT,
)

log = logging.getLogger(__name__)

# Re-export the schema for callers that need the dispatcher-layer constant.
__all__ = ["score_job", "ScoringResult", "JOB_ASSESSMENT_SCHEMA"]

# Canonical sub-score keys (matches v3 prompt schema + CONTEXT D-05).
# The LLM emits these at the TOP LEVEL of the response alongside `rationale`
# and `legitimacy_note` — NOT nested under "sub_scores".
_SUB_SCORE_KEYS: tuple[str, ...] = (
    "title_fit",
    "location_fit",
    "comp_fit",
    "domain_match",
    "seniority_match",
    "skills_match",
)

# Simple truncation guardrail. sonnet_evaluator.py uses a richer
# build_description_snippet helper; Plan 4 (COLLAPSE-01) migrates the
# shared helper into scoring_types.py. Plan 1 uses this simpler
# constant to avoid coupling to haiku_scorer before it is deleted.
_MAX_JD_CHARS = 10_000


@dataclass(frozen=True)
class ScoringResult:
    """Envelope returned by score_job(). status ∈ {"ok", "skipped", "error"}.

    - status="ok": data is a JobAssessment, provider is the attribution string.
    - status="skipped": data is None, provider is None, error is None —
      precondition (jd_full present) was not met. SCORER-05.
    - status="error": data is None, provider is whatever the dispatcher
      reported if the call reached it, error is a human-readable reason.
    """

    status: str
    data: JobAssessment | None
    provider: str | None = None
    error: str | None = None


def _build_system_prompt() -> str:
    """Assemble the full system prompt from the frozen v3 modules.

    Concatenates V3_SCORING_PROMPT + FEWSHOT_EXAMPLES + FIELD_REINFORCEMENT.
    No caching — the assembly cost is negligible vs the model call.
    """
    return V3_SCORING_PROMPT + "\n\n" + FEWSHOT_EXAMPLES + "\n\n" + FIELD_REINFORCEMENT


def _build_user_message(job: dict) -> str:
    """Minimal user-side assembly: title + company + location + (truncated) JD.

    Keeps the user message small and consistent across candidates so the
    LLM sees a stable request shape. Plan 4 may migrate to a richer
    helper that mirrors sonnet_evaluator.evaluate_job_sonnet's format.
    """
    title = job.get("title") or "(no title)"
    company = job.get("company_canonical") or job.get("company") or "(no company)"
    location = job.get("location") or "(no location)"
    salary_min = job.get("salary_min")
    salary_max = job.get("salary_max")
    comp = ""
    if salary_min or salary_max:
        comp = f"\nSalary: {salary_min or '?'}-{salary_max or '?'}"
    jd = (job.get("jd_full") or "")[:_MAX_JD_CHARS]
    return (
        f"Title: {title}\n"
        f"Company: {company}\n"
        f"Location: {location}"
        f"{comp}\n\n"
        f"Job Description:\n{jd}"
    )


def _coerce_assessment(data: dict, provider: str | None) -> JobAssessment:
    """Coerce dispatcher-returned dict into a JobAssessment.

    The v3 schema emits the 6 sub-score fields at the TOP LEVEL of `data`
    alongside `rationale` and `legitimacy_note` — it does NOT nest them
    under a "sub_scores" key. This function extracts them by name.

    Ignores any top-level 'classification' field the model may emit —
    classification is Python-derived at persist time (anti-pattern 3
    defense; see db.derive_classification).

    Defensive int-coercion: the dispatcher's _sanitize_output should have
    already coerced strings→ints, but we re-enforce here so downstream
    derive_classification and persist_job_assessment see integers.
    """
    sub_scores: dict[str, int] = {}
    for key in _SUB_SCORE_KEYS:
        raw = data.get(key)
        if raw is None:
            continue
        try:
            sub_scores[key] = int(raw)
        except (TypeError, ValueError):
            # Schema validation would have already rejected this in the
            # dispatcher; skip silently to avoid cascading failure here.
            continue
    rationale = data.get("rationale") or {}
    # classification is the sentinel — persist_job_assessment overwrites
    # it with derive_classification(sub_scores, row.legitimacy_note).
    return JobAssessment(
        sub_scores=sub_scores,
        classification="",
        rationale=rationale,
        provider=provider,
    )


def score_job(
    job: dict,
    conn: sqlite3.Connection,
    config: dict,
    client: Any | None = None,
) -> ScoringResult:
    """Score a single job with the v3.0 ordinal rubric.

    Per SCORER-05: empty or missing jd_full returns status='skipped' without
    invoking call_model (no API call, no cost, no log spam).

    Routes through call_model(tier='scoring', output_schema=JOB_ASSESSMENT_SCHEMA)
    per CONTEXT D-09 — inherits schema retry, cascade fallback (Ollama → Groq →
    Cerebras → Gemini → Anthropic per D-10), rate limiting, provider attribution.

    Args:
        job: Job row dict with dedup_key, title, company_canonical (or company),
            location, salary_min, salary_max, jd_full.
        conn: Open sqlite3 connection (used by call_model for cost recording
            and rate-limit bootstrap).
        config: Application config dict.
        client: Optional pre-constructed client for tests. Defaults to None,
            which lets call_model build one via resolve_provider_config.

    Returns:
        ScoringResult envelope.
          ok      → data is JobAssessment, provider is attribution string.
          skipped → data is None (jd_full absent).
          error   → data is None, error is reason string.
    """
    jd = job.get("jd_full")
    if not jd:
        log.info(
            "score_job: skip dedup_key=%s (empty jd_full)",
            job.get("dedup_key"),
        )
        return ScoringResult(status="skipped", data=None)

    system = _build_system_prompt()
    user_content = _build_user_message(job)

    try:
        result = call_model(
            tier="scoring",
            system=system,
            messages=[{"role": "user", "content": user_content}],
            conn=conn,
            config=config,
            output_schema=JOB_ASSESSMENT_SCHEMA,
            job_id=job.get("dedup_key"),
            purpose="score_job",
            max_tokens=2048,
            client=client,
        )
    except Exception as exc:
        log.exception(
            "score_job: dispatcher error for dedup_key=%s",
            job.get("dedup_key"),
        )
        return ScoringResult(status="error", data=None, error=str(exc))

    if not result.data or not result.schema_valid:
        return ScoringResult(
            status="error",
            data=None,
            provider=result.provider,
            error="dispatcher returned empty or schema-invalid data",
        )

    assessment = _coerce_assessment(result.data, result.provider)
    return ScoringResult(
        status="ok", data=assessment, provider=result.provider
    )
