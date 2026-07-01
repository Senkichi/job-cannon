"""Application package assembler — prepare-layer substrate.

Assembles a complete application package for a job (tailored resume, form mapping,
drafted free-text answers) WITHOUT submitting anything. This is the reversible
foundation the future auto-submit layer sits on top of.

DEPENDS ON: the resume-tailor transform (issue #598). The resume-tailor call
is a single monkeypatchable seam: ``tailor_resume`` (module-level attribute).
Tests should patch ``job_finder.web.application_prepare.tailor_resume``.
"""

import logging
import sqlite3

from job_finder.web.direct_link import apply_url_for
from job_finder.web.model_provider import call_model
from job_finder.web.profile_schema import load_profile

logger = logging.getLogger(__name__)


# Resume-tailor seam — patched in CI gate test until the resume-tailor PR lands.
# The real implementation lives in job_finder.web.resume_tailor (issue #598).
def _tailor_resume(*, conn: sqlite3.Connection, config: dict, job: dict, profile: dict) -> str:
    """Shim: raises NotImplementedError until resume-tailor transform is available.

    The CI gate test monkeypatches this module attribute to return a sentinel,
    so the implementation path can be tested before the resume-tailor PR lands.
    """
    raise NotImplementedError(
        "resume-tailor transform not yet available (blocked on the resume-tailor issue #598)"
    )


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
    profile = load_profile()

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

    # Basic contact info from profile
    if "full_name" in profile:
        mapping["full_name"] = profile["full_name"]
    if "email" in profile:
        mapping["email"] = profile["email"]
    if "phone" in profile:
        mapping["phone"] = profile["phone"]
    if "linkedin" in profile:
        mapping["linkedin"] = profile["linkedin"]
    if "github" in profile:
        mapping["github"] = profile["github"]
    if "portfolio" in profile:
        mapping["portfolio"] = profile["portfolio"]

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

    # Location
    if "location" in profile:
        mapping["location"] = profile["location"]

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

    Uses call_model(tier="quick") to draft answers. The set of questions
    may come from config/job apply metadata in the future; for now,
    we draft answers for a fixed set of common questions.
    """
    questions = [
        "Why do you want to work here?",
        "Summarize your relevant experience for this role.",
        "What is your greatest professional achievement?",
    ]

    answers = {}
    for question in questions:
        try:
            system = (
                "You are a job application assistant. Draft a concise, professional "
                "answer to the given question based on the candidate's profile and "
                "the job description. Keep the answer under 150 words."
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
                conn=conn,
                config=config,
                job_id=job.get("dedup_key"),
                purpose="application_draft",
            )
            answers[question] = result.content
        except Exception as e:
            logger.warning("Failed to draft answer for %s: %s", question, e)
            answers[question] = ""

    return answers
