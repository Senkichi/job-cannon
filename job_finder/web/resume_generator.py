"""Sonnet resume generator with closed-world constraint.

Provides:
    RESUME_SCHEMA        -- JSON schema for structured Sonnet resume output.
    STRATEGY_POOL        -- Pool of resume strategy identifiers for multi-version.
    generate_resume_single      -- Generate tailored resume dict via Sonnet.
    generate_resume_background  -- Background thread: full gen + Drive upload.

Multi-version synthesis functions live in resume_multi_version.py:
    generate_resume_multi, _haiku_select_strategies,
    _generate_single_variant, _synthesize_variants

Closed-world constraint: the system prompt explicitly forbids inventing,
inferring, or adding any information not present in the candidate's profile.
Every bullet point must trace back to the profile data.

Background thread pattern follows stale_detector.py: opens its own
sqlite3 connection (not Flask g.db) for APScheduler/thread safety.
"""

import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional

try:
    import anthropic
    _anthropic_available = True
except ImportError:
    anthropic = None  # type: ignore[assignment]
    _anthropic_available = False

from job_finder.config import DEFAULT_MULTI_VERSION_THRESHOLD
from job_finder.web.claude_client import BudgetExceededError
from job_finder.web.db_helpers import standalone_connection
from job_finder.web.model_provider import call_model
from job_finder.web.docx_formatter import build_resume_docx
from job_finder.web.drive_uploader import get_drive_service, upload_to_drive

# Re-export content module symbols so all existing callers importing from this
# module continue to work without modification.
from job_finder.web.resume_content import (  # noqa: F401
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

    try:
        result_obj = call_model(
            tier="sonnet",
            system=_SYSTEM_PROMPT,
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
        logger.info(
            "generate_resume_single: budget exceeded for '%s' @ '%s' -- returning None",
            job_row.get("title"),
            job_row.get("company"),
        )
        return None
    except Exception as exc:
        logger.error(
            "generate_resume_single: call_model failed for '%s' @ '%s': %s",
            job_row.get("title"),
            job_row.get("company"),
            exc,
        )
        raise
    result = result_obj.data

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
# Background thread function
# ---------------------------------------------------------------------------

def generate_resume_background(
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
            raw_score = job_row.get("sonnet_score")
            sonnet_score = float(raw_score) if raw_score is not None else 0.0
            use_multi = sonnet_score >= multi_threshold

            if use_multi:
                # Multi-version synthesis: 3 strategy-focused variants + synthesis pass
                # Note: generate_resume_multi manages its own connections per thread
                # Deferred import avoids circular import at module load time
                from job_finder.web.resume_multi_version import generate_resume_multi
                logger.info(
                    "generate_resume_background: using multi-version synthesis for gen_id=%s "
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
                    logger.info("generate_resume_background: budget exceeded for gen_id=%s", gen_id)
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
                        "generate_resume_background: %d error violations, running fix pass for gen_id=%s",
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
                    "generate_resume_background: validation failed for gen_id=%s: %s", gen_id, e
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
            if not folder_id:
                raise ValueError(
                    "drive.folder_id is not configured — set it in Settings before generating resumes."
                )
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
                "generate_resume_background: done for gen_id=%s, type=%s, url=%s",
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
                logger.exception("generate_resume_background: failed to update error state for gen_id=%s", gen_id)
            logger.warning(
                "generate_resume_background: error for gen_id=%s: %s", gen_id, e
            )


# Backward-compatible alias -- tests and older callers that imported the private name
# still work; new code should use generate_resume_background.
_generate_resume_background = generate_resume_background
