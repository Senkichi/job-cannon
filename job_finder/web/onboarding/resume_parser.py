"""Resume parser module - extracts structured profile from PDF/DOCX resumes.

Provides parse_resume() function for wizard integration and a standalone CLI
for manual profile extraction.
"""

import json
import logging
from pathlib import Path
from typing import Any

from docx import Document
from pdfplumber import PDF

from job_finder.web.model_provider import call_model

logger = logging.getLogger(__name__)

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


def _call_llm(text: str, config: dict | None = None) -> dict:
    """Call LLM to extract structured profile from resume text.

    Args:
        text: Resume text extracted from file.
        config: Optional config dict (not used in this phase).

    Returns:
        Structured profile dict from LLM response.
    """
    system_prompt = """You are a resume parser. Extract structured information from the resume text.
Return a JSON object with positions, skills, education, and suggested target roles/locations/salary range."""
    user_message = f"Resume text:\n\n{text}"

    result = call_model(
        tier="low",
        system_prompt=system_prompt,
        user_message=user_message,
        output_schema=EXPERIENCE_PROFILE_SCHEMA,
        max_tokens=2048,
    )

    if result and result.data:
        return result.data
    return {}


def parse_resume(path: str | Path, config: dict | None = None) -> dict:
    """Parse resume file and extract structured profile.

    Args:
        path: Path to resume file (PDF or DOCX).
        config: Optional config dict (not used in this phase).

    Returns:
        Structured profile dict. Returns empty profile on any failure
        (parse quality or LLM failure) - never raises.
    """
    try:
        path = Path(path)
        text = _extract_text(path)

        if not text or not text.strip():
            logger.warning("Extracted blank text from resume: %s", path)
            return _empty_profile()

        profile = _call_llm(text, config)

        if not profile:
            logger.warning("LLM returned empty profile for resume: %s", path)
            return _empty_profile()

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

    if len(sys.argv) != 2:
        print("Usage: python -m job_finder.web.onboarding.resume_parser <resume.pdf>")
        print("Extracts structured profile from PDF/DOCX resume and writes experience_profile.json")
        sys.exit(2)

    resume_path = Path(sys.argv[1])
    if not resume_path.exists():
        print(f"Error: File not found: {resume_path}")
        sys.exit(2)

    try:
        profile = parse_resume(resume_path)
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
