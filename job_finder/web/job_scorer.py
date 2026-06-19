"""Unified v3.0 scoring module — single-tier ordinal rubric.

Replaces the Phase 1/2 two-tier (Haiku + Sonnet) scoring split. Emits a
JobAssessment (6 ordinal 1-5 sub-scores + 4-list rationale); classification
is Python-derived at persist time (see derive_classification in db.py).

This module is a pure-function addition in Phase 34 Plan 1 — no callers
land until Plan 2's orchestrator wires score_and_persist_job through it.

Routes through shared call_model(tier="score", ...) per CONTEXT D-09.
Does NOT instantiate its own provider or duplicate schema-retry/cascade
logic. Inherits ~250 lines of battle-tested dispatcher behavior.

D-28 note: byte-identical determinism is not achievable on the local
Ollama + CUDA stack (non-deterministic reductions below Ollama). The
success criterion is ordinal stability — axis rankings preserved
across repeated invocations. No byte-identical test here; rescore
gates (Plan 4 G1-G4) capture the same intent via G3 correlation.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass

from job_finder.db import JobAssessment
from job_finder.db._classification import _TERMINAL_ENRICHMENT_TIERS
from job_finder.web.model_provider import call_model
from job_finder.web.scoring_prompts.v3_scoring_prompt import JOB_ASSESSMENT_SCHEMA
from job_finder.web.scoring_types import build_comp_context

log = logging.getLogger(__name__)

# Re-export the schema for callers that need the dispatcher-layer constant.
# This is the BASELINE schema; per-call schema for variant selection is
# resolved through _resolve_schema(config).
__all__ = ["JOB_ASSESSMENT_SCHEMA", "ScoringResult", "score_job"]

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

# Safety-net ceiling on JD size sent to the scorer. The cleaned jd_full is
# normally sent WHOLE — real JD prose is short (~6k chars; p95 of naively
# de-bloated postings ~18k per the 2026-06-03 JD-length investigation) and
# the local model has ~28k tokens of context headroom after the system
# prompt, so truncation is almost never needed. This cap only guards against
# a pathological / poorly-cleaned posting (HTML, duplication, Word-export
# bloat): past it we hard-truncate the tail WITH a logged warning rather
# than silently dropping content mid-document.
#
# Removing genuinely-superfluous context (boilerplate, EEO, marketing) is an
# upstream-extraction concern (trafilatura + ATS JSON — "Layer 2"), NOT the
# scorer's job. config.JD_STORAGE_MAX_CHARS (50k) bounds what can ever reach
# here; this 24k cap sits comfortably above real JD lengths and well below
# that storage bound.
_MAX_JD_CHARS = 24_000


@dataclass(frozen=True)
class ScoringResult:
    """Envelope returned by score_job(). status ∈ {"ok", "skipped", "error"}.

    - status="ok": data is a JobAssessment, provider + model are the attribution
      strings reported by the cascade.
    - status="skipped": data is None, provider/model are None, error is None —
      a precondition was not met (SCORER-05). ``reason`` names which gate fired:
        "awaiting_jd"       — jd_full absent/empty; job needs enrichment.
        "awaiting_location" — locations_structured + location both empty and the
                              job is still enrichable (D-7 / P3.2 gate, issue #391).
    - status="error": data is None, provider/model are whatever the dispatcher
      reported if the call reached it, error is a human-readable reason.
    """

    status: str
    data: JobAssessment | None
    provider: str | None = None
    model: str | None = None
    error: str | None = None
    reason: str | None = None


def _variant_name(config: dict | None) -> str:
    """Read scoring.prompt_variant from config; default to 'baseline'."""
    if not config:
        return "baseline"
    return (config.get("scoring") or {}).get("prompt_variant") or "baseline"


def _resolve_variant_module(variant_name: str):
    """Return the prompt module for a named variant.

    'baseline' aliases the production v3_scoring_prompt module. Any other
    name is resolved as ``job_finder.web.scoring_prompts.variants.<name>``.
    Unknown names raise ImportError mentioning the requested variant — never
    silently fall back to baseline (silent fallback masks experiment errors).
    """
    if variant_name == "baseline":
        from job_finder.web.scoring_prompts import v3_scoring_prompt as mod

        return mod
    import importlib

    try:
        return importlib.import_module(f"job_finder.web.scoring_prompts.variants.{variant_name}")
    except ImportError as exc:
        raise ImportError(f"Unknown scoring prompt variant: {variant_name!r}") from exc


def _resolve_schema(config: dict | None) -> dict:
    """Resolve the JSON-schema dict for the variant named in config."""
    return _resolve_variant_module(_variant_name(config)).JOB_ASSESSMENT_SCHEMA


def _build_system_prompt(
    candidate_context: str,
    config: dict | None = None,
) -> str:
    """Assemble the full system prompt from the resolved variant module.

    Variant selection: ``config["scoring"]["prompt_variant"]`` picks the
    module. 'baseline' (or absent) loads ``v3_scoring_prompt``; any other
    name loads ``scoring_prompts.variants.<name>``. Each variant module
    must export V3_SCORING_PROMPT, FIELD_REINFORCEMENT, FEWSHOT_EXAMPLES,
    and JOB_ASSESSMENT_SCHEMA (V3_SCORING_PROMPT_HEADER is optional).

    Always splices candidate_context between FIELD_REINFORCEMENT and
    FEWSHOT_EXAMPLES so the model reads:
        rubric/dimensions header -> field reinforcement -> candidate context
        -> few-shot calibration examples.

    candidate_context is REQUIRED — the v3 location_fit / comp_fit / etc.
    anchors are unscorable without knowing the candidate's target locations,
    floor, and background. The orchestrator's
    ``_resolve_candidate_context(config)`` is the single source of truth in
    production; tests inject a stub. The pre-Phase-2a no-context fallback
    was removed in this refactor — it silently produced wrong scores (e.g.
    rating an on-site Bangalore role as a 'feasible hybrid' = 4 for a
    Remote/SF-only candidate) and existed only because the wiring across
    six of seven call sites had never been completed.
    """
    if not candidate_context:
        raise ValueError(
            "_build_system_prompt: candidate_context is required. "
            "Use scoring_orchestrator._resolve_candidate_context(config) "
            "in production, or pass an explicit test stub."
        )
    mod = _resolve_variant_module(_variant_name(config))
    header = getattr(mod, "V3_SCORING_PROMPT_HEADER", None) or mod.V3_SCORING_PROMPT
    field_reinforcement = mod.FIELD_REINFORCEMENT
    fewshot = mod.FEWSHOT_EXAMPLES

    return header + "\n\n" + field_reinforcement + "\n\n" + candidate_context + "\n\n" + fewshot


def _maybe_location_facts_line(
    job: dict,
    config: dict | None,
    conn: sqlite3.Connection | None,
) -> str | None:
    """Render the v3.1 ``Location facts`` line, or None for other variants.

    Returns None unless ``config["scoring"]["prompt_variant"] == "v3_1"`` — so
    every other variant (including baseline) keeps the byte-identical
    ``Location: <string>`` user-message line.

    For v3_1, resolves the three location columns (``locations_structured``,
    ``workplace_type``, ``primary_country_code``) from the jobs row by
    dedup_key — they are not all carried on the job dict (workplace_type /
    primary_country_code are absent from JOBS_ALL_COLUMNS), the same reason
    ``_apply_location_fit_override`` reads them straight from the DB. Degrades
    gracefully to the job dict's ``locations_structured`` (and an "unknown"
    geography match) when the DB is unavailable. Never raises — a facts-block
    failure must not block scoring.
    """
    if _variant_name(config) != "v3_1":
        return None

    from job_finder.web.location_fit import resolve_targets_and_home
    from job_finder.web.scoring_prompts.location_facts import render_location_facts_line

    def _decode_locs(raw) -> list:
        if not raw:
            return []
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else raw
        except (json.JSONDecodeError, TypeError):
            return []
        return parsed if isinstance(parsed, list) else []

    locations_structured: list = []
    workplace_type = None
    primary_country_code = None

    dedup_key = job.get("dedup_key")
    if conn is not None and dedup_key:
        try:
            row = conn.execute(
                "SELECT locations_structured, workplace_type, primary_country_code "
                "FROM jobs WHERE dedup_key = ?",
                (dedup_key,),
            ).fetchone()
            if row is not None:
                locations_structured = _decode_locs(row[0])
                workplace_type = row[1]
                primary_country_code = row[2]
        except Exception:
            log.warning(
                "_maybe_location_facts_line: DB read failed for dedup_key=%s; "
                "falling back to job dict",
                dedup_key,
            )

    if not locations_structured:
        locations_structured = _decode_locs(job.get("locations_structured"))

    target_locations, home_country = resolve_targets_and_home(config or {})
    return render_location_facts_line(
        locations_structured=locations_structured,
        workplace_type=workplace_type,
        primary_country_code=primary_country_code,
        target_locations=target_locations,
        home_country=home_country,
    )


def _build_user_message(job: dict, location_line: str | None = None) -> str:
    """User-side assembly: title + company + location + comp + JD.

    Keeps the request shape stable across candidates so the LLM sees a
    consistent prompt.

    - Location: defaults to ``Location: <string>``. The v3.1 variant passes a
      pre-rendered ``location_line`` (the deterministic ``Location facts: …``
      block, D-6) via ``_maybe_location_facts_line``; all other variants pass
      None and keep the byte-identical legacy line.
    - JD: the cleaned ``jd_full`` is sent WHOLE. Real JD prose is short and
      the local model has ample context headroom, so truncation is almost
      never needed — and a silent truncation that drops the requirements /
      location / compensation sections is far worse than a slightly larger
      prompt. As a pure safety net against a pathological / poorly-cleaned
      posting, anything past ``_MAX_JD_CHARS`` is hard-truncated WITH a
      logged warning (never a silent section-drop). Removing superfluous
      content properly is an upstream-extraction job (Layer 2).
    - Compensation: the salary_min/max range is always shown; richer
      ATS-sourced comp (equity / bonus / tier summary from comp_data_json)
      is appended via ``build_comp_context`` when present.
    """
    title = job.get("title") or "(no title)"
    company = job.get("company_canonical") or job.get("company") or "(no company)"
    location = job.get("location") or "(no location)"
    loc_section = location_line if location_line is not None else f"Location: {location}"
    salary_min = job.get("salary_min")
    salary_max = job.get("salary_max")
    comp = ""
    if salary_min or salary_max:
        comp = f"\nSalary: {salary_min or '?'}-{salary_max or '?'}"
    comp_extra = build_comp_context(job)
    if comp_extra:
        comp += f"\nCompensation: {comp_extra}"

    jd_full = job.get("jd_full") or ""
    if len(jd_full) > _MAX_JD_CHARS:
        log.warning(
            "score_job: jd_full for dedup_key=%s is %d chars (> %d cap); "
            "hard-truncating tail. Signals upstream cleaning bloat "
            "(HTML / duplication) — see Layer 2 extraction plan.",
            job.get("dedup_key"),
            len(jd_full),
            _MAX_JD_CHARS,
        )
        jd = jd_full[:_MAX_JD_CHARS]
    else:
        jd = jd_full
    return f"Title: {title}\nCompany: {company}\n{loc_section}{comp}\n\nJob Description:\n{jd}"


class _IncompleteAssessmentError(ValueError):
    """A dispatcher payload was missing or uncoercible on a required axis.

    Issue #227 (mechanism 2 — fail-closed coercion). Raised by
    ``_coerce_assessment`` instead of silently dropping an axis and producing
    a partial sub-score vector that ``derive_classification`` could then read
    as ``apply``. ``score_job`` catches this and returns status="error" so the
    job is left unscored rather than wrongly classified.
    """


def _coerce_assessment(
    data: dict, provider: str | None, *, degenerate: bool = False
) -> JobAssessment:
    """Coerce dispatcher-returned dict into a JobAssessment.

    The v3 schema emits the 6 sub-score fields at the TOP LEVEL of `data`
    alongside `rationale` and `legitimacy_note` — it does NOT nest them
    under a "sub_scores" key. This function extracts them by name.

    Ignores any top-level 'classification' field the model may emit —
    classification is Python-derived at persist time (anti-pattern 3
    defense; see db.derive_classification).

    Fail-closed coercion (issue #227, mechanism 2): every one of the six axes
    is REQUIRED. A missing or uncoercible axis raises
    ``_IncompleteAssessmentError`` rather than being silently dropped. The
    previous behaviour produced a *partial* sub-score vector, which
    ``derive_classification`` could then read with ``all(v >= 3 ...)`` passing
    vacuously over the surviving axes → a spurious ``apply``. Making the
    partial vector unrepresentable here closes that hole regardless of which
    upstream schema is in play. The baseline schema already rejects partial
    vectors at the dispatcher, so this is latent insurance against variant
    schemas — but cheap and correct insurance.

    ``degenerate`` is threaded through from the cascade (ModelResult.degenerate)
    so persistence can route an all-providers-degenerate result to low_signal.
    """
    sub_scores: dict[str, int] = {}
    for key in _SUB_SCORE_KEYS:
        raw = data.get(key)
        if raw is None:
            raise _IncompleteAssessmentError(
                f"assessment missing required axis {key!r} "
                f"(present axes: {sorted(k for k in _SUB_SCORE_KEYS if data.get(k) is not None)})"
            )
        # Variant v4d2 emits each axis as {"evidence": "...", "score": <int>}.
        # Unwrap the score; everything downstream (derive_classification,
        # persistence) only needs the integer.
        if isinstance(raw, dict) and "score" in raw:
            raw = raw["score"]
        try:
            sub_scores[key] = int(raw)
        except (TypeError, ValueError) as exc:
            raise _IncompleteAssessmentError(
                f"assessment axis {key!r} is not coercible to int (got {raw!r})"
            ) from exc
    rationale = data.get("rationale") or {}
    # classification is the sentinel — persist_job_assessment overwrites
    # it with derive_classification(sub_scores, row.legitimacy_note).
    return JobAssessment(
        sub_scores=sub_scores,
        classification="",
        rationale=rationale,
        provider=provider,
        degenerate=degenerate,
    )


def score_job(
    job: dict,
    conn: sqlite3.Connection,
    config: dict,
    candidate_context: str,
) -> ScoringResult:
    """Score a single job with the v3.0 ordinal rubric.

    Two completeness gates (D-7, no garbage-in scoring):

    SCORER-05 (jd_full gate): empty or missing jd_full returns
    status='skipped' (reason='awaiting_jd') without invoking call_model —
    no API call, no cost, no log spam.

    P3.2 (location gate, issue #391): when locations_structured AND location
    are both empty AND the job is not at a terminal enrichment tier AND the
    row does not carry "location_missing" in unresolved_reasons, returns
    status='skipped' (reason='awaiting_location'). Batch scoring re-selects
    classification IS NULL continuously, so the gate self-heals once P2.3
    fills location — it cannot orphan jobs.

    Routes through call_model(tier='scoring', output_schema=JOB_ASSESSMENT_SCHEMA)
    per CONTEXT D-09 — inherits schema retry, cascade fallback (Ollama → Groq →
    Cerebras → Gemini → Anthropic per D-10), rate limiting, provider attribution.

    Args:
        job: Job row dict with dedup_key, title, company_canonical (or company),
            location, locations_structured, salary_min, salary_max, jd_full,
            enrichment_tier, unresolved_reasons.
        conn: Open sqlite3 connection (used by call_model for cost recording
            and rate-limit bootstrap).
        config: Application config dict.
        candidate_context: REQUIRED prompt-ready candidate-context block. The
            v3 rubric anchors (location_fit, comp_fit, etc.) reference
            candidate-specific facts (target locations, comp floor, target
            titles) — scoring without this block silently produces wrong
            scores. Production callers route through
            ``scoring_orchestrator.score_and_persist_job``, which resolves
            this from config via the memoized
            ``_resolve_candidate_context(config)``. Direct callers (eval
            harness, tests) must build it explicitly via
            ``build_candidate_context(config, profile)``.

    Returns:
        ScoringResult envelope.
          ok      → data is JobAssessment, provider is attribution string.
          skipped → data is None; reason='awaiting_jd' or 'awaiting_location'.
          error   → data is None, error is reason string.
    """
    jd = job.get("jd_full")
    if not jd:
        log.info(
            "score_job: skip dedup_key=%s (empty jd_full)",
            job.get("dedup_key"),
        )
        return ScoringResult(status="skipped", data=None, reason="awaiting_jd")

    # D-7 (Completeness gates, not garbage-in scoring) / P3.2 (issue #391):
    # Gate on location resolvability — mirrors the jd_full gate above.
    #
    # A job is gated when ALL of the following hold:
    #   1. locations_structured is empty (no gazetteer-resolved JobLocation objects)
    #   2. location flat string is also empty (no display string either)
    #   3. enrichment_tier is NOT in _TERMINAL_ENRICHMENT_TIERS — the job can still
    #      be enriched (P2.3 will fill location), so scoring now would be premature.
    #      Terminal-tier rows pass through: their location is as good as it will
    #      ever get, so the LLM must judge from JD prose.
    #   4. The row does NOT carry "location_missing" in unresolved_reasons — that
    #      code (introduced by P2.3/P2.4, issue #388/#389) signals that location
    #      was explicitly confirmed unresolvable from all available evidence, so
    #      blocking forever would orphan the row.
    #
    # Rationale: batch scoring re-selects classification IS NULL continuously, so
    # the gate self-heals once P2.3 fills location — it cannot orphan jobs.
    # _TERMINAL_ENRICHMENT_TIERS is imported from job_finder.db._classification
    # (the three-element scoring-terminal set: exhausted, agentic, agentic_exhausted)
    # NOT from enrichment_states.TERMINAL (the seven-tier backfill-exclusion list).
    _locs_structured_raw = job.get("locations_structured")
    _locs_structured: list = []
    if _locs_structured_raw:
        try:
            _parsed = json.loads(_locs_structured_raw)
            if isinstance(_parsed, list):
                _locs_structured = _parsed
        except (json.JSONDecodeError, TypeError):
            pass  # treat malformed JSON as empty — no structured data

    _location_flat = job.get("location") or ""
    _enrichment_tier = job.get("enrichment_tier")
    _unresolved_reasons: list[str] = []
    _unresolved_raw = job.get("unresolved_reasons")
    if _unresolved_raw:
        try:
            _parsed_reasons = json.loads(_unresolved_raw)
            if isinstance(_parsed_reasons, list):
                _unresolved_reasons = _parsed_reasons
        except (json.JSONDecodeError, TypeError):
            pass

    if (
        not _locs_structured
        and not _location_flat.strip()
        and _enrichment_tier not in _TERMINAL_ENRICHMENT_TIERS
        and "location_missing" not in _unresolved_reasons
    ):
        log.info(
            "score_job: skip dedup_key=%s (empty location, enrichment_tier=%r, "
            "awaiting P2.3 location fill — D-7/P3.2)",
            job.get("dedup_key"),
            _enrichment_tier,
        )
        return ScoringResult(status="skipped", data=None, reason="awaiting_location")

    system = _build_system_prompt(candidate_context=candidate_context, config=config)
    location_line = _maybe_location_facts_line(job, config, conn)
    user_content = _build_user_message(job, location_line=location_line)
    output_schema = _resolve_schema(config)

    try:
        result = call_model(
            tier="score",
            system=system,
            messages=[{"role": "user", "content": user_content}],
            conn=conn,
            config=config,
            output_schema=output_schema,
            job_id=job.get("dedup_key"),
            purpose="score_job",
            max_tokens=2048,
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
            model=result.model,
            error="dispatcher returned empty or schema-invalid data",
        )

    try:
        assessment = _coerce_assessment(
            result.data,
            result.provider,
            degenerate=getattr(result, "degenerate", False),
        )
    except _IncompleteAssessmentError as exc:
        # Fail-closed (issue #227): a partial/uncoercible vector must not be
        # persisted as a complete score. Leave the job unscored.
        log.warning(
            "score_job: incomplete assessment for dedup_key=%s from provider=%s: %s",
            job.get("dedup_key"),
            result.provider,
            exc,
        )
        return ScoringResult(
            status="error",
            data=None,
            provider=result.provider,
            model=result.model,
            error=f"incomplete assessment: {exc}",
        )
    return ScoringResult(
        status="ok",
        data=assessment,
        provider=result.provider,
        model=result.model,
    )
