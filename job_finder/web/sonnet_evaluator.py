"""Sonnet deep evaluator for job postings.

Produces a comprehensive fit analysis for jobs that passed the Haiku fast-filter.
Unlike Haiku (which works from a description snippet), Sonnet reads the full job
description (jd_full) and produces actionable guidance:
- A 0-100 fit score
- A 2-3 sentence evaluation summary
- Structured fit analysis with strengths, gaps, talking points, and resume skills

Sonnet evaluation is:
- Budget-gated: skipped when monthly cap is reached
- JD-required: returns None when jd_full is absent
- Cost-tracked: records cost with purpose="sonnet_eval"
- Preference-aware: evaluates both competency ("can do") AND preference alignment
  ("wants to do") using target_titles, target_locations, min_salary, and industries
  from config.yaml's profile section.

Exports:
    SONNET_SCHEMA: JSON schema for structured Sonnet output.
    evaluate_job_sonnet: Evaluate a job row against candidate profile using Sonnet.
"""

import logging
from typing import Any

from job_finder.config import DEFAULT_MODEL_SONNET
from job_finder.web.claude_client import BudgetExceededError, ClaudeContext, call_claude
from job_finder.web.scoring_types import JobRow, ScoringResult, format_salary_range

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Structured output schema for Sonnet evaluation
# ---------------------------------------------------------------------------

SONNET_SCHEMA = {
    "type": "object",
    "properties": {
        "score": {
            "type": "integer",
            "description": "Overall fit score 0-100",
        },
        "summary": {
            "type": "string",
            "description": "2-3 sentence evaluation summary",
        },
        "fit_analysis": {
            "type": "object",
            "properties": {
                "strengths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Candidate strengths for this role",
                },
                "gaps": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Gaps or missing qualifications",
                },
                "talking_points": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Key points to emphasize in application",
                },
                "resume_priority_skills": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Skills to highlight on resume for this job",
                },
            },
            "required": ["strengths", "gaps", "talking_points", "resume_priority_skills"],
            "additionalProperties": False,
        },
    },
    "required": ["score", "summary", "fit_analysis"],
    "additionalProperties": False,
}

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = (
    "You are a senior career advisor evaluating job fit. Analyze the full job description "
    "against the candidate's experience profile. Be specific about strengths (cite concrete "
    "experience), gaps (be honest but constructive), and resume priority skills (what to "
    "emphasize for this specific role). Score calibration: 80+ = excellent fit, apply "
    "immediately; 65-79 = good fit worth applying; 50-64 = partial fit; <50 = poor fit."
)

def evaluate_job_sonnet(
    job_row: JobRow,
    experience_profile: dict,
    conn: Any,
    config: dict,
    *,
    ctx: ClaudeContext | None = None,
) -> ScoringResult:
    """Evaluate a job against the candidate profile using Claude Sonnet.

    Reads the full job description (jd_full) and produces a comprehensive fit
    analysis.

    Args:
        job_row: Job record dict. Must include jd_full (str or None), plus
                 title, company, location, salary_min, salary_max.
        experience_profile: Experience profile dict (from experience_profile.json).
        conn: Open SQLite connection for cost recording.
            Ignored when *ctx* is provided.
        config: Application config dict (reads scoring.models.sonnet and
                profile section for candidate preferences).
            Ignored when *ctx* is provided.
        ctx: ClaudeContext bundling (conn, config).  When supplied,
            the individual conn/config parameters are ignored.

    Returns:
        ScoringResult with status='success' and data dict containing score,
        summary, fit_analysis. On failure: status='skipped' (jd_full absent),
        status='budget_exceeded', or status='error', with data=None.
    """
    # Resolve context: prefer ctx fields over individual params
    if ctx is not None:
        conn = ctx.conn
        config = ctx.config

    jd_full = job_row.get("jd_full")
    if not jd_full:
        logger.debug(
            "Sonnet eval skipped for '%s' @ '%s': jd_full is absent",
            job_row.get("title"),
            job_row.get("company"),
        )
        return ScoringResult(data=None, status="skipped")

    model = (
        config.get("scoring", {})
        .get("models", {})
        .get("sonnet", DEFAULT_MODEL_SONNET)
    )

    # Build salary string
    salary_min = job_row.get("salary_min")
    salary_max = job_row.get("salary_max")
    salary_str = format_salary_range(salary_min, salary_max)

    # Build experience profile section
    positions = experience_profile.get("positions", [])
    skills = experience_profile.get("skills", [])
    education = experience_profile.get("education", [])

    positions_text = ""
    for pos in positions:
        title = pos.get("title", "")
        company = pos.get("company", "")
        achievements = pos.get("achievements", [])
        pos_skills = pos.get("skills", [])
        achievements_text = "\n".join(f"  - {a}" for a in achievements) if achievements else "  None listed"
        positions_text += (
            f"\n  Role: {title} at {company}\n"
            f"  Skills: {', '.join(pos_skills)}\n"
            f"  Achievements:\n{achievements_text}"
        )

    skills_text = ", ".join(skills) if skills else "Not specified"

    # === Candidate Preferences (from config.yaml profile section) ===
    profile_prefs = config.get("profile", {})
    pref_target_titles = profile_prefs.get("target_titles", [])
    pref_target_locations = profile_prefs.get("target_locations", [])
    pref_min_salary = profile_prefs.get("min_salary")
    pref_industries = profile_prefs.get("industries", [])

    pref_titles_str = ", ".join(pref_target_titles) if pref_target_titles else "Not specified"
    pref_locations_str = ", ".join(pref_target_locations) if pref_target_locations else "Not specified"
    pref_salary_str = f"${pref_min_salary:,}" if pref_min_salary else "Not specified"
    pref_industries_str = ", ".join(pref_industries) if pref_industries else "Not specified"

    user_message = (
        f"## Full Job Description\n\n"
        f"**Title:** {job_row.get('title', 'Unknown Title')}\n"
        f"**Company:** {job_row.get('company', 'Unknown Company')}\n"
        f"**Location:** {job_row.get('location', 'Unknown Location')}\n"
        f"**Salary:** {salary_str}\n\n"
        f"{jd_full}\n\n"
        f"---\n\n"
        f"## Candidate Experience Profile\n\n"
        f"**Key Skills:** {skills_text}\n"
        f"**Positions:**{positions_text}\n\n"
        f"**Education:**\n"
        + (
            "\n".join(
                f"  - {ed.get('degree', '')} — {ed.get('institution', '')} ({ed.get('graduation', '')})"
                + (f" | Thesis: {ed['thesis']}" if ed.get("thesis") else "")
                for ed in education
            )
            if education
            else "  Not specified"
        )
        + "\n\n"
        f"## Candidate Preferences\n\n"
        f"**Target Titles:** {pref_titles_str}\n"
        f"**Target Locations:** {pref_locations_str}\n"
        f"**Minimum Salary:** {pref_salary_str}\n"
        f"**Target Industries:** {pref_industries_str}\n\n"
        f"Evaluate the candidate's fit for this role. Consider both competency match "
        f"(skills, experience) AND preference alignment (title, location, salary, industry). "
        f"Provide structured output."
    )

    try:
        result, _cost = call_claude(
            model=model,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
            output_schema=SONNET_SCHEMA,
            job_id=job_row.get("dedup_key"),
            purpose="sonnet_eval",
            max_tokens=2048,
            ctx=ctx or ClaudeContext(conn=conn, config=config),
        )
        logger.debug(
            "Sonnet evaluated '%s' @ '%s': score=%s",
            job_row.get("title"),
            job_row.get("company"),
            result.get("score"),
        )
        return ScoringResult(data=result, status="success")

    except BudgetExceededError:
        logger.info(
            "Sonnet eval budget exceeded for '%s' @ '%s'",
            job_row.get("title"),
            job_row.get("company"),
        )
        return ScoringResult(data=None, status="budget_exceeded")

    except Exception as e:
        logger.warning(
            "Sonnet eval error for '%s' @ '%s': %s",
            job_row.get("title"),
            job_row.get("company"),
            e,
        )
        return ScoringResult(data=None, status="error")
