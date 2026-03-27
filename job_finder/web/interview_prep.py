"""Interview prep generation engine for Phase 5 Intelligence.

Generates structured interview preparation content using Opus when a job
transitions to 'applied' status. Runs in a background daemon thread following
the stale_detector.py pattern (own sqlite3 connection for thread safety).

Exports:
    generate_interview_prep_background: Main entry point for background thread.
    INTERVIEW_PREP_SCHEMA: JSON schema for Opus structured output.
"""

import json
import logging
import sqlite3
from datetime import datetime, timezone

import anthropic
import requests

from job_finder.web.db_helpers import standalone_connection
from job_finder.web.claude_client import BudgetExceededError
from job_finder.web.model_provider import call_model
from job_finder.web.scoring_orchestrator import load_scoring_profile

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Structured output schema for Opus
# ---------------------------------------------------------------------------

INTERVIEW_PREP_SCHEMA = {
    "type": "object",
    "properties": {
        "company_brief": {
            "type": "string",
            "description": "2-3 sentences summarizing company mission, size, recent news",
        },
        "predicted_questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "star_story": {
                        "type": "string",
                        "description": "Specific STAR story from profile for this question",
                    },
                    "key_points": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["question", "star_story", "key_points"],
                "additionalProperties": False,
            },
            "minItems": 5,
            "maxItems": 7,
        },
        "gap_mitigation": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Talking points to reframe profile gaps as strengths",
        },
        "questions_to_ask": {
            "type": "array",
            "items": {"type": "string"},
            "description": "5 thoughtful questions for the interviewer",
        },
    },
    "required": ["company_brief", "predicted_questions", "gap_mitigation", "questions_to_ask"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------------------
# SerpAPI company brief fetcher
# ---------------------------------------------------------------------------

SERPAPI_BASE_URL = "https://serpapi.com/search.json"


def _fetch_company_info(company_name: str, config: dict) -> str:
    """Fetch company info from SerpAPI for interview prep context.

    Uses SerpAPI web search with q="{company_name} company" to get company
    snippets. Falls back to empty string on any failure.

    Args:
        company_name: Name of the company to search for.
        config: Application config dict (reads apis.serpapi_key).

    Returns:
        Company info string (joined snippets) or empty string on failure.
    """
    api_key = (
        config.get("apis", {}).get("serpapi_key")
        or config.get("serpapi", {}).get("api_key")
        or config.get("serpapi_key")
    )
    if not api_key:
        logger.debug("_fetch_company_info: no SerpAPI key configured, skipping")
        return ""

    try:
        params = {
            "engine": "google",
            "q": f"{company_name} company",
            "api_key": api_key,
            "num": 3,
        }
        resp = requests.get(SERPAPI_BASE_URL, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        snippets = []
        for result in data.get("organic_results", [])[:3]:
            snippet = result.get("snippet", "")
            if snippet:
                snippets.append(snippet)

        return " ".join(snippets) if snippets else ""

    except Exception as e:
        logger.warning("_fetch_company_info failed for '%s': %s", company_name, e)
        return ""


# ---------------------------------------------------------------------------
# Background interview prep generation
# ---------------------------------------------------------------------------

def generate_interview_prep_background(
    dedup_key: str,
    db_path: str,
    config: dict,
) -> None:
    """Generate structured interview prep for a job in a background thread.

    Opens its own sqlite3 connection (thread-safe — follows stale_detector.py
    pattern, NOT Flask g.db). Inserts a 'generating' row, calls Opus with the
    job description and profile, then updates to 'done' with content or 'error'
    with an error message.

    Dedup guard: skips if a row with status='generating' or 'done' already
    exists for this job.

    Args:
        dedup_key: Job dedup_key (also job_id in interview_preps).
        db_path: Path to the SQLite database file.
        config: Application config dict.
    """
    with standalone_connection(db_path) as conn:
        _run_prep_generation(conn, dedup_key, config)


def _run_prep_generation(
    conn: sqlite3.Connection,
    dedup_key: str,
    config: dict,
) -> None:
    """Inner logic for interview prep generation (extracted for testability)."""
    # --- Dedup guard: skip if already generating or done ---
    existing = conn.execute(
        "SELECT id FROM interview_preps WHERE job_id = ? AND status IN ('generating', 'done')",
        (dedup_key,),
    ).fetchone()
    if existing:
        logger.info(
            "generate_interview_prep_background: skipping %s — prep already exists (id=%s)",
            dedup_key,
            existing["id"],
        )
        return

    # --- Insert initial 'generating' row ---
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    cursor = conn.execute(
        "INSERT INTO interview_preps (job_id, status, generated_at) VALUES (?, ?, ?)",
        (dedup_key, "generating", now),
    )
    prep_id = cursor.lastrowid
    if prep_id is None:
        raise RuntimeError(
            f"Failed to insert interview_prep row for dedup_key: {dedup_key}"
        )
    conn.commit()

    try:
        # --- Load job row ---
        job = conn.execute(
            "SELECT title, company, jd_full, sonnet_score, fit_analysis "
            "FROM jobs WHERE dedup_key = ?",
            (dedup_key,),
        ).fetchone()

        if job is None:
            raise ValueError(f"Job not found for dedup_key: {dedup_key}")

        title = job["title"] or ""
        company = job["company"] or ""
        jd_full = job["jd_full"] or ""
        fit_analysis_raw = job["fit_analysis"] or "{}"

        try:
            fit_analysis = json.loads(fit_analysis_raw)
        except json.JSONDecodeError:
            fit_analysis = {}

        # --- Load experience profile ---
        profile = load_scoring_profile(config)

        # --- Fetch company info via SerpAPI (best-effort) ---
        company_info = _fetch_company_info(company, config)

        # --- Build system prompt ---
        system_prompt = _build_system_prompt(title, company, jd_full, profile, fit_analysis, company_info)

        # --- Call Opus ---
        client = anthropic.Anthropic()
        messages = [
            {"role": "user", "content": "Generate the interview preparation for this job application."}
        ]

        result_obj = call_model(
            tier="opus",
            system=system_prompt,
            messages=messages,
            output_schema=INTERVIEW_PREP_SCHEMA,
            conn=conn,
            job_id=dedup_key,
            purpose="opus_interview_prep",
            config=config,
            max_tokens=4096,
            client=client,
        )
        result = result_obj.data
        cost_usd = result_obj.cost_usd

        # --- Store results ---
        company_brief = result.get("company_brief", "")
        predicted_questions = json.dumps(result.get("predicted_questions", []))
        gap_mitigation = json.dumps(result.get("gap_mitigation", []))
        questions_to_ask = json.dumps(result.get("questions_to_ask", []))

        conn.execute(
            """UPDATE interview_preps
               SET status = 'done',
                   company_brief = ?,
                   predicted_questions = ?,
                   gap_mitigation = ?,
                   questions_to_ask = ?,
                   cost_usd = ?
               WHERE id = ?""",
            (company_brief, predicted_questions, gap_mitigation, questions_to_ask, cost_usd, prep_id),
        )
        conn.commit()
        logger.info(
            "generate_interview_prep_background: completed for %s (cost=%.4f)",
            dedup_key,
            cost_usd,
        )

    except BudgetExceededError as e:
        error_msg = str(e)
        logger.info(
            "generate_interview_prep_background: budget exceeded for %s: %s", dedup_key, e
        )
        conn.execute(
            "UPDATE interview_preps SET status = 'error', error_msg = ? WHERE id = ?",
            (error_msg, prep_id),
        )
        conn.commit()

    except Exception as e:
        error_msg = str(e)[:500]  # Truncate long error messages
        logger.exception(
            "generate_interview_prep_background: failed for %s: %s", dedup_key, e
        )
        conn.execute(
            "UPDATE interview_preps SET status = 'error', error_msg = ? WHERE id = ?",
            (error_msg, prep_id),
        )
        conn.commit()


def _build_system_prompt(
    title: str,
    company: str,
    jd_full: str,
    profile: dict,
    fit_analysis: dict,
    company_info: str,
) -> str:
    """Build the Opus system prompt for interview prep generation."""
    profile_summary = _format_profile_for_prompt(profile)
    fit_summary = _format_fit_analysis(fit_analysis)

    company_context = ""
    if company_info:
        company_context = f"\n\n## Company Research\n{company_info}"

    jd_section = ""
    if jd_full:
        jd_preview = jd_full[:3000]  # Limit JD to avoid token overflow
        jd_section = f"\n\n## Job Description\n{jd_preview}"

    return f"""You are an expert interview coach preparing a candidate for a job interview.

## Role Being Applied For
Title: {title}
Company: {company}{company_context}{jd_section}

## Candidate Profile
{profile_summary}

## AI Fit Analysis
{fit_summary}

Generate comprehensive interview preparation with all four required sections:

1. **company_brief**: 2-3 sentences about the company's mission, size, and any notable recent news. Use the company research provided; if none, use general knowledge.

2. **predicted_questions**: 5-7 likely interview questions based on the job description. For EACH question, provide:
   - A specific STAR story drawn from the candidate's actual experience (positions/achievements listed above)
   - 3-5 key points to emphasize

3. **gap_mitigation**: Talking points to reframe any profile gaps as strengths or growth opportunities. Be specific and actionable.

4. **questions_to_ask**: 5 thoughtful questions for the interviewer that demonstrate genuine interest and strategic thinking about the role.

Be specific, practical, and tailored to both the role and the candidate's actual experience."""


def _format_profile_for_prompt(profile: dict) -> str:
    """Format experience profile for inclusion in Opus prompt."""
    if not profile:
        return "(No profile available)"

    lines = []

    name = profile.get("name", "")
    if name:
        lines.append(f"Name: {name}")

    summary = profile.get("summary", "")
    if summary:
        lines.append(f"\nSummary: {summary[:500]}")

    skills = profile.get("skills", [])
    if skills:
        lines.append(f"\nSkills: {', '.join(skills[:20])}")

    positions = profile.get("positions", [])
    if positions:
        lines.append("\nExperience:")
        for pos in positions[:5]:  # Limit to 5 most recent positions
            title = pos.get("title", "")
            company = pos.get("company", "")
            dates = pos.get("dates", "")
            lines.append(f"  - {title} at {company} ({dates})")
            for ach in pos.get("achievements", [])[:3]:  # Limit achievements
                lines.append(f"    * {ach}")

    education = profile.get("education", [])
    if education:
        lines.append("\nEducation:")
        for ed in education:
            degree = ed.get("degree", "")
            institution = ed.get("institution", "")
            graduation = ed.get("graduation", "")
            lines.append(f"  - {degree} — {institution} ({graduation})")
            if ed.get("thesis"):
                lines.append(f"    Thesis: {ed['thesis']}")

    return "\n".join(lines) if lines else "(No profile data)"


def _format_fit_analysis(fit_analysis: dict) -> str:
    """Format AI fit analysis for inclusion in Opus prompt."""
    if not fit_analysis:
        return "(No fit analysis available)"

    lines = []

    strengths = fit_analysis.get("strengths", [])
    if strengths:
        lines.append("Strengths: " + "; ".join(strengths[:5]))

    gaps = fit_analysis.get("gaps", [])
    if gaps:
        lines.append("Gaps to address: " + "; ".join(gaps[:5]))

    return "\n".join(lines) if lines else "(No fit analysis data)"
