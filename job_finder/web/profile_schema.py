"""Profile schema definition, validation, and I/O utilities.

Provides:
    PROFILE_SCHEMA  -- Reference dict documenting expected experience_profile.json structure
    validate_profile(profile) -> list[dict]   -- Returns list of warning dicts
    load_profile(path) -> dict                -- Load JSON file (returns empty structure if missing)
    save_profile(profile, path) -> None       -- Write JSON file with indent=2 (with empty-overwrite guard)
"""

import json
import logging
import re
from pathlib import Path

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
}

# ---------------------------------------------------------------------------
# Empty / default profile structure
# ---------------------------------------------------------------------------

EMPTY_PROFILE = {
    "positions": [],
    "skills": [],
    "education": [],
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
            warnings.append(
                {
                    "field": f"positions[{company}].achievements",
                    "message": f"Position at {company} has no achievements",
                    "severity": "warning",
                }
            )

        # Check: achievement without quantified impact (no numbers or %)
        for achievement in achievements:
            has_number = bool(re.search(r"\d+(?:[,\.]\d+)?[%x]?|\d+x", achievement))
            if not has_number:
                short = achievement[:50] + "..." if len(achievement) > 50 else achievement
                warnings.append(
                    {
                        "field": f"positions[{company}].achievements",
                        "message": f"Achievement lacks quantified impact: '{short}'",
                        "severity": "info",
                    }
                )

        # Check: skills in position not present in top-level skills list
        for skill in skills:
            if skill and skill not in top_level_skills:
                warnings.append(
                    {
                        "field": f"positions[{company}].skills",
                        "message": f"Skill '{skill}' in {company} position not in main skills list",
                        "severity": "info",
                    }
                )

        # Check: no skills tagged on position
        if not skills:
            warnings.append(
                {
                    "field": f"positions[{company}].skills",
                    "message": f"Position at {company} has no skills tagged",
                    "severity": "warning",
                }
            )

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

    with open(path, encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON in profile file {path}: {exc}") from exc


def save_profile(
    profile: dict, profile_path: str = "experience_profile.json", *, force: bool = False
) -> None:
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
            with open(path, encoding="utf-8") as f:
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
        if (
            existing_has_data
            and len(incoming_positions) < len(existing_positions)
            and len(incoming_skills) < len(existing_skills)
        ):
            logger.warning(
                "save_profile: suspicious reduction detected (%d->%d positions, %d->%d skills) "
                "at %s. Save aborted. Use force=True for intentional changes.",
                len(existing_positions),
                len(incoming_positions),
                len(existing_skills),
                len(incoming_skills),
                profile_path,
            )
            return

    with open(path, "w", encoding="utf-8") as f:
        json.dump(profile, f, indent=2, ensure_ascii=False)
