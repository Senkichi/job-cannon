"""Resume parser module - extracts structured profile from PDF/DOCX resumes.

Provides parse_resume() function for wizard integration and a standalone CLI
for manual profile extraction.
"""

import json
import logging
import re
import sqlite3
from pathlib import Path

from docx import Document
from pdfplumber import PDF

from job_finder.web.model_provider import call_model

logger = logging.getLogger(__name__)

# Email-address matcher used to lift a contact address out of raw resume text so
# the IMAP step can prefill the Gmail field (Issue #399). Kept deterministic and
# zero-cost: a regex over the extracted text is more reliable than asking the LLM
# to surface an email and adds no provider round-trip. Conservative pattern — no
# attempt at full RFC 5322; just the common "local@domain.tld" shape.
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def _extract_email(text: str) -> str:
    """Return the first plausible email address found in ``text``, else ''.

    Args:
        text: Raw resume text.

    Returns:
        The first matched email address (lowercased), or '' when none is found.
    """
    match = _EMAIL_RE.search(text or "")
    return match.group(0).lower() if match else ""


# JSON schema for experience profile extraction
EXPERIENCE_PROFILE_SCHEMA = {
    "type": "object",
    "properties": {
        "positions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "company": {"type": "string"},
                    "start_date": {"type": "string"},
                    "end_date": {"type": "string"},
                    "description": {"type": "string"},
                },
            },
        },
        "skills": {"type": "array", "items": {"type": "string"}},
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
        "target_roles_suggested": {"type": "array", "items": {"type": "string"}},
        "target_locations_suggested": {"type": "array", "items": {"type": "string"}},
        "salary_range_suggested": {
            "type": "object",
            "properties": {
                "min": {"type": "integer"},
                "max": {"type": "integer"},
                "currency": {"type": "string"},
            },
        },
    },
    "required": [
        "positions",
        "skills",
        "education",
        "target_roles_suggested",
        "target_locations_suggested",
        "salary_range_suggested",
    ],
}


def _extract_text(path: Path) -> str:
    """Extract text from PDF or DOCX file.

    Args:
        path: Path to resume file.

    Returns:
        Extracted text as string.

    Raises:
        ValueError: If file type is not supported.
    """
    suffix = path.suffix.lower()

    if suffix == ".pdf":
        try:
            with PDF.open(path) as pdf:
                text = ""
                for page in pdf.pages:
                    text += page.extract_text() or ""
                return text
        except Exception as e:
            logger.error("PDF extraction failed: %s", e)
            raise
    elif suffix == ".docx":
        try:
            doc = Document(str(path))
            text = ""
            for paragraph in doc.paragraphs:
                text += paragraph.text + "\n"
            return text
        except Exception as e:
            logger.error("DOCX extraction failed: %s", e)
            raise
    else:
        raise ValueError(f"Unsupported resume file type: {suffix}")


def _empty_profile() -> dict:
    """Return empty profile structure.

    Returns:
        Dict with all required fields as empty containers.
    """
    return {
        "positions": [],
        "skills": [],
        "education": [],
        "target_roles_suggested": [],
        "target_locations_suggested": [],
        "salary_range_suggested": {},
    }


def _call_llm(text: str, conn: sqlite3.Connection, config: dict) -> dict:
    """Call LLM to extract structured profile from resume text.

    Args:
        text: Resume text extracted from file.
        conn: Open SQLite connection (passed to call_model for cost recording).
        config: Application config dict (used by call_model for routing).

    Returns:
        Structured profile dict from LLM response, or empty dict on failure.
    """
    # UAT F4 (2026-05-21): when the resume has no explicit Skills section,
    # the LLM previously returned skills=[] and the wizard's profile-edit
    # step rendered with an empty Skills textarea. The user either left it
    # blank ("I trust the parser") or typed something off the top of their
    # head, weakening the scoring prompt downstream. Fix is prompt-only:
    # tell the model how to behave when no Skills section exists. Keeps
    # this a single LLM call — no second pass, no retry loop.
    system_prompt = """You are a resume parser. Extract structured information from the resume text and return a JSON object with positions, skills, education, and suggested target roles, locations, and salary range.

For the `skills` field:
- If the resume has an explicit Skills / Technical Skills / Competencies section, populate `skills` from that section.
- If no such section exists, infer 8-15 skills from the technologies, tools, frameworks, methodologies, and domain terms mentioned in position descriptions, project bullets, and education. Return inferred skills as plain strings (no parenthetical "(inferred)" markers).
- Always return `skills` as a non-empty array when any work history is present in the resume."""
    user_message = f"Resume text:\n\n{text}"

    result = call_model(
        tier="quick",
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
        conn=conn,
        config=config,
        output_schema=EXPERIENCE_PROFILE_SCHEMA,
        job_id=None,
        purpose="resume_parse",
        max_tokens=2048,
    )

    if result and result.data:
        return result.data
    return {}


def parse_resume(path: str | Path, conn: sqlite3.Connection, config: dict) -> dict:
    """Parse resume file and extract structured profile.

    Args:
        path: Path to resume file (PDF or DOCX).
        conn: Open SQLite connection (used for LLM cost recording).
        config: Application config dict (used for provider routing).

    Returns:
        Structured profile dict. Returns empty profile on any failure
        (parse quality or LLM failure) - never raises except on unsupported extension.
    """
    try:
        path = Path(path)
        text = _extract_text(path)

        if not text or not text.strip():
            logger.warning("Extracted blank text from resume: %s", path)
            return _empty_profile()

        profile = _call_llm(text, conn, config)

        if not profile:
            logger.warning("LLM returned empty profile for resume: %s", path)
            profile = _empty_profile()

        # Surface a contact email for the IMAP-step prefill (Issue #399). Derived
        # deterministically from the raw text, not the LLM, so it works even when
        # the model returns an empty profile.
        email = _extract_email(text)
        if email:
            profile = {**profile, "email": email}

        return profile
    except ValueError as e:
        # Unsupported file type - let this propagate
        logger.error("Unsupported file type: %s", e)
        raise
    except Exception as e:
        # Parse quality or LLM failure - return empty profile
        logger.warning("Resume parsing failed for %s: %s", path, e)
        return _empty_profile()


if __name__ == "__main__":
    import sys

    from job_finder.config import load_config
    from job_finder.web import user_data_dirs
    from job_finder.web.db_helpers import standalone_connection

    if len(sys.argv) != 2:
        print("Usage: python -m job_finder.web.onboarding.resume_parser <resume.pdf>")
        print(
            "Extracts structured profile from PDF/DOCX resume and writes experience_profile.json"
        )
        sys.exit(2)

    resume_path = Path(sys.argv[1])
    if not resume_path.exists():
        print(f"Error: File not found: {resume_path}")
        sys.exit(2)

    try:
        cfg = load_config(allow_missing=True) or {}
        db_path = str(user_data_dirs.db_path())
        with standalone_connection(db_path) as standalone_conn:
            profile = parse_resume(resume_path, conn=standalone_conn, config=cfg)
        output_path = Path.cwd() / "experience_profile.json"
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(profile, f, indent=2)
        print(f"Profile written to: {output_path}")
    except ValueError as e:
        print(f"Error: {e}")
        sys.exit(2)
    except Exception as e:
        print(f"Error parsing resume: {e}")
        sys.exit(1)
