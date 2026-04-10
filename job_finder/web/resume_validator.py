"""Post-generation resume quality validator.

Provides: VALIDATION_SCHEMA, validate_resume, fix_resume_violations.

Runs a Sonnet audit pass on every generated resume to catch content integrity
violations (fabricated skills, leaked client names, wrong dates, missing sections)
and style/JD-alignment warnings. Error-severity violations trigger an auto-fix
pass. Both functions fail-open on error so resume generation is never blocked.

Max 2 Sonnet calls per resume: 1 audit + 1 conditional fix.
"""

import json
import logging
import sqlite3

from job_finder.config import DEFAULT_MODEL_SONNET
from job_finder.web.claude_client import call_claude
from job_finder.web.resume_generator import RESUME_SCHEMA

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# JSON schema for Sonnet audit output
# ---------------------------------------------------------------------------

VALIDATION_SCHEMA = {
    "type": "object",
    "properties": {
        "passed": {
            "type": "boolean",
            "description": "True if no error-severity violations found",
        },
        "violations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "description": (
                            "content_integrity, structural, style, "
                            "jd_alignment, or readability"
                        ),
                    },
                    "description": {
                        "type": "string",
                        "description": "Specific violation with exact offending text",
                    },
                    "severity": {
                        "type": "string",
                        "description": "error or warning",
                    },
                    "location": {
                        "type": "string",
                        "description": "Which resume section contains the violation",
                    },
                },
                "required": ["category", "description", "severity"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["passed", "violations"],
    "additionalProperties": False,
}

# ---------------------------------------------------------------------------
# System prompt for audit pass
# ---------------------------------------------------------------------------

_AUDIT_SYSTEM = (
    "You are a professional resume quality auditor. Review the generated resume against "
    "the candidate's experience profile and job description, then report any violations.\n\n"

    "## Violation Categories and Severity\n\n"

    "### content_integrity (error — triggers auto-fix)\n"
    "These are factual errors that damage the candidate's credibility:\n"
    "- Fabricated skills: any skill, tool, or technology in the resume not present in the "
    "candidate's experience profile\n"
    "- Leaked client names: specific client or company names mentioned in bullets when the "
    "source material says to use generic descriptors\n"
    "- Date mismatches: employment dates that differ from the profile data\n"
    "- Missing or broken sections: required sections (Summary, Skills, Experience, Education) "
    "that are absent, empty, or contain placeholder text like 'UNKNOWN'\n\n"

    "### structural (error if egregious, warning if minor)\n"
    "- Summary exceeds 5 sentences: error\n"
    "- Skills section exceeds 3 lines: error\n"
    "- Summary is 4-5 sentences or skills is 3 lines: warning\n"
    "- Bullet counts significantly deviate from seniority guidelines: warning\n\n"

    "### style (warning — reported but not auto-fixed)\n"
    "- Two or more consecutive bullets start with the same verb\n"
    "- A bullet exceeds 3 lines\n"
    "- Soft skills listed in the Skills section (e.g., 'Stakeholder Communication', "
    "'Cross-Functional Collaboration', 'Team Leadership')\n"
    "- Bold text used within bullet point content (bold is only for headers and titles)\n\n"

    "### ats_compatibility (error — triggers auto-fix)\n"
    "- Em dashes (\u2014) or en dashes (\u2013) used anywhere in the document\n"
    "- Smart/curly quotes (\u201C \u201D \u2018 \u2019) used anywhere in the document\n"
    "- These characters break ATS keyword matching (Workday, Taleo, iCIMS). "
    "Replace with ASCII equivalents (hyphens, straight quotes).\n\n"

    "### jd_alignment (warning — reported but not auto-fixed)\n"
    "- Top 5 JD keywords are missing from the resume entirely\n"
    "- Verbatim JD phrases lifted directly into resume bullets\n\n"

    "### readability (warning — reported but not auto-fixed)\n"
    "- Bullets that fail the 'so what?' test: no quantified result or compelling business outcome\n"
    "- Vague language: 'helped with', 'assisted in', 'was involved in', 'was responsible for'\n"
    "- Passive voice used in bullet points\n\n"

    "## Rules\n\n"
    "- Return passed=true ONLY if there are ZERO error-severity violations. "
    "Warnings alone do NOT cause passed=false.\n"
    "- For each violation, provide the exact offending text in the description field.\n"
    "- Be specific: 'Skill dbt listed in Skills section but not found in profile' rather "
    "than 'fabricated skill found'.\n"
    "- Cross-reference the experience profile carefully before flagging content_integrity errors. "
    "A skill is only fabricated if it genuinely does not appear in the profile data.\n"
    "- Do not flag stylistic opinions; only flag clear violations of the rules above."
)

_FIX_SYSTEM = (
    "You are a resume editor. Fix only the specific violations listed below. "
    "Maintain the closed-world constraint: do not add, invent, or infer any content "
    "not present in the original resume. Only fix the specific violations listed. "
    "Preserve all content not affected by a violation exactly as written. "
    "Return the complete fixed resume matching the output schema."
)

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def validate_resume(
    resume_data: dict,
    jd_text: str,
    profile: dict,
    conn: sqlite3.Connection,
    config: dict,
) -> dict:
    """Run a Sonnet audit pass on a generated resume.

    Checks for content integrity errors (fabricated skills, leaked client names,
    wrong dates, missing sections) and style/JD-alignment warnings. Does NOT
    budget-gate — validation always runs.

    Args:
        resume_data: Generated resume dict matching RESUME_SCHEMA.
        jd_text: Full job description text (used for JD alignment checks).
        profile: Candidate experience profile dict (for verifying facts).
        conn: Open SQLite connection for cost recording.
        config: Application config dict.

    Returns:
        Validation report dict with keys:
            passed (bool): True if no error-severity violations.
            violations (list): List of violation dicts (may be empty).
        On any exception, returns {"passed": True, "violations": []} (fail-open).
    """
    try:
        model = (
            config.get("scoring", {})
            .get("models", {})
            .get("sonnet", DEFAULT_MODEL_SONNET)
        )

        # Build a compact profile summary for Sonnet to cross-reference
        profile_skills = profile.get("skills", [])
        positions_summary = []
        for pos in profile.get("positions", []):
            positions_summary.append(
                f"- {pos.get('title', '')} at {pos.get('company', '')} "
                f"({pos.get('start_date', '')} - {pos.get('end_date', 'Present')})"
            )

        user_message = (
            "## Generated Resume (JSON)\n\n"
            f"```json\n{json.dumps(resume_data, indent=2)}\n```\n\n"
            "---\n\n"
            "## Job Description\n\n"
            f"{jd_text or '(no JD provided)'}\n\n"
            "---\n\n"
            "## Candidate Profile (for fact-checking)\n\n"
            f"**Skills in profile:** {', '.join(profile_skills) if profile_skills else 'Not specified'}\n\n"
            "**Employment history:**\n"
            + ("\n".join(positions_summary) if positions_summary else "None listed")
            + "\n\n"
            "Audit the generated resume and report any violations."
        )

        result, _cost = call_claude(
            model=model,
            system=_AUDIT_SYSTEM,
            messages=[{"role": "user", "content": user_message}],
            output_schema=VALIDATION_SCHEMA,
            conn=conn,
            job_id=None,
            purpose="resume_validation",
            config=config,
            max_tokens=2048,
        )
        return result

    except Exception as e:
        logger.warning("validate_resume: audit failed, returning fail-open result: %s", e)
        return {"passed": True, "violations": []}

def fix_resume_violations(
    resume_data: dict,
    violations: list[dict],
    profile: dict,
    conn: sqlite3.Connection,
    config: dict,
) -> dict:
    """Run a Sonnet fix pass on a resume to correct error-severity violations.

    Only sends error-severity violations to Sonnet. Warning violations are not
    fixed. If no error violations exist, returns resume_data unchanged without
    calling Claude. On any exception, returns original resume_data (fail-open).

    Args:
        resume_data: Generated resume dict matching RESUME_SCHEMA.
        violations: List of violation dicts from validate_resume().
        profile: Candidate experience profile dict (for context).
        conn: Open SQLite connection for cost recording.
        config: Application config dict.

    Returns:
        Fixed resume dict matching RESUME_SCHEMA, or original resume_data on error.
    """
    # Filter to error-severity only
    error_violations = [v for v in violations if v.get("severity") == "error"]
    if not error_violations:
        return resume_data

    try:
        model = (
            config.get("scoring", {})
            .get("models", {})
            .get("sonnet", DEFAULT_MODEL_SONNET)
        )

        profile_skills = profile.get("skills", [])

        violation_lines = []
        for i, v in enumerate(error_violations, 1):
            loc = f" (location: {v['location']})" if v.get("location") else ""
            violation_lines.append(
                f"{i}. [{v.get('category', 'unknown')}]{loc}: {v.get('description', '')}"
            )
        violations_text = "\n".join(violation_lines)

        user_message = (
            "## Resume to Fix (JSON)\n\n"
            f"```json\n{json.dumps(resume_data, indent=2)}\n```\n\n"
            "---\n\n"
            "## Violations to Fix\n\n"
            f"{violations_text}\n\n"
            "---\n\n"
            "## Candidate's Actual Skills (for replacing fabricated ones)\n\n"
            f"{', '.join(profile_skills) if profile_skills else 'Not specified'}\n\n"
            "Fix ONLY the violations listed above. Do not modify any other content. "
            "Return the complete fixed resume."
        )

        result, _cost = call_claude(
            model=model,
            system=_FIX_SYSTEM,
            messages=[{"role": "user", "content": user_message}],
            output_schema=RESUME_SCHEMA,
            conn=conn,
            job_id=None,
            purpose="resume_fix",
            config=config,
            max_tokens=4096,
        )
        return result

    except Exception as e:
        logger.warning("fix_resume_violations: fix pass failed, returning original resume: %s", e)
        return resume_data
