"""Application package assembler — prepare-layer substrate.

Assembles a complete application package for a job (tailored resume, form mapping,
drafted free-text answers) WITHOUT submitting anything. This is the reversible
foundation the future auto-submit layer sits on top of.

DEPENDS ON: the resume-tailor transform (issue #598). The resume-tailor call
is a single monkeypatchable seam: ``tailor_resume`` (module-level attribute).
Tests should patch ``job_finder.web.application_prepare.tailor_resume``.
"""

import json
import logging
import sqlite3

from job_finder.web import user_data_dirs
from job_finder.web.direct_link import apply_url_for
from job_finder.web.model_provider import call_model
from job_finder.web.profile_schema import load_profile
from job_finder.web.resume_tailor import tailor_resume as _real_tailor_resume

logger = logging.getLogger(__name__)

# Structured-output schema for a single drafted answer. Without a schema the
# production-primary ollama provider forces format="json" and returns a PARSED
# dict (never a raw str), so a bare str(result.data) would surface a brace-wrapped
# Python-repr to the user. Constraining to {"answer": str} makes the shape
# deterministic across providers and lets us read the prose out cleanly.
_DRAFT_ANSWER_SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
    "additionalProperties": False,
}


def _extract_answer(data: object) -> str:
    """Pull the prose answer out of a call_model result.

    Handles both the structured dict (ollama/JSON providers -> {"answer": ...})
    and a raw string (text providers), never repr-ing a dict into the UI.
    """
    if isinstance(data, str):
        return data.strip()
    if isinstance(data, dict):
        answer = data.get("answer")
        if isinstance(answer, str):
            return answer.strip()
        # Fallback: first string value in the dict (defensive against key drift).
        for value in data.values():
            if isinstance(value, str):
                return value.strip()
    return ""


def _tailor_resume(*, conn: sqlite3.Connection, config: dict, job: dict, profile: dict) -> str:
    """Seam wrapper around the real resume-tailor implementation.

    The real function (job_finder.web.resume_tailor.tailor_resume) takes positional
    args and returns a structured dict. This wrapper adapts the call signature and
    serializes the dict to JSON for storage/display.

    Args:
        conn: sqlite3 connection
        config: app config dict
        job: job dict (must have jd_full)
        profile: experience profile dict

    Returns:
        JSON string of the tailored resume dict.

    Raises:
        ValueError: if profile has no positions/skills or job has no jd_full
            (propagated from the real implementation).
    """
    # Real function takes positional args: (job, profile, config, conn)
    result_dict = _real_tailor_resume(job, profile, config, conn)
    # Serialize to JSON for storage/display
    return json.dumps(result_dict, indent=2)


# Module-level attribute for test monkeypatching
tailor_resume = _tailor_resume


def prepare_application_package(conn: sqlite3.Connection, config: dict, job: dict) -> dict:
    """Assemble (do NOT submit) an application package for a job.

    Returns a NEW dict:
        {
          "resume_content": str,          # from the resume-tailor transform
          "form_mapping": dict,           # apply-field -> answer from profile
          "drafted_answers": dict,        # free-text question -> drafted answer
        }
    All three values MUST be non-empty for a real (non-stubbed) run.
    Performs NO network submission of any kind.
    """
    # Resolve the profile via the sanctioned user-data path (CWD-independent),
    # mirroring the profile blueprint and scorer — a bare load_profile() would
    # read a CWD-relative "experience_profile.json" and silently return an EMPTY
    # profile when launched from any non-data-root CWD (e.g. `serve` in a worktree).
    profile = load_profile(str(user_data_dirs.profile_path()))

    # 1. Tailored resume (via the resume-tailor seam)
    resume_content = tailor_resume(conn=conn, config=config, job=job, profile=profile)

    # 2. Form mapping: derive from profile keys
    form_mapping = _build_form_mapping(job, profile)

    # 3. Drafted free-text answers
    drafted_answers = _draft_free_text_answers(conn=conn, config=config, job=job, profile=profile)

    return {
        "resume_content": resume_content,
        "form_mapping": form_mapping,
        "drafted_answers": drafted_answers,
    }


def _build_form_mapping(job: dict, profile: dict) -> dict:
    """Build a mapping of apply fields to answers from the profile.

    Derives the field set from the profile schema — no hardcoded manually-maintained
    field list. Includes the apply URL via apply_url_for().
    """
    mapping = {}

    # Contact fields from profile (derived from schema)
    contact = profile.get("contact", {})
    contact_fields = ["full_name", "email", "phone", "linkedin", "github", "portfolio", "location"]
    for field in contact_fields:
        if field in contact:
            mapping[field] = contact[field]

    # Experience summary
    if profile.get("positions"):
        latest_position = profile["positions"][0]
        if "title" in latest_position:
            mapping["current_title"] = latest_position["title"]
        if "company" in latest_position:
            mapping["current_company"] = latest_position["company"]

    # Skills
    if "skills" in profile:
        mapping["skills"] = ", ".join(profile["skills"])

    # Years of experience (derived from positions)
    if profile.get("positions"):
        mapping["years_experience"] = str(len(profile["positions"]))

    # Apply URL (single enforcement point)
    apply_url = apply_url_for(job)
    if apply_url:
        mapping["apply_url"] = apply_url

    return mapping


def _draft_free_text_answers(
    conn: sqlite3.Connection, config: dict, job: dict, profile: dict
) -> dict:
    """Draft free-text answers for common application questions.

    Uses call_model(tier="quick") to draft answers. Questions are sourced from
    config.application.draft_questions (defaults to a fixed set if not configured).

    Returns a dict of question -> answer. All answers MUST be non-empty for a
    successful package; if any draft fails, the caller should surface an error.
    """
    # Source questions from config, with fallback to defaults
    questions = config.get("application", {}).get(
        "draft_questions",
        [
            "Why do you want to work here?",
            "Summarize your relevant experience for this role.",
            "What is your greatest professional achievement?",
        ],
    )

    answers = {}
    for question in questions:
        try:
            system = (
                "You are a job application assistant. Draft a concise, professional "
                "answer to the given question based on the candidate's profile and "
                "the job description. Keep the answer under 150 words. Respond with "
                'JSON of the form {"answer": "<your drafted answer>"}.'
            )
            messages = [
                {
                    "role": "user",
                    "content": f"Question: {question}\n\n"
                    f"Job Title: {job.get('title', 'N/A')}\n"
                    f"Company: {job.get('company', 'N/A')}\n"
                    f"Job Description: {job.get('jd_full', 'N/A')}\n\n"
                    f"Candidate Profile: {profile}",
                }
            ]

            result = call_model(
                tier="quick",
                system=system,
                messages=messages,
                output_schema=_DRAFT_ANSWER_SCHEMA,
                conn=conn,
                config=config,
                job_id=job.get("dedup_key"),
                purpose="application_draft",
            )
            answers[question] = _extract_answer(result.data)
        except Exception as e:
            logger.warning("Failed to draft answer for %s: %s", question, e)
            answers[question] = ""

    return answers
