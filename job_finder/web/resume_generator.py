"""Sonnet resume generator with closed-world constraint.

Provides:
    RESUME_SCHEMA        -- JSON schema for structured Sonnet resume output.
    STRATEGY_POOL        -- Pool of resume strategy identifiers for multi-version.
    generate_resume_single   -- Generate tailored resume dict via Sonnet.
    generate_resume_multi    -- Multi-version synthesis for high-scoring jobs.
    _haiku_select_strategies -- Haiku-based strategy selection from STRATEGY_POOL.
    _generate_single_variant -- Generate one strategy-focused variant (thread-safe).
    _synthesize_variants     -- Sonnet synthesis pass merging best variant sections.
    _generate_resume_background  -- Background thread: full gen + Drive upload.

Closed-world constraint: the system prompt explicitly forbids inventing,
inferring, or adding any information not present in the candidate's profile.
Every bullet point must trace back to the profile data.

Background thread pattern follows stale_detector.py: opens its own
sqlite3 connection (not Flask g.db) for APScheduler/thread safety.

Multi-version synthesis pattern:
  1. Haiku selects 3 strategies from STRATEGY_POOL based on JD fit.
  2. ThreadPoolExecutor runs 3 parallel Sonnet variant generators, each with
     a different strategy emphasis directive prepended to the system prompt.
  3. A final Sonnet synthesis pass selects the best sections from all variants.
  4. Triggered only when sonnet_score >= multi_version_threshold (default 80).
"""

import json
import logging
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import anthropic

from job_finder.config import DEFAULT_MODEL_HAIKU, DEFAULT_MODEL_SONNET, DEFAULT_MULTI_VERSION_THRESHOLD
from job_finder.web.claude_client import call_claude, cost_gate
from job_finder.web.db_helpers import standalone_connection
from job_finder.web.docx_formatter import build_resume_docx
from job_finder.web.drive_uploader import get_drive_service, upload_to_drive

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# JSON schema for structured Sonnet resume output
# ---------------------------------------------------------------------------

RESUME_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "contact_line": {"type": "string"},
        "summary": {
            "type": "string",
            "description": "2-3 sentences tailored to JD",
        },
        "skills": {"type": "array", "items": {"type": "string"}},
        "positions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "company": {"type": "string"},
                    "dates": {"type": "string"},
                    "achievements": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": ["title", "company", "dates", "achievements"],
            },
        },
        "education": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "degree": {"type": "string"},
                    "institution": {"type": "string"},
                    "year": {"type": "string"},
                },
            },
        },
    },
    "required": ["name", "summary", "skills", "positions"],
    "additionalProperties": False,
}

# ---------------------------------------------------------------------------
# Multi-version strategy pool
# ---------------------------------------------------------------------------

STRATEGY_POOL = [
    "impact_focused",      # Lead with quantified business outcomes
    "technical_depth",     # Emphasize technical architecture and system complexity
    "leadership_scope",    # Emphasize team/org/stakeholder leadership and mentoring
    "problem_solver",      # Frame as identifying problems and delivering solutions
    "cross_functional",    # Highlight cross-team collaboration and influence
]

# Human-readable descriptions for each strategy (used in system prompt additions)
_STRATEGY_DESCRIPTIONS = {
    "impact_focused": (
        "Lead with quantified business outcomes. Every bullet should start with a metric "
        "or result: revenue impact, user growth, latency reduction, cost savings. "
        "Frame the candidate as a business-outcome driver."
    ),
    "technical_depth": (
        "Emphasize technical architecture and system complexity. Highlight scale, "
        "system design decisions, engineering tradeoffs, and technical leadership. "
        "Frame the candidate as an expert engineer who solves hard problems."
    ),
    "leadership_scope": (
        "Emphasize team, org, and stakeholder leadership. Highlight mentoring, "
        "cross-functional coordination, influencing direction, and growing others. "
        "Frame the candidate as a leader who multiplies the team."
    ),
    "problem_solver": (
        "Frame the candidate as someone who identifies problems and delivers solutions. "
        "Lead each bullet with the problem context, then the solution approach, then result. "
        "Show initiative, ownership, and end-to-end delivery."
    ),
    "cross_functional": (
        "Highlight cross-team collaboration and influence without authority. "
        "Emphasize partnerships, alignment across orgs, driving consensus, "
        "and delivering outcomes that required coordinating multiple stakeholders."
    ),
}


# ---------------------------------------------------------------------------
# System prompt with closed-world constraint + distilled writing guidelines
# ---------------------------------------------------------------------------

_RESUME_GUIDELINES = (
    "\n\n"
    "## RESUME WRITING GUIDELINES\n\n"

    "### SOURCE FIDELITY (highest priority rule)\n"
    "Never list a skill, tool, or technology the candidate has not actually used. "
    "Never fabricate achievements, companies, or experiences. "
    "Do NOT add tools to match the JD — if the JD asks for Looker and the candidate "
    "uses Tableau, list Tableau. "
    "Gap mitigation: use the candidate's closest real analog, positioned to address "
    "the same underlying competency. Every bullet must trace back to profile data.\n\n"

    "### PROFESSIONAL SUMMARY\n"
    "3-4 sentences maximum. Formula: (1) Role archetype + years + context. "
    "(2) Strongest achievement with a number. "
    "(3) 2-3 JD capabilities + value prop for this role. "
    "Mirror the JD's title/archetype language in the opening. "
    "Never use the word 'seeking'. Keep to 3-4 rendered lines; cut if longer.\n\n"

    "### SKILLS SECTION\n"
    "Hard skills and methodologies ONLY. Never list soft skills "
    "(no 'Cross-Functional Collaboration', 'Stakeholder Communication', 'Team Leadership'). "
    "Soft skills belong in experience bullets, demonstrated through action. "
    "Front-load skills to JD priority order. 1-2 lines maximum, pipe-separated.\n\n"

    "### BULLET WRITING FORMULA\n"
    "Every bullet: Action Verb + What You Did + How/With What + Quantified Impact. "
    "Lead with strong verbs (Designed, Engineered, Architected, Directed, Built, Led). "
    "Rotate verbs — never start two consecutive bullets with the same verb. "
    "Quantify aggressively: user counts, revenue, % improvements, time savings. "
    "1-2 lines per bullet (3 lines absolute max, rare). "
    "Every bullet must pass the 'so what?' test — result must be clear.\n"
    "Anti-patterns to eliminate: (a) 'problem-identified' openers that burn half the "
    "bullet on context ('Identified lack of...', 'Recognized that...') — lead with action; "
    "(b) methods-listing without business outcome; "
    "(c) two bullets both demonstrating the same dimension — vary them; "
    "(d) soft skill claims as standalone bullets.\n\n"

    "### BULLET COUNT BY SENIORITY\n"
    "Most recent/current role (Lead/Senior): 4-6 bullets. "
    "Previous role at same company: 2-3 bullets. "
    "Prior companies (mid-career): 1-2 bullets each. "
    "Early career: 1 bullet maximum.\n\n"

    "### CONFIDENTIALITY\n"
    "Never include specific client name in resume bullets. "
    "Use generic descriptors: 'a major enterprise client', 'a Fortune 500 financial services client'. "
    "Client names may exist in profile for context but must never surface in output. "
    "Omit specific team sizes unless the JD explicitly requires them.\n\n"

    "### TYPOGRAPHY\n"
    "No bold text within bullet point content (bold reserved for headers, company names, titles). "
    "No em dash anywhere in the document — restructure using commas or semicolons instead. "
    "Minimize parentheses; integrate details naturally. "
    "Do not define well-known acronyms (ITT, DiD, RCT, ROI, KPI, ETL).\n\n"

    "### JD MIRRORING\n"
    "Use the JD's exact terminology for tools and methodologies. "
    "Ensure each of the top 5-7 JD keywords appears at least once. "
    "Never lift full phrases verbatim from the JD. "
    "Use a JD phrase at most once; never repeat the same JD phrase across the resume. "
    "The reader should feel alignment, not pattern-matching.\n\n"

    "### PRE-DELIVERY CHECKS\n"
    "Before finalizing, verify: "
    "no fabricated skills or tools; "
    "no client names anywhere in the document; "
    "all employment dates match profile data exactly; "
    "professional summary is 3-4 sentences; "
    "skills section is 1-2 lines; "
    "most recent role has 4-6 bullets with progressively fewer for earlier roles; "
    "no em dashes; "
    "no bold in bullet content; "
    "every bullet has a quantified result or compelling business outcome.\n"
)

_SYSTEM_PROMPT = (
    "You are a professional resume writer. Generate a tailored resume for the candidate "
    "applying to this specific job. "
    "CRITICAL CONSTRAINT: You must ONLY use information from the candidate's profile below. "
    "You may rephrase, reframe, and reorder content, but you must NEVER invent, infer, or add "
    "achievements, skills, companies, or experiences not present in the profile. "
    "Every bullet point must trace back to the profile data."
    + _RESUME_GUIDELINES
)


# ---------------------------------------------------------------------------
# Helper: accepted preferences query
# ---------------------------------------------------------------------------

def _get_accepted_preferences(conn: sqlite3.Connection) -> list:
    """Return accepted, unconsumed preference texts for resume prompt injection.

    Reads from resume_preferences_detected WHERE accepted=1 AND applied_at IS NULL.
    Returns empty list gracefully if table does not exist (test DBs, older schemas).
    """
    try:
        rows = conn.execute(
            "SELECT preference_text FROM resume_preferences_detected "
            "WHERE accepted = 1 AND applied_at IS NULL "
            "ORDER BY preference_type, detected_at"
        ).fetchall()
        return [row[0] if isinstance(row, tuple) else row["preference_text"] for row in rows]
    except Exception:
        # Table may not exist in test DBs or older schemas — degrade gracefully
        logger.debug("Failed to load resume preferences (non-fatal)", exc_info=True)
        return []


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_resume_single(
    client: Any,
    job_row: dict,
    profile: dict,
    conn: sqlite3.Connection,
    config: dict,
) -> Optional[dict]:
    """Generate a tailored resume dict via Sonnet with closed-world constraint.

    Args:
        client: Anthropic client instance (injected for testability).
        job_row: Job record dict. Must include jd_full, title, company.
        profile: Experience profile dict (from experience_profile.json).
        conn: Open SQLite connection for cost recording.
        config: Application config dict (reads scoring.models.sonnet).

    Returns:
        Structured resume dict matching RESUME_SCHEMA, or None if budget exceeded.
    """
    # Budget gate -- callers decide what to do on False
    if not cost_gate(conn, config, "sonnet"):
        logger.info(
            "generate_resume_single: budget exceeded for '%s' @ '%s' -- returning None",
            job_row.get("title"),
            job_row.get("company"),
        )
        return None

    model = (
        config.get("scoring", {})
        .get("models", {})
        .get("sonnet", DEFAULT_MODEL_SONNET)
    )

    # Build fit_analysis context from job_row if present
    fit_analysis = job_row.get("fit_analysis")
    priority_skills: list[str] = []
    if fit_analysis:
        if isinstance(fit_analysis, str):
            try:
                fit_analysis = json.loads(fit_analysis)
            except (json.JSONDecodeError, TypeError):
                fit_analysis = {}
        priority_skills = fit_analysis.get("resume_priority_skills", [])

    # Build profile text
    positions_text = _format_profile_positions(profile)
    skills = profile.get("skills", [])
    skills_text = ", ".join(skills) if skills else "Not specified"

    # Build contact line from profile if available
    prefs = profile.get("resume_preferences", {})
    contact_hint = prefs.get("contact_line", "")

    user_message = (
        f"## Job Description\n\n"
        f"**Title:** {job_row.get('title', 'Unknown')}\n"
        f"**Company:** {job_row.get('company', 'Unknown')}\n\n"
        f"{job_row.get('jd_full', '')}\n\n"
        f"---\n\n"
        f"## Candidate Experience Profile\n\n"
        f"**Key Skills:** {skills_text}\n"
        f"**Positions:**{positions_text}\n\n"
        f"**Education:**{_format_education(profile)}\n\n"
    )

    if priority_skills:
        user_message += (
            f"## Resume Priority Skills (from fit analysis)\n"
            f"Prioritize these skills in the skills section: {', '.join(priority_skills)}\n\n"
        )

    if fit_analysis and isinstance(fit_analysis, dict):
        strengths = fit_analysis.get("strengths", [])
        if strengths:
            user_message += (
                f"## Candidate Strengths for This Role\n"
                f"{chr(10).join(f'- {s}' for s in strengths)}\n\n"
            )

    # Inject style guide directives + accepted Drive feedback at same priority level
    from job_finder.web.resume_style_guide import load_style_guide, _build_style_guide_directives
    style_guide = load_style_guide()
    style_directives = _build_style_guide_directives(style_guide)
    accepted_prefs = _get_accepted_preferences(conn)
    all_formatting = style_directives + accepted_prefs
    if all_formatting:
        pref_lines = "\n".join(f"- {p}" for p in all_formatting)
        user_message += (
            f"## Formatting Preferences\n"
            f"Apply these formatting preferences (soft guidelines -- "
            f"JD requirements and fit analysis take priority):\n"
            f"{pref_lines}\n\n"
        )

    user_message += (
        "## Instructions\n"
        "- List positions in reverse chronological order\n"
        "- Write 3-5 achievement bullets per position, each matched to JD requirements\n"
        "- Order skills list with JD keywords and priority skills first\n"
        "- Write a 2-3 sentence professional summary emphasizing strengths relevant to this role\n"
        "- Education: degree, institution, year only (brief)\n"
    )

    if contact_hint:
        user_message += f"- Contact line: {contact_hint}\n"

    result, _cost = call_claude(
        client=client,
        model=model,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
        output_schema=RESUME_SCHEMA,
        conn=conn,
        job_id=job_row.get("dedup_key"),
        purpose="resume_generation",
        config=config,
        max_tokens=4096,
    )

    logger.debug(
        "generate_resume_single: generated resume for '%s' @ '%s'",
        job_row.get("title"),
        job_row.get("company"),
    )

    # --- Inline validation for quick-apply path ---
    from job_finder.web.resume_validator import validate_resume as _validate, fix_resume_violations as _fix
    try:
        jd_text = job_row.get("jd_full", "")
        audit = _validate(result, jd_text, profile, conn, config)
        has_errors = any(v.get("severity") == "error" for v in audit.get("violations", []))
        if has_errors:
            logger.info(
                "generate_resume_single: %d error violations found, running fix pass",
                sum(1 for v in audit["violations"] if v.get("severity") == "error"),
            )
            result = _fix(result, audit["violations"], profile, conn, config)
    except Exception as e:
        logger.warning("generate_resume_single: inline validation failed: %s", e)

    return result


# ---------------------------------------------------------------------------
# Multi-version synthesis functions
# ---------------------------------------------------------------------------

def _haiku_select_strategies(
    client: Any,
    job_row: dict,
    conn: sqlite3.Connection,
    config: dict,
) -> list[str]:
    """Select 3 strategies from STRATEGY_POOL using Haiku based on the JD.

    Args:
        client: Anthropic client instance (injected for testability).
        job_row: Job record dict. Must include jd_full, title, company.
        conn: Open SQLite connection for cost recording.
        config: Application config dict (reads scoring.models.haiku).

    Returns:
        List of exactly 3 strategy identifier strings from STRATEGY_POOL.
        Falls back to first 3 from STRATEGY_POOL if Haiku call fails.
    """
    model = (
        config.get("scoring", {})
        .get("models", {})
        .get("haiku", DEFAULT_MODEL_HAIKU)
    )

    # Build strategy descriptions for the prompt
    strategy_list = "\n".join(
        f"- {name}: {_STRATEGY_DESCRIPTIONS.get(name, name)}"
        for name in STRATEGY_POOL
    )

    system = (
        "You are a resume strategy advisor. Given a job description, select the 3 most "
        "effective resume strategies from the available pool that will best highlight the "
        "candidate's fit. Return exactly the strategy identifiers as listed."
    )
    user_message = (
        f"## Job Description\n\n"
        f"**Title:** {job_row.get('title', 'Unknown')}\n"
        f"**Company:** {job_row.get('company', 'Unknown')}\n\n"
        f"{job_row.get('jd_full', '')}\n\n"
        f"---\n\n"
        f"## Available Strategies\n\n{strategy_list}\n\n"
        f"Select the 3 strategies that best match this job's requirements."
    )

    strategy_schema = {
        "type": "object",
        "properties": {
            "strategies": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Exactly 3 strategy identifiers from the available pool",
            },
            "reasoning": {
                "type": "string",
                "description": "Brief explanation of why these strategies fit this role",
            },
        },
        "required": ["strategies", "reasoning"],
        "additionalProperties": False,
    }

    try:
        result, _cost = call_claude(
            client=client,
            model=model,
            system=system,
            messages=[{"role": "user", "content": user_message}],
            output_schema=strategy_schema,
            conn=conn,
            job_id=job_row.get("dedup_key"),
            purpose="resume_strategy",
            config=config,
            max_tokens=512,
        )
        strategies = result.get("strategies", [])
        # Validate: ensure we got 3 valid strategy identifiers
        valid = [s for s in strategies if s in STRATEGY_POOL]
        if len(valid) >= 3:
            return valid[:3]
        # If fewer valid, fill from STRATEGY_POOL
        for s in STRATEGY_POOL:
            if s not in valid:
                valid.append(s)
            if len(valid) == 3:
                break
        return valid[:3]
    except Exception as e:
        logger.warning("_haiku_select_strategies: Haiku call failed, using fallback: %s", e)
        return STRATEGY_POOL[:3]


def _generate_single_variant(
    db_path: str,
    client_factory: Callable,
    job_row: dict,
    profile: dict,
    strategy: str,
    config: dict,
) -> dict:
    """Generate one strategy-focused resume variant (thread-safe).

    Opens its own SQLite connection and creates its own Anthropic client for
    thread safety (per architecture decision: each background thread owns its
    own connections, following stale_detector.py pattern).

    Args:
        db_path: Path to the SQLite database file.
        client_factory: Callable that returns an Anthropic client instance.
        job_row: Job record dict.
        profile: Experience profile dict.
        strategy: Strategy identifier from STRATEGY_POOL.
        config: Application config dict.

    Returns:
        Structured resume dict matching RESUME_SCHEMA.

    Raises:
        Exception: Any error from generate_resume_single propagates up to
            generate_resume_multi for partial-failure handling.
    """
    with standalone_connection(db_path) as conn:
        client = client_factory()

        # Build strategy-specific system prompt
        strategy_desc = _STRATEGY_DESCRIPTIONS.get(strategy, strategy)
        strategy_system = (
            f"{_SYSTEM_PROMPT} "
            f"STRATEGY EMPHASIS: {strategy_desc}. "
            f"Weight your achievement selection and summary framing toward this angle."
        )

        model = (
            config.get("scoring", {})
            .get("models", {})
            .get("sonnet", DEFAULT_MODEL_SONNET)
        )

        # Check budget gate
        if not cost_gate(conn, config, "sonnet"):
            raise RuntimeError(
                f"Budget exceeded during variant generation for strategy: {strategy}"
            )

        # Build the same user message as generate_resume_single
        fit_analysis = job_row.get("fit_analysis")
        priority_skills: list[str] = []
        if fit_analysis:
            if isinstance(fit_analysis, str):
                try:
                    fit_analysis = json.loads(fit_analysis)
                except (json.JSONDecodeError, TypeError):
                    fit_analysis = {}
            priority_skills = fit_analysis.get("resume_priority_skills", [])

        positions_text = _format_profile_positions(profile)
        skills = profile.get("skills", [])
        skills_text = ", ".join(skills) if skills else "Not specified"

        prefs = profile.get("resume_preferences", {})
        contact_hint = prefs.get("contact_line", "")

        user_message = (
            f"## Job Description\n\n"
            f"**Title:** {job_row.get('title', 'Unknown')}\n"
            f"**Company:** {job_row.get('company', 'Unknown')}\n\n"
            f"{job_row.get('jd_full', '')}\n\n"
            f"---\n\n"
            f"## Candidate Experience Profile\n\n"
            f"**Key Skills:** {skills_text}\n"
            f"**Positions:**{positions_text}\n\n"
        )

        if priority_skills:
            user_message += (
                f"## Resume Priority Skills (from fit analysis)\n"
                f"Prioritize these skills in the skills section: {', '.join(priority_skills)}\n\n"
            )

        if fit_analysis and isinstance(fit_analysis, dict):
            strengths = fit_analysis.get("strengths", [])
            if strengths:
                user_message += (
                    f"## Candidate Strengths for This Role\n"
                    f"{chr(10).join(f'- {s}' for s in strengths)}\n\n"
                )

        # Inject style guide directives + accepted Drive feedback at same priority level
        from job_finder.web.resume_style_guide import load_style_guide, _build_style_guide_directives
        style_guide = load_style_guide()
        style_directives = _build_style_guide_directives(style_guide)
        accepted_prefs = _get_accepted_preferences(conn)
        all_formatting = style_directives + accepted_prefs
        if all_formatting:
            pref_lines = "\n".join(f"- {p}" for p in all_formatting)
            user_message += (
                f"## Formatting Preferences\n"
                f"Apply these formatting preferences (soft guidelines -- "
                f"JD requirements and fit analysis take priority):\n"
                f"{pref_lines}\n\n"
            )

        user_message += (
            "## Instructions\n"
            "- List positions in reverse chronological order\n"
            "- Write 3-5 achievement bullets per position, each matched to JD requirements\n"
            "- Order skills list with JD keywords and priority skills first\n"
            "- Write a 2-3 sentence professional summary emphasizing strengths relevant to this role\n"
            "- Education: degree, institution, year only (brief)\n"
        )

        if contact_hint:
            user_message += f"- Contact line: {contact_hint}\n"

        result, _cost = call_claude(
            client=client,
            model=model,
            system=strategy_system,
            messages=[{"role": "user", "content": user_message}],
            output_schema=RESUME_SCHEMA,
            conn=conn,
            job_id=job_row.get("dedup_key"),
            purpose="resume_generation",
            config=config,
            max_tokens=4096,
        )
        return result


def generate_resume_multi(
    db_path: str,
    job_row: dict,
    profile: dict,
    config: dict,
) -> dict:
    """Generate multi-version synthesis resume for high-scoring jobs.

    Workflow:
    1. Haiku selects 3 strategies from STRATEGY_POOL based on JD fit.
    2. ThreadPoolExecutor runs 3 parallel Sonnet variant generators.
    3. Synthesis Sonnet pass merges best sections from all succeeded variants.

    Args:
        db_path: Path to the SQLite database file.
        job_row: Job record dict. Must include jd_full, title, company.
        profile: Experience profile dict (from experience_profile.json).
        config: Application config dict.

    Returns:
        Synthesized resume dict matching RESUME_SCHEMA.

    Raises:
        RuntimeError: If all 3 variant generators fail.
    """
    # Step 1: Haiku selects 3 strategies
    with standalone_connection(db_path) as strategy_conn:
        strategy_client = anthropic.Anthropic()
        strategies = _haiku_select_strategies(strategy_client, job_row, strategy_conn, config)

    logger.debug(
        "generate_resume_multi: selected strategies %s for '%s' @ '%s'",
        strategies,
        job_row.get("title"),
        job_row.get("company"),
    )

    # Step 2: Parallel Sonnet variant generation
    def client_factory() -> Any:
        return anthropic.Anthropic()

    variants: list[dict] = []
    futures_to_strategy: dict = {}

    with ThreadPoolExecutor(max_workers=3) as executor:
        for strategy in strategies:
            future = executor.submit(
                _generate_single_variant,
                db_path,
                client_factory,
                job_row,
                profile,
                strategy,
                config,
            )
            futures_to_strategy[future] = strategy

        for future in as_completed(futures_to_strategy):
            strategy = futures_to_strategy[future]
            try:
                result = future.result()
                variants.append(result)
                logger.debug("generate_resume_multi: variant '%s' succeeded", strategy)
            except Exception as e:
                logger.warning(
                    "generate_resume_multi: variant '%s' failed: %s", strategy, e
                )

    if not variants:
        raise RuntimeError("All resume variants failed")

    logger.debug(
        "generate_resume_multi: %d/%d variants succeeded, running synthesis",
        len(variants),
        len(strategies),
    )

    # Step 3: Synthesis pass
    return _synthesize_variants(db_path, variants, job_row, config)


def _synthesize_variants(
    db_path: str,
    variants: list[dict],
    job_row: dict,
    config: dict,
) -> dict:
    """Synthesis Sonnet pass: merge best sections from all variant resumes.

    Opens its own SQLite connection and Anthropic client (thread-safe).

    Args:
        db_path: Path to the SQLite database file.
        variants: List of resume dicts (each matching RESUME_SCHEMA).
        job_row: Job record dict (for JD context).
        config: Application config dict.

    Returns:
        Final synthesized resume dict matching RESUME_SCHEMA.
    """
    with standalone_connection(db_path) as conn:
        client = anthropic.Anthropic()

        model = (
            config.get("scoring", {})
            .get("models", {})
            .get("sonnet", DEFAULT_MODEL_SONNET)
        )

        synthesis_system = (
            "You are a resume editor. You have multiple resume variants for the same candidate "
            "and job. Select the BEST professional summary, the BEST achievement bullets for "
            "each position, and the optimal skill ordering from across all variants. Produce one "
            "final resume that combines the strongest elements. "
            "CRITICAL CONSTRAINT: Maintain the closed-world constraint -- do not add any content "
            "not present in the variants. You may only select and combine existing content."
        )

        # Build numbered variant sections
        variants_text = ""
        for i, variant in enumerate(variants, 1):
            variants_text += f"\n\n## Variant {i}\n\n"
            variants_text += json.dumps(variant, indent=2)

        user_message = (
            f"## Original Job Description\n\n"
            f"**Title:** {job_row.get('title', 'Unknown')}\n"
            f"**Company:** {job_row.get('company', 'Unknown')}\n\n"
            f"{job_row.get('jd_full', '')}\n\n"
            f"---\n\n"
            f"## Resume Variants to Synthesize\n"
            f"{variants_text}\n\n"
            f"---\n\n"
            f"## Instructions\n"
            f"- Select the strongest professional summary from the variants\n"
            f"- For each position, select the best achievement bullets across all variants\n"
            f"- Produce the optimal skills ordering (JD keywords first)\n"
            f"- Output a single unified resume combining the best elements\n"
        )

        result, _cost = call_claude(
            client=client,
            model=model,
            system=synthesis_system,
            messages=[{"role": "user", "content": user_message}],
            output_schema=RESUME_SCHEMA,
            conn=conn,
            job_id=job_row.get("dedup_key"),
            purpose="resume_synthesis",
            config=config,
            max_tokens=4096,
        )
        return result


# ---------------------------------------------------------------------------
# Background thread function
# ---------------------------------------------------------------------------

def _generate_resume_background(
    db_path: str,
    gen_id: int,
    job_row: dict,
    profile: dict,
    config: dict,
) -> None:
    """Background thread: generate resume, format .docx, upload to Drive.

    Opens its own sqlite3 connection -- NOT Flask g.db. This is safe for
    background threads and APScheduler jobs (per project architecture decision).

    Status transitions:
        pending -> generating -> done (on success)
        pending -> generating -> error (on failure or budget exceeded)

    Args:
        db_path: Path to the SQLite database file.
        gen_id: ID of the resume_generations row to update.
        job_row: Job record dict.
        profile: Experience profile dict.
        config: Application config dict.
    """
    with standalone_connection(db_path) as conn:
        try:
            # Transition: pending -> generating
            conn.execute(
                "UPDATE resume_generations SET status = 'generating' WHERE id = ?",
                (gen_id,),
            )
            conn.commit()

            # Determine dispatch: single or multi based on sonnet_score threshold
            multi_threshold = (
                config.get("scoring", {}).get("multi_version_threshold", DEFAULT_MULTI_VERSION_THRESHOLD)
            )
            sonnet_score = float(job_row.get("sonnet_score") or 0.0)
            use_multi = sonnet_score >= multi_threshold

            if use_multi:
                # Multi-version synthesis: 3 strategy-focused variants + synthesis pass
                # Note: generate_resume_multi manages its own connections per thread
                logger.info(
                    "_generate_resume_background: using multi-version synthesis for gen_id=%s "
                    "(sonnet_score=%.1f >= threshold=%d)",
                    gen_id, sonnet_score, multi_threshold,
                )
                resume_data = generate_resume_multi(db_path, job_row, profile, config)
                generation_type = "multi"
            else:
                # Single-pass generation
                client = anthropic.Anthropic()
                resume_data = generate_resume_single(client, job_row, profile, conn, config)
                generation_type = "single"

                if resume_data is None:
                    # Budget exceeded
                    conn.execute(
                        "UPDATE resume_generations SET status = 'error', error_msg = ? WHERE id = ?",
                        ("Monthly budget exceeded", gen_id),
                    )
                    conn.commit()
                    logger.info("_generate_resume_background: budget exceeded for gen_id=%s", gen_id)
                    return

            # --- Validate generated resume ---
            from job_finder.web.resume_validator import validate_resume, fix_resume_violations
            import json as _json

            validation_report = None
            try:
                jd_text = job_row.get("jd_full", "")
                validation_report = validate_resume(resume_data, jd_text, profile, conn, config)

                # Save validation report to DB
                conn.execute(
                    "UPDATE resume_generations SET validation_report = ? WHERE id = ?",
                    (_json.dumps(validation_report), gen_id),
                )
                conn.commit()

                # Auto-fix if error-severity violations found
                has_errors = any(
                    v.get("severity") == "error"
                    for v in validation_report.get("violations", [])
                )
                if has_errors:
                    logger.info(
                        "_generate_resume_background: %d error violations, running fix pass for gen_id=%s",
                        sum(1 for v in validation_report["violations"] if v.get("severity") == "error"),
                        gen_id,
                    )
                    fixed_resume = fix_resume_violations(
                        resume_data,
                        validation_report["violations"],
                        profile,
                        conn,
                        config,
                    )
                    # Update validation report with fix info
                    validation_report["fix_applied"] = True
                    validation_report["original_resume_skills"] = resume_data.get("skills", [])
                    validation_report["fixed_resume_skills"] = fixed_resume.get("skills", [])
                    resume_data = fixed_resume

                    # Update the stored report with fix info
                    conn.execute(
                        "UPDATE resume_generations SET validation_report = ? WHERE id = ?",
                        (_json.dumps(validation_report), gen_id),
                    )
                    conn.commit()
            except Exception as e:
                logger.warning(
                    "_generate_resume_background: validation failed for gen_id=%s: %s", gen_id, e
                )

            # Format as .docx
            docx_buffer = build_resume_docx(resume_data)

            # Build document name: "Company - Title - YYYY-MM-DD"
            company = job_row.get("company", "Unknown")
            title = job_row.get("title", "Resume")
            date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            doc_name = f"{company} - {title} - {date_str}"

            # Upload to Drive
            drive_service = get_drive_service()
            folder_id = config.get("drive", {}).get("folder_id", "")
            convert_to_gdoc = config.get("drive", {}).get("convert_to_gdoc", True)

            doc_url = upload_to_drive(
                drive_service,
                doc_name,
                docx_buffer,
                folder_id=folder_id,
                convert_to_gdoc=convert_to_gdoc,
            )

            # Transition: generating -> done (update generation_type alongside status)
            conn.execute(
                "UPDATE resume_generations SET status = 'done', doc_url = ?, generation_type = ? WHERE id = ?",
                (doc_url, generation_type, gen_id),
            )
            conn.commit()
            logger.info(
                "_generate_resume_background: done for gen_id=%s, type=%s, url=%s",
                gen_id, generation_type, doc_url,
            )

        except Exception as e:
            error_msg = str(e)[:500]
            try:
                conn.execute(
                    "UPDATE resume_generations SET status = 'error', error_msg = ? WHERE id = ?",
                    (error_msg, gen_id),
                )
                conn.commit()
            except Exception:
                logger.exception("_generate_resume_background: failed to update error state for gen_id=%s", gen_id)
            logger.warning(
                "_generate_resume_background: error for gen_id=%s: %s", gen_id, e
            )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _format_education(profile: dict) -> str:
    """Format education from profile into a readable text block for the prompt."""
    education = profile.get("education", [])
    if not education:
        return "\n  Not specified"

    text = ""
    for ed in education:
        degree = ed.get("degree", "")
        institution = ed.get("institution", "")
        graduation = ed.get("graduation", "")
        text += f"\n  - {degree} — {institution} ({graduation})"
        if ed.get("thesis"):
            text += f" | Thesis: {ed['thesis']}"
    return text


def _format_profile_positions(profile: dict) -> str:
    """Format positions from profile into a readable text block for the prompt."""
    positions = profile.get("positions", [])
    if not positions:
        return "\n  None listed"

    text = ""
    for pos in positions:
        p_title = pos.get("title", "")
        p_company = pos.get("company", "")
        start = pos.get("start_date", "")
        end = pos.get("end_date", "Present") or "Present"
        achievements = pos.get("achievements", [])
        skills = pos.get("skills", [])

        achievements_text = (
            "\n".join(f"  - {a}" for a in achievements) if achievements else "  None listed"
        )
        text += (
            f"\n  Role: {p_title} at {p_company} ({start} - {end})\n"
            f"  Skills: {', '.join(skills)}\n"
            f"  Achievements:\n{achievements_text}"
        )
    return text
