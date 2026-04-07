"""Resume content helpers: constants, prompt strings, and profile formatters.

Extracted from resume_generator.py. Provides:
    RESUME_SCHEMA            -- JSON schema for structured Sonnet resume output.
    STRATEGY_POOL            -- Pool of resume strategy identifiers for multi-version.
    _STRATEGY_DESCRIPTIONS   -- Human-readable descriptions per strategy.
    _RESUME_GUIDELINES       -- Distilled writing guidelines injected into system prompt.
    _SYSTEM_PROMPT           -- Full system prompt with closed-world constraint.
    _format_education        -- Format education block from profile dict.
    _format_profile_positions -- Format positions block from profile dict.
    _get_accepted_preferences -- Query accepted, unconsumed resume preferences from DB.
"""

import logging
import sqlite3

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
    except sqlite3.OperationalError:
        # Table may not exist in test DBs or older schemas — degrade gracefully
        logger.debug("Failed to load resume preferences (non-fatal)", exc_info=True)
        return []


# ---------------------------------------------------------------------------
# Private helpers: profile formatters
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
