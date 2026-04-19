"""Haiku fast-filter scorer for job postings.

Provides score_job_haiku(), which evaluates a job row against the candidate
profile using Claude Haiku's structured output. This is the first tier of
AI scoring — every new job gets a quick assessment of title fit, location fit,
and salary floor, producing a 0-100 score and a one-line summary.

Output schema:
  score (int 0-100): Overall fit score.
  summary (str): One-sentence rationale for the score.
  title_fit (str): "strong" | "partial" | "weak" | "reject"
  location_fit (str): "remote" | "target" | "other" | "unknown"
  salary_meets_floor (bool): True if salary >= candidate's min_salary.
"""

import json
import logging
import re
from typing import Any

from job_finder.config import DEFAULT_MODEL_HAIKU
from job_finder.web.claude_client import call_claude, BudgetExceededError, ClaudeContext
from job_finder.web.model_provider import ProviderCascadeExhaustedError, call_model
from job_finder.web.scoring_types import JobRow, ScoringResult, format_salary_range

logger = logging.getLogger(__name__)


# Satisfies _make_adapter's api_key guard without pulling in the Anthropic SDK.
# AnthropicProvider forwards this to call_claude(), which ignores client and
# routes through the CLI — OAuth/subscription billing is preserved.
class _CLIClientStub:
    api_key = "cli-managed"


_CLI_CLIENT_STUB = _CLIClientStub()


# ---------------------------------------------------------------------------
# Structured output schema for Haiku scoring
# ---------------------------------------------------------------------------

HAIKU_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "score": {
            "type": "integer",
            "description": "Score 0-100 based on title/location/salary fit against candidate profile",
            "minimum": 0,
            "maximum": 100,
        },
        "summary": {
            "type": "string",
            "description": "One-sentence summary of fit rationale",
        },
        "title_fit": {
            "type": "string",
            "enum": ["strong", "partial", "weak", "reject"],
        },
        "location_fit": {
            "type": "string",
            "enum": ["remote", "target", "other", "unknown"],
        },
        "salary_meets_floor": {
            "type": "boolean",
        },
    },
    "required": ["score", "summary", "title_fit", "location_fit", "salary_meets_floor"],
    "additionalProperties": False,
}

# System prompt for Haiku scoring
_SYSTEM_PROMPT = (
    "You are a job fit evaluator. Score this job posting against the candidate's profile. "
    "Consider title match, location fit, salary floor, industry relevance, and seniority alignment. "
    "Be calibrated: 70+ = strong match worth reviewing, 50-69 = partial match, <50 = poor fit."
)

def _build_comp_context(job_row: JobRow) -> str | None:
    """Build a concise compensation context string from comp_data_json.

    Extracts equity, bonus, and benefits summaries from ATS-sourced
    compensation data (stored as JSON). Returns a short summary string
    suitable for inclusion in the Haiku scoring prompt, or None if no
    additional compensation data is available.

    Kept concise to minimize token cost — just the summary, not raw JSON.

    Args:
        job_row: Job record dict. May contain 'comp_data_json' field.

    Returns:
        Short compensation summary string, or None if no data available.
    """
    comp_data_raw = job_row.get("comp_data_json")
    if not comp_data_raw:
        return None

    try:
        comp = json.loads(comp_data_raw) if isinstance(comp_data_raw, str) else comp_data_raw
    except (ValueError, TypeError):
        return None

    if not comp or not isinstance(comp, dict):
        return None

    parts = []

    # Ashby: compensationTierSummary is a human-readable summary
    tier_summary = comp.get("compensationTierSummary")
    if tier_summary and isinstance(tier_summary, str):
        parts.append(tier_summary.strip())

    # Lever: if it's a salaryRange dict with currency info
    currency = comp.get("currency")
    if currency and not tier_summary:
        comp_min = comp.get("min")
        comp_max = comp.get("max")
        if comp_min and comp_max:
            parts.append(f"{currency} {comp_min:,}-{comp_max:,}")

    return "; ".join(parts) if parts else None

def build_description_snippet(
    description: str,
    profile_skills: list[str],
    max_chars: int = 2000,
) -> str:
    """Build an intelligent snippet: first 1200 chars + skill keyword summary + requirements extraction.

    Replaces the old 500-char truncation. Gives Haiku access to requirements/qualifications
    sections and skill match counts from the full description, without sending the entire
    posting text.

    Args:
        description: Full job description text.
        profile_skills: List of candidate skill strings from config.yaml profile.skills.
        max_chars: Maximum total snippet length (default 2000).

    Returns:
        Snippet string of at most max_chars characters.
    """
    if not description:
        return ""

    # Primary snippet: first 1200 characters (covers intro + start of requirements)
    snippet = description[:1200].strip()

    # Skill keyword count from FULL description (zero extra tokens vs sending full text)
    description_lower = description.lower()
    skill_matches = {}
    for skill in profile_skills:
        if not skill:
            continue
        count = description_lower.count(skill.lower())
        if count > 0:
            skill_matches[skill] = count

    # Build keyword summary
    if skill_matches:
        matches_str = ", ".join(
            f"{skill} ({count}x)"
            for skill, count in sorted(skill_matches.items(), key=lambda x: -x[1])
        )
        keyword_summary = f"\n\n[Skill keyword matches in full posting: {matches_str}]"
    else:
        keyword_summary = "\n\n[No candidate skill keywords found in full posting text]"

    # Budget remaining characters for requirements section extraction
    remaining_chars = max_chars - len(snippet) - len(keyword_summary)
    if remaining_chars > 200 and len(description) > 1200:
        req_pattern = re.compile(
            r'(requirements|qualifications|what you.ll bring|what we.re looking for|'
            r'about you|your background|skills|experience required|must.have)',
            re.IGNORECASE,
        )
        match = req_pattern.search(description, 800)
        if match:
            req_start = max(0, match.start() - 20)
            req_section = description[req_start : req_start + remaining_chars].strip()
            if req_section and req_section not in snippet:
                snippet += f"\n\n[...Requirements section:]\n{req_section}"

    return (snippet + keyword_summary)[:max_chars]

def score_job_haiku(
    job_row: JobRow,
    experience_profile: dict,
    conn: Any,
    config: dict,
    max_chars: int = 2000,
    purpose: str = "haiku_score",
    *,
    ctx: ClaudeContext | None = None,
) -> ScoringResult:
    """Score a single job against the candidate profile using Claude Haiku.

    Args:
        job_row: Job record dict with keys: dedup_key, title, company, location,
                 salary_min (optional), salary_max (optional), description (optional).
        experience_profile: Profile dict with target_titles, target_locations,
                 min_salary, skills, industries keys.
        conn: Open SQLite connection for cost recording.
            Ignored when *ctx* is provided.
        config: Application config dict (reads scoring.models.haiku, scoring.daily_budget_usd).
            Ignored when *ctx* is provided.
        max_chars: Maximum characters for description snippet (default 2000).
                   Pass 4000 for borderline re-evaluation to expand context.
        purpose: Cost tracking purpose label (default "haiku_score", use "haiku_reeval"
                 for the borderline second-pass call).
        ctx: ClaudeContext bundling (conn, config).  When supplied,
            the individual conn/config parameters are ignored.

    Returns:
        ScoringResult with status='success' and data dict containing score,
        summary, title_fit, location_fit, salary_meets_floor.
        On failure: status='budget_exceeded' or status='error', data=None.
    """
    # Resolve context: prefer ctx fields over individual params
    if ctx is not None:
        conn = ctx.conn
        config = ctx.config

    profile_section: dict = experience_profile

    # Build job context snippet
    title = job_row.get("title", "Unknown Title")
    company = job_row.get("company", "Unknown Company")
    location = job_row.get("location", "Unknown Location")
    salary_min = job_row.get("salary_min")
    salary_max = job_row.get("salary_max")
    # Prefer jd_full (enriched, full description) over description (raw ingest
    # excerpt). Enrichment writes jd_full but not description, so careers_crawl
    # and similar sparse-ingest sources end up with description="" but a
    # populated jd_full after enrichment runs.
    description = job_row.get("jd_full") or job_row.get("description") or ""

    # Skip when there is no scorable content. Scoring on title alone (empty
    # jd_full + empty description) produces meaningless numbers — especially
    # dangerous for careers_crawl jobs where the crawler intentionally emits
    # title-only shells and relies on enrichment to fill the rest. Callers
    # must run enrich_job before scoring; this guard is the invariant.
    if not description.strip():
        logger.info(
            "Haiku skipped for '%s' @ '%s': no jd_full or description — "
            "run enrichment before scoring",
            title, company,
        )
        return ScoringResult(data=None, status="skipped")
    # Strip residual HTML to prevent CSS soup from inflating scores
    if "<" in description:
        from job_finder.web.description_formatter import strip_html_to_text
        description = strip_html_to_text(description)

    # Build an intelligent snippet (C1): first 1200 chars + skill keyword summary
    # + requirements section extraction (replaces old 500-char truncation).
    # max_chars is 2000 by default; pass 4000 for borderline re-evaluation (C2).
    profile_skills = profile_section.get("skills", [])
    description_snippet = build_description_snippet(description, profile_skills, max_chars=max_chars)

    # Build salary string
    salary_str = format_salary_range(salary_min, salary_max)

    # Build compensation context from comp_data_json when available
    # Kept concise to minimize token cost — summary only, not raw JSON
    comp_context = _build_comp_context(job_row)

    # Build profile context
    target_titles = profile_section.get("target_titles", [])
    target_locations = profile_section.get("target_locations", [])
    min_salary = profile_section.get("min_salary")
    skills = profile_section.get("skills", [])
    industries = profile_section.get("industries", [])

    target_titles_str = ", ".join(target_titles) if target_titles else "Not specified"
    target_locations_str = ", ".join(target_locations) if target_locations else "Not specified"
    min_salary_str = f"${min_salary:,}" if min_salary is not None else "Not specified"
    skills_str = ", ".join(skills[:10]) if skills else "Not specified"
    industries_str = ", ".join(industries[:5]) if industries else "Not specified"

    user_prompt = (
        f"## Job Posting\n"
        f"Title: {title}\n"
        f"Company: {company}\n"
        f"Location: {location}\n"
        f"Salary: {salary_str}\n"
        + (f"Additional Compensation: {comp_context}\n" if comp_context else "")
        + f"Description: {description_snippet}\n"
        f"\n"
        f"## Candidate Profile\n"
        f"Target Titles: {target_titles_str}\n"
        f"Target Locations: {target_locations_str}\n"
        f"Minimum Salary: {min_salary_str}\n"
        f"Key Skills: {skills_str}\n"
        f"Target Industries: {industries_str}\n"
    )

    # Inject legitimacy signals if any red flags detected
    try:
        from job_finder.web.legitimacy_signals import compute_legitimacy_signals
        leg_signals = compute_legitimacy_signals(job_row, conn)
        if leg_signals.get("legitimacy_note"):
            user_prompt += (
                f"\n## Legitimacy Signals\n"
                f"{leg_signals['legitimacy_note']}\n\n"
                f"Factor these signals into your score. A job with multiple red flags "
                f"(very old posting, no salary, vague description, appears on many "
                f"sources) is likely a ghost posting and should be scored lower.\n"
            )
    except Exception:
        pass  # Legitimacy signals are best-effort; never block scoring

    user_prompt += (
        f"\n"
        f"Score this job posting against the candidate profile. "
        f"Use the structured output format."
    )

    # Get Haiku model from config
    model = (
        config.get("scoring", {})
        .get("models", {})
        .get("haiku", DEFAULT_MODEL_HAIKU)
    )

    job_id = job_row.get("dedup_key")

    # Route via call_model() only when providers.haiku is configured (e.g. Ollama
    # primary with CLI fallback). Without that config, stay on the direct
    # call_claude() path so untouched deployments see zero behavior change.
    use_dispatcher = bool(config.get("providers", {}).get("haiku"))

    try:
        if use_dispatcher:
            model_result = call_model(
                tier="haiku",
                system=_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_prompt}],
                conn=conn,
                config=config,
                output_schema=HAIKU_SCHEMA,
                job_id=job_id,
                purpose=purpose,
                max_tokens=1024,
                client=_CLI_CLIENT_STUB,
            )
            result = model_result.data
            result["provider"] = model_result.provider
            cost_usd = model_result.cost_usd
        else:
            result, cost_usd = call_claude(
                model=model,
                system=_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_prompt}],
                output_schema=HAIKU_SCHEMA,
                job_id=job_id,
                purpose=purpose,
                ctx=ctx or ClaudeContext(conn=conn, config=config),
            )
            result["provider"] = "anthropic"
        logger.debug(
            "Haiku scored '%s' @ '%s': score=%s (cost=$%.5f)",
            title,
            company,
            result.get("score"),
            cost_usd,
        )
        return ScoringResult(data=result, status="success")
    except BudgetExceededError:
        # Haiku should never hit budget cap, but handle defensively
        logger.warning(
            "BudgetExceededError for Haiku scoring of '%s' @ '%s'",
            title,
            company,
        )
        return ScoringResult(data=None, status="budget_exceeded")
    except ProviderCascadeExhaustedError as exc:
        # All providers in the cascade (Ollama primary + Anthropic fallback)
        # failed. Log and fall through to the CLI path as a last resort so a
        # scheduled run does not silently stop scoring when Ollama is down.
        logger.warning(
            "Haiku provider cascade exhausted for '%s' @ '%s' (%s); "
            "retrying via CLI",
            title, company, exc,
        )
        try:
            result, cost_usd = call_claude(
                model=model,
                system=_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_prompt}],
                output_schema=HAIKU_SCHEMA,
                job_id=job_id,
                purpose=purpose,
                ctx=ctx or ClaudeContext(conn=conn, config=config),
            )
            result["provider"] = "anthropic"
            return ScoringResult(data=result, status="success")
        except Exception as retry_exc:
            logger.warning(
                "Haiku CLI retry also failed for '%s' @ '%s': %s",
                title, company, retry_exc,
            )
            return ScoringResult(data=None, status="error")
    except Exception as e:
        logger.warning(
            "Haiku scoring failed for '%s' @ '%s': %s",
            title,
            company,
            e,
        )
        return ScoringResult(data=None, status="error")
