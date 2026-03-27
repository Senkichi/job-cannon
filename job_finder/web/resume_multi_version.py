"""Multi-version resume generation with strategy selection and variant synthesis."""

import json
import logging
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable

import anthropic

from job_finder.web.claude_client import BudgetExceededError, cost_gate
from job_finder.web.db_helpers import standalone_connection
from job_finder.web.model_provider import call_model
from job_finder.web.resume_generator import (
    RESUME_SCHEMA,
    STRATEGY_POOL,
    _STRATEGY_DESCRIPTIONS,
    _RESUME_GUIDELINES,
    _SYSTEM_PROMPT,
    _format_education,
    _format_profile_positions,
    _get_accepted_preferences,
)

logger = logging.getLogger(__name__)


def _haiku_select_strategies(
    client: Any,
    job_row: dict,
    conn,
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
        result_obj = call_model(
            tier="haiku",
            system=system,
            messages=[{"role": "user", "content": user_message}],
            conn=conn,
            config=config,
            output_schema=strategy_schema,
            job_id=job_row.get("dedup_key"),
            purpose="resume_strategy",
            max_tokens=512,
            client=client,
        )
        strategies = result_obj.data.get("strategies", [])
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

        try:
            result_obj = call_model(
                tier="sonnet",
                system=strategy_system,
                messages=[{"role": "user", "content": user_message}],
                conn=conn,
                config=config,
                output_schema=RESUME_SCHEMA,
                job_id=job_row.get("dedup_key"),
                purpose="resume_generation",
                max_tokens=4096,
                client=client,
            )
        except BudgetExceededError:
            raise RuntimeError(
                f"Budget exceeded during variant generation for strategy: {strategy}"
            )
        return result_obj.data


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

        result_obj = call_model(
            tier="sonnet",
            system=synthesis_system,
            messages=[{"role": "user", "content": user_message}],
            conn=conn,
            config=config,
            output_schema=RESUME_SCHEMA,
            job_id=job_row.get("dedup_key"),
            purpose="resume_synthesis",
            max_tokens=4096,
            client=client,
        )
        return result_obj.data
