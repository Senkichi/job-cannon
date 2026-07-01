"""Scoring orchestration -- v3.0 unified entry (Phase 34 Plan 4).

Consolidates the scoring workflow (cost gate, profile loading, persistence)
for the v3.0 unified scorer. The legacy two-tier (Haiku + Sonnet) entry
points were removed in Plan 4 Commit E once all callers migrated to
score_and_persist_job.

Public API:
    score_and_persist_job(job, conn, config,
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

import hashlib
import json
import logging
import os
import sqlite3
import threading
from collections.abc import Callable

from job_finder.db import JobAssessment, persist_job_assessment
from job_finder.web import user_data_dirs

logger = logging.getLogger(__name__)

# Memoized candidate context. The cache lives at module scope (one slot is
# enough for a single-user local app, but the dict structure leaves room for
# multi-config eval runs). Invalidation is automatic — the fingerprint hashes
# the relevant config slice plus the experience-profile file mtime, so any
# settings save or profile edit produces a new key.
_CONTEXT_CACHE: dict[str, str] = {}
_CONTEXT_CACHE_LOCK = threading.Lock()
_CONTEXT_CACHE_MAX = 8  # cap to avoid unbounded growth in eval sweeps


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
        or str(user_data_dirs.profile_path())
    )
    return load_profile(profile_path)


def _profile_path(config: dict) -> str:
    """Single source of truth for the profile file path.

    Defaults to user_data_dirs.profile_path() — the same absolute location the
    onboarding wizard writes and the Profile editor reads — instead of a bare
    CWD-relative string, so a packaged/pipx install doesn't score against an
    empty file that lives somewhere other than where onboarding saved it.
    """
    return (
        config.get("scoring", {}).get("profile_path")
        or config.get("profile_path")
        or str(user_data_dirs.profile_path())
    )


def _context_fingerprint(config: dict) -> str:
    """Stable fingerprint of all inputs that affect the candidate context.

    Hashes the ``config["profile"]`` block (target titles / locations / floor
    / industries / exclusions) together with the experience-profile file's
    mtime. Settings saves rebuild config["profile"], so the JSON content
    changes; profile-file edits change the mtime. Either invalidates the
    cache automatically — no manual flush required.
    """
    cfg_profile = config.get("profile") or {}
    path = _profile_path(config)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = 0.0
    blob = json.dumps(
        {"profile": cfg_profile, "mtime": mtime, "path": path},
        sort_keys=True,
        default=str,
    )
    return hashlib.sha1(blob.encode("utf-8"), usedforsecurity=False).hexdigest()


def _resolve_candidate_context(config: dict) -> str:
    """Return the prompt-ready candidate-context block for this config.

    Memoized by ``_context_fingerprint(config)``. Cache invalidates when
    the relevant config slice changes or the profile file is rewritten.
    This is the production-path entry point; tests can still call
    ``build_candidate_context`` directly for unit-level assertions.
    """
    key = _context_fingerprint(config)
    with _CONTEXT_CACHE_LOCK:
        cached = _CONTEXT_CACHE.get(key)
        if cached is not None:
            return cached

    # Load + build OUTSIDE the lock — load_profile does file I/O, and we
    # don't want to serialize unrelated scorers behind a slow disk read.
    profile = load_scoring_profile(config)
    ctx = build_candidate_context(config, profile)

    with _CONTEXT_CACHE_LOCK:
        # Evict-oldest if we're at the cap. dict insertion order is the
        # FIFO we want; pop the first key.
        if len(_CONTEXT_CACHE) >= _CONTEXT_CACHE_MAX and key not in _CONTEXT_CACHE:
            oldest = next(iter(_CONTEXT_CACHE))
            _CONTEXT_CACHE.pop(oldest, None)
        _CONTEXT_CACHE[key] = ctx
    return ctx


def _apply_location_fit_override(
    assessment: JobAssessment,
    job: dict,
    conn: sqlite3.Connection,
    config: dict,
) -> JobAssessment:
    """Apply the P3.1 deterministic location_fit override to a JobAssessment.

    Reads ``locations_structured``, ``workplace_type``, and
    ``primary_country_code`` directly from the jobs row (these three columns
    are NOT in JOBS_ALL_COLUMNS, so the job dict passed by the caller does not
    carry them). Delegates to ``compute_location_fit`` for the rule table.

    Returns a new ``JobAssessment`` (immutability — frozen dataclass) with:
    - ``sub_scores['location_fit']`` replaced by the deterministic verdict
    - ``rationale['overrides']`` appended with an audit note recording the
      override (NOT ``gaps`` — that is a user-facing shortcomings list and is
      scanned by the eval coherence metric)

    Returns the original ``assessment`` unchanged when:
    - the DB row is missing (dedup_key not found)
    - ``compute_location_fit`` returns ``None`` (LLM judgment wins)
    - the location_fit score is already the same value (no-op)

    D-6 (facts beat judgment), P3.1.
    """
    from job_finder.web.location_canonical import from_json as _locs_from_json
    from job_finder.web.location_fit import compute_location_fit

    dedup_key = job.get("dedup_key")
    if not dedup_key:
        return assessment

    try:
        row = conn.execute(
            "SELECT locations_structured, workplace_type, primary_country_code "
            "FROM jobs WHERE dedup_key = ?",
            (dedup_key,),
        ).fetchone()
    except Exception:
        logger.warning(
            "_apply_location_fit_override: DB read failed for dedup_key=%s",
            dedup_key,
        )
        return assessment

    if row is None:
        return assessment

    try:
        import dataclasses

        locations_structured = [
            dataclasses.asdict(loc) if not isinstance(loc, dict) else loc
            for loc in _locs_from_json(row[0] or "[]")
        ]
    except Exception:
        locations_structured = []

    workplace_type = row[1]
    primary_country_code = row[2]

    cfg_profile = config.get("profile") or {}
    target_locations: list[str] = list(cfg_profile.get("target_locations") or [])
    home_country: str | None = cfg_profile.get("home_country") or None
    work_arrangement: str | None = cfg_profile.get("work_arrangement") or None
    # When the candidate targets remote work, synthesize the "remote" sentinel so
    # compute_location_fit's Row 1/2 remote-eligibility checks still fire correctly.
    # This is a call-site adaptation: the rule table in compute_location_fit is unchanged.
    if work_arrangement == "remote" and not any(
        (t or "").strip().lower() == "remote" for t in target_locations
    ):
        target_locations = ["remote"] + target_locations

    verdict = compute_location_fit(
        locations_structured=locations_structured,
        workplace_type=workplace_type,
        primary_country_code=primary_country_code,
        target_locations=target_locations,
        home_country=home_country,
        work_arrangement=work_arrangement,
    )

    if verdict is None:
        # LLM judgment is authoritative — no override.
        return assessment

    new_score, reason = verdict
    llm_score = assessment.sub_scores.get("location_fit")

    if llm_score == new_score:
        # Scores agree — no observable change, avoid a spurious rationale entry.
        return assessment

    logger.info(
        "_apply_location_fit_override: dedup_key=%s location_fit %s→%s (%s)",
        dedup_key,
        llm_score,
        new_score,
        reason,
    )

    new_sub_scores = {**assessment.sub_scores, "location_fit": new_score}

    # Record the override in a dedicated ``overrides`` audit field — NOT in
    # ``gaps`` (D-6 audit trail). ``gaps`` is a user-facing list of role
    # shortcomings; it is also scanned by the eval coherence metric
    # (job_finder.eval.metrics.coherence_violations). An audit note there
    # rendered as a bogus headline gap in the UI (it lands at gaps[0]) AND
    # manufactured false coherence violations — the note names "on-site"/
    # "geography" while location_fit is scored high. The overrides list is
    # persisted verbatim in fit_analysis and surfaced via logs, so the audit
    # trail is preserved without polluting the gaps surface.
    rationale = dict(assessment.rationale)
    override_note = (
        f"[location_fit override P3.1] {reason} (LLM: {llm_score} → deterministic: {new_score})"
    )
    existing_overrides: list = list(rationale.get("overrides") or [])
    rationale = {**rationale, "overrides": [*existing_overrides, override_note]}

    return JobAssessment(
        sub_scores=new_sub_scores,
        classification=assessment.classification,
        rationale=rationale,
        provider=assessment.provider,
        degenerate=assessment.degenerate,
    )


def score_and_persist_job(
    job: dict,
    conn: sqlite3.Connection,
    config: dict,
    scorer_fn: Callable | None = None,
    *,
    run_id: str | None = None,
):
    """Unified v3.0 scoring entry point.

    - scorer_fn: defaults to job_scorer.score_job. Injection point preserved
      for tests — pass your own reference to support mock injection.
    - The candidate-context block is resolved INTERNALLY via
      ``_resolve_candidate_context(config)`` — callers cannot bypass it.
      Single-point-of-enforcement: every scoring call sees the candidate's
      target locations / titles / floor / background, so the v3 rubric
      anchors (e.g. "on-site in a location candidate cannot relocate to")
      can be applied correctly. Spec D-2.1 / D-2.2.
    - Persists: classification (Python-derived), sub_scores_json,
      fit_analysis (rationale payload), scoring_provider, scoring_model.
    - Returns the underlying ScoringResult (status='ok'/'skipped'/'error')
      or None if the scorer returned nothing. Missing dedup_key rows are
      silent no-ops (matches SQLite UPDATE-no-match semantics).
    - run_id: optional correlation id from the scheduler / harness run
      wrapper. When supplied, the per-job ``score`` event emitted onto the
      ``run_events`` stream after a successful persist carries it; ad-hoc
      paths (manual rescore, eval, tests) that don't have a run envelope
      fall back to the sentinel ``"scoring:adhoc"`` so the event is still
      produced, just uncorrelated. ``skipped`` / ``error`` results emit no
      event (mirrors the existing pass-through-no-write branch). Issue #215.

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
    candidate_context = _resolve_candidate_context(config)
    result = scorer_fn(job, conn, config, candidate_context=candidate_context)

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
    model = getattr(result, "model", None)

    # P3.1 — deterministic location_fit override (D-6: facts beat judgment).
    # Runs post-LLM, pre-persist: no schema change, no prompt change, no eval
    # gate needed. The override replaces the LLM-emitted location_fit sub-score
    # when structured location facts decide the outcome unambiguously.
    assessment = _apply_location_fit_override(assessment, job, conn, config)

    classification = persist_job_assessment(
        conn,
        dedup_key,
        assessment,
        provider=provider,
        model=model,
        config=config,
    )
    conn.commit()

    # Per-job audit event (issue #215). The F4 substrate already swallows
    # emission errors in run_events._append, so instrumentation can never
    # raise into the scoring path — no try/except needed here. Missing-row
    # silent no-ops return classification=None and skip emission (nothing
    # landed on disk to audit).
    if classification is not None:
        from job_finder.web import run_events

        run_events.mark(
            "score",
            run_id or "scoring:adhoc",
            job="scoring",
            source="orchestrator",
            dedup_key=dedup_key,
            sub_scores=dict(assessment.sub_scores),
            classification=classification,
            provider=provider,
            model=model,
            jd_len=len(job.get("jd_full") or ""),
        )

    return result


def _render_location_targeting(
    work_arrangement: str | None, target_locations: list[str]
) -> list[str]:
    """Render work-arrangement + geographies as a PREFERENCE HIERARCHY.

    The candidate's ``work_arrangement`` is their *preferred* arrangement (a
    ranking, not a hard filter), and the geographic ``target_locations`` are the
    places where a *non-remote* role is acceptable. Two rules keep this honest:

    - The ``"remote"`` token is stripped from the geography list. "Remote" is a
      modality, not a place; it is already carried by ``work_arrangement``. This
      single-sources the treatment with ``location_fit._target_loc_matches``,
      which likewise excludes ``"remote"`` from geography matching.
    - When remote is preferred, a fully-remote role is stated as the IDEAL
      location match and explicitly disqualified from being a "gap".

    This exists because the prior flat rendering ("Target locations: San
    Francisco, Remote") gave the scorer no ordering, so ``qwen2.5:14b`` read the
    first-listed geography as the candidate's preference and inverted it —
    flagging a fully-remote role as the gap "Remote role, candidate prefers San
    Francisco location". Making the hierarchy explicit removes the ambiguity the
    model was resolving incorrectly.
    """
    wa = (work_arrangement or "remote").strip().lower()
    # Keep only real places: drop blanks/None AND the "remote" modality token.
    # This mirrors location_fit._target_loc_matches, which _norm-coerces every
    # entry (None → "") and skips falsy tokens — so a bare "- " list item in a
    # hand-edited config (PyYAML yields None) or an eval-harness config renders
    # cleanly instead of crashing the ", ".join below.
    geos = [
        t.strip()
        for t in target_locations
        if (t or "").strip() and (t or "").strip().lower() != "remote"
    ]
    geo_str = ", ".join(geos) if geos else "Not specified"

    lines: list[str] = [f"- Preferred work arrangement: {wa}"]
    if wa == "remote":
        lines.append(
            "- A fully remote role is the candidate's IDEAL location match "
            "(location_fit 5). Remote is the top preference, NOT a shortcoming — "
            "never list a remote role as a location gap."
        )
        lines.append(
            f"- Acceptable on-site/hybrid geographies (relevant ONLY when the role "
            f"is not remote): {geo_str}. On-site/hybrid in one of these is "
            "acceptable — geography membership overrides the on-site penalty for "
            "location_fit — but a remote role is still preferred over any of them."
        )
    else:
        lines.append(
            f"- Target geographies: {geo_str}. On-site/hybrid in one of these is a "
            "match — geography membership overrides the on-site penalty for "
            "location_fit. A fully remote role is also a strong match."
        )
    return lines


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
    work_arrangement = cfg_profile.get("work_arrangement") or "remote"
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
    parts += _render_location_targeting(work_arrangement, target_locations)
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
