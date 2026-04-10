"""Profile schema definition, validation, and I/O utilities.

Provides:
    PROFILE_SCHEMA  -- Reference dict documenting expected experience_profile.json structure
    validate_profile(profile) -> list[dict]   -- Returns list of warning dicts
    load_profile(path) -> dict                -- Load JSON file (returns empty structure if missing)
    save_profile(profile, path) -> None       -- Write JSON file with indent=2 (with empty-overwrite guard)
    extract_profile_from_markdown(text) -> dict  -- Opus-powered extraction from markdown
"""

import json
import logging
import os
import re
from pathlib import Path

from job_finder.config import DEFAULT_MODEL_OPUS

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema reference
# ---------------------------------------------------------------------------

PROFILE_SCHEMA = {
    "positions": [
        {
            "title": "str",
            "company": "str",
            "start_date": "str",
            "end_date": "str or null",
            "achievements": ["str"],
            "skills": ["str"],
        }
    ],
    "skills": ["str (ordered by priority)"],
    "education": ["dict (opaque passthrough — no form UI, preserved on save)"],
    "resume_preferences": {
        "summary_style": "str",
        "emphasis": ["str"],
    },
}

# ---------------------------------------------------------------------------
# Empty / default profile structure
# ---------------------------------------------------------------------------

EMPTY_PROFILE = {
    "positions": [],
    "skills": [],
    "education": [],
    "resume_preferences": {
        "summary_style": "",
        "emphasis": [],
    },
}

# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_profile(profile: dict) -> list:
    """Validate a profile dict and return advisory warnings.

    Args:
        profile: Profile dict matching PROFILE_SCHEMA.

    Returns:
        List of warning dicts, each with keys: {field, message, severity}.
        Empty list means the profile is valid.
    """
    warnings = []
    positions = profile.get("positions", [])
    top_level_skills = set(profile.get("skills", []))

    for position in positions:
        company = position.get("company", "unknown")
        achievements = position.get("achievements", [])
        skills = position.get("skills", [])

        # Check: no achievements
        if not achievements:
            warnings.append({
                "field": f"positions[{company}].achievements",
                "message": f"Position at {company} has no achievements",
                "severity": "warning",
            })

        # Check: achievement without quantified impact (no numbers or %)
        for achievement in achievements:
            has_number = bool(re.search(r"\d+(?:[,\.]\d+)?[%x]?|\d+x", achievement))
            if not has_number:
                short = achievement[:50] + "..." if len(achievement) > 50 else achievement
                warnings.append({
                    "field": f"positions[{company}].achievements",
                    "message": f"Achievement lacks quantified impact: '{short}'",
                    "severity": "info",
                })

        # Check: skills in position not present in top-level skills list
        for skill in skills:
            if skill and skill not in top_level_skills:
                warnings.append({
                    "field": f"positions[{company}].skills",
                    "message": f"Skill '{skill}' in {company} position not in main skills list",
                    "severity": "info",
                })

        # Check: no skills tagged on position
        if not skills:
            warnings.append({
                "field": f"positions[{company}].skills",
                "message": f"Position at {company} has no skills tagged",
                "severity": "warning",
            })

    return warnings

# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------

def load_profile(profile_path: str = "experience_profile.json") -> dict:
    """Load the experience profile from a JSON file.

    Args:
        profile_path: Path to the profile JSON file.

    Returns:
        Profile dict, or empty structure if file doesn't exist.
    """
    path = Path(profile_path)
    if not path.exists():
        return dict(EMPTY_PROFILE)

    with open(path, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON in profile file {path}: {exc}") from exc

def save_profile(profile: dict, profile_path: str = "experience_profile.json", *, force: bool = False) -> None:
    """Save the experience profile to a JSON file.

    Safety guards (skipped when *force=True*):
    1. Refuses to overwrite a populated profile with an empty one (0 positions AND 0 skills).
    2. Refuses "suspicious reduction" — incoming has strictly fewer positions AND strictly
       fewer skills than existing — which signals an accidental wipe rather than intentional edit.

    Args:
        profile: Profile dict to save.
        profile_path: Path to write the profile JSON file.
        force: When True, bypass safety guards. Use for explicit user-initiated saves.
    """
    path = Path(profile_path)

    incoming_positions = profile.get("positions", [])
    incoming_skills = profile.get("skills", [])

    if not force and path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                existing = json.load(f)
            existing_positions = existing.get("positions", [])
            existing_skills = existing.get("skills", [])
        except (json.JSONDecodeError, ValueError):
            existing_positions = []
            existing_skills = []

        existing_has_data = len(existing_positions) > 0 or len(existing_skills) > 0

        # Guard 1: completely empty incoming over populated existing
        if existing_has_data and len(incoming_positions) == 0 and len(incoming_skills) == 0:
            logger.warning(
                "save_profile: refusing to overwrite populated profile (%d positions, %d skills) "
                "with empty data at %s. Save aborted.",
                len(existing_positions),
                len(existing_skills),
                profile_path,
            )
            return

        # Guard 2: suspicious reduction — both dimensions shrink
        if (existing_has_data
                and len(incoming_positions) < len(existing_positions)
                and len(incoming_skills) < len(existing_skills)):
            logger.warning(
                "save_profile: suspicious reduction detected (%d->%d positions, %d->%d skills) "
                "at %s. Save aborted. Use force=True for intentional changes.",
                len(existing_positions), len(incoming_positions),
                len(existing_skills), len(incoming_skills),
                profile_path,
            )
            return

    with open(path, "w", encoding="utf-8") as f:
        json.dump(profile, f, indent=2, ensure_ascii=False)

# ---------------------------------------------------------------------------
# Opus-powered markdown extraction
# ---------------------------------------------------------------------------

_EXTRACTION_PROMPT = """\
You are a professional resume parser. Extract structured experience data from the following resume/markdown text.

Return ONLY valid JSON (no markdown code fences, no explanation) matching this exact schema:
{
  "positions": [
    {
      "title": "Job Title",
      "company": "Company Name",
      "start_date": "MMM YYYY",
      "end_date": "MMM YYYY or null if current",
      "achievements": ["Bullet 1", "Bullet 2"],
      "skills": ["Skill1", "Skill2"]
    }
  ],
  "skills": ["Ordered list of all skills, most important first"],
  "resume_preferences": {
    "summary_style": "professional summary extracted or inferred from the document",
    "emphasis": ["Key theme 1", "Key theme 2"]
  }
}

Rules:
- Extract ALL positions from the document, most recent first
- For achievements, use the actual bullet text verbatim where possible
- For skills in each position, infer from the achievements/context
- For top-level skills, aggregate all unique skills ordered by how frequently they appear
- Do not fabricate information; only extract what's present
- If a field is not found, use an empty string or empty array

Resume/markdown to extract from:
"""

def extract_profile_from_markdown(markdown_text: str) -> dict:
    """Extract a structured profile from markdown text using Claude Opus.

    Args:
        markdown_text: Raw markdown resume/experience text.

    Returns:
        Profile dict matching PROFILE_SCHEMA, or an error dict with key 'error'.
    """
    try:
        from job_finder.web.claude_client import _run_oneshot

        envelope = _run_oneshot(
            model=DEFAULT_MODEL_OPUS,
            system="You extract structured profiles from resume text. Return valid JSON only.",
            user_message=_EXTRACTION_PROMPT + markdown_text,
            timeout=120,
        )

        response_text = envelope.get("result", "").strip()

        # Strip any accidental code fences
        if response_text.startswith("```"):
            lines = response_text.splitlines()
            response_text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

        extracted = json.loads(response_text)

        # Ensure required keys exist with proper types
        if "positions" not in extracted:
            extracted["positions"] = []
        if "skills" not in extracted:
            extracted["skills"] = []
        if "resume_preferences" not in extracted:
            extracted["resume_preferences"] = {"summary_style": "", "emphasis": []}

        return extracted

    except Exception as exc:  # noqa: BLE001
        logger.warning("extract_profile_from_markdown failed: %s", exc)
        return {"error": str(exc), "positions": [], "skills": [], "resume_preferences": {"summary_style": "", "emphasis": []}}
