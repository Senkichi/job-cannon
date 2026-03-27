"""Resume style guide module -- load, save, extract, and build directives.

Provides:
    _STYLE_GUIDE_PATH     -- Default path for resume_style_guide.json
    STYLE_GUIDE_SCHEMA    -- JSON schema for structured Sonnet style extraction output
    load_style_guide      -- Load style guide from JSON file (returns {} if missing)
    save_style_guide      -- Save style guide dict to JSON file
    _build_style_guide_directives -- Convert guide dict to list of prompt directive strings
    extract_style_guide   -- Call Sonnet to extract/merge style preferences from resume text
    merge_guidelines_into_guide  -- Merge guidelines text into existing guide via Sonnet (helper)
    migrate_style_guide   -- Populate new structured fields from guidelines doc via Sonnet
"""

import json
import logging
import sqlite3

import anthropic

from job_finder.web.model_provider import call_model

logger = logging.getLogger(__name__)

_STYLE_GUIDE_PATH = "resume_style_guide.json"

STYLE_GUIDE_SCHEMA = {
    "type": "object",
    "properties": {
        "bullet_style": {"type": "string"},
        "verb_tense": {"type": "string"},
        "section_order": {"type": "array", "items": {"type": "string"}},
        "tone": {"type": "string"},
        "date_format": {"type": "string"},
        "summary_style": {"type": "string"},
        "summary_formula": {"type": "string"},
        "skills_format": {"type": "string"},
        "bullet_formula": {"type": "string"},
        "bullet_counts": {
            "type": "object",
            "properties": {
                "current": {"type": "string"},
                "previous": {"type": "string"},
                "prior": {"type": "string"},
                "early": {"type": "string"},
            },
        },
        "confidentiality_rules": {"type": "string"},
        "typography_rules": {"type": "string"},
        "jd_mirroring_rules": {"type": "string"},
        "anti_patterns": {"type": "array", "items": {"type": "string"}},
        "role_archetype": {"type": "string"},
    },
    "required": ["bullet_style", "verb_tense", "section_order", "tone", "date_format"],
    "additionalProperties": False,
}

# Human-readable labels for each field
FIELD_LABELS = {
    "bullet_style": "Bullet style",
    "verb_tense": "Verb tense",
    "section_order": "Section order",
    "tone": "Tone",
    "date_format": "Date format",
    "summary_style": "Summary style",
    "summary_formula": "Summary formula",
    "skills_format": "Skills format",
    "bullet_formula": "Bullet formula",
    "bullet_counts": "Bullet counts",
    "confidentiality_rules": "Confidentiality rules",
    "typography_rules": "Typography rules",
    "jd_mirroring_rules": "JD mirroring rules",
    "anti_patterns": "Anti-patterns",
    "role_archetype": "Role archetype",
}


def load_style_guide(path: str = _STYLE_GUIDE_PATH) -> dict:
    """Load style guide from JSON file.

    Args:
        path: Path to the JSON file. Defaults to resume_style_guide.json.

    Returns:
        Dict with style guide data, or {} if file does not exist.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.warning("load_style_guide: failed to load '%s': %s", path, e)
        return {}


def save_style_guide(guide: dict, path: str = _STYLE_GUIDE_PATH) -> None:
    """Save style guide dict to JSON file.

    Args:
        guide: Style guide data dict.
        path: Path to write to. Defaults to resume_style_guide.json.
    """
    with open(path, "w", encoding="utf-8") as f:
        json.dump(guide, f, indent=2, ensure_ascii=False)


def _build_style_guide_directives(guide: dict) -> list[str]:
    """Convert a style guide dict to a list of formatted prompt directive strings.

    Args:
        guide: Style guide data dict (may be empty).

    Returns:
        List of formatted strings like "Bullet style: dashes".
        Returns empty list if guide is empty or has no non-empty fields.
    """
    if not guide:
        return []

    directives = []
    for field, label in FIELD_LABELS.items():
        value = guide.get(field)
        if not value:
            continue
        if isinstance(value, dict):
            parts = [f"{k} {v}" for k, v in value.items() if v]
            if parts:
                directives.append(f"{label}: {', '.join(parts)}")
        elif isinstance(value, list):
            if value:
                directives.append(f"{label}: {', '.join(value)}")
        else:
            if str(value).strip():
                directives.append(f"{label}: {value}")

    return directives


def extract_style_guide(
    raw_text: str,
    existing_guide: dict,
    conn: sqlite3.Connection,
    config: dict,
) -> dict | None:
    """Extract formatting style preferences from resume text using Sonnet.

    If existing_guide is non-empty, instructs Sonnet to synthesize/merge the
    new text's style with the existing guide rather than replacing it.

    Args:
        raw_text: Raw text extracted from the uploaded PDF resume.
        existing_guide: Current style guide dict (may be empty for first upload).
        conn: Open SQLite connection for cost recording.
        config: Application YAML config dict (reads scoring.models.sonnet).

    Returns:
        Dict matching STYLE_GUIDE_SCHEMA with extracted style preferences.
        None on error (caller must handle failure).
    """
    try:
        client = anthropic.Anthropic()

        if existing_guide:
            system = (
                "You are a resume style analyst. Analyze the resume text and extract formatting "
                "and style preferences. You also have an existing style guide from prior uploads. "
                "Your task is to SYNTHESIZE and MERGE the new resume's style with the existing "
                "guide -- preserve existing preferences where the new resume confirms them, and "
                "update or add preferences where the new resume provides new information. "
                "Do not replace existing preferences unless the new resume clearly contradicts them. "
                "Return a unified style guide that represents the candidate's overall style."
            )
            user_message = (
                f"## Resume Text to Analyze\n\n"
                f"{raw_text}\n\n"
                f"---\n\n"
                f"## Existing Style Guide (from prior uploads)\n\n"
                f"```json\n{json.dumps(existing_guide, indent=2)}\n```\n\n"
                f"Synthesize a merged style guide that combines both sources."
            )
        else:
            system = (
                "You are a resume style analyst. Analyze the resume text and extract formatting "
                "and style preferences. Identify the candidate's consistent formatting choices "
                "including bullet style, verb tense, section ordering, overall tone, date format, "
                "summary style, and any notable consistency patterns."
            )
            user_message = (
                f"## Resume Text to Analyze\n\n"
                f"{raw_text}\n\n"
                f"Extract the formatting and style preferences from this resume."
            )

        result_obj = call_model(
            tier="sonnet",
            system=system,
            messages=[{"role": "user", "content": user_message}],
            conn=conn,
            config=config,
            output_schema=STYLE_GUIDE_SCHEMA,
            job_id=None,
            purpose="resume_style_extraction",
            max_tokens=1024,
            client=client,
        )
        return result_obj.data

    except Exception as e:
        logger.warning("extract_style_guide: failed: %s", e)
        return None


def merge_guidelines_into_guide(
    guidelines_text: str,
    existing_guide: dict,
    client,
    model: str,
    conn: sqlite3.Connection,
    config: dict,
    mode: str = "populate_new",
    purpose: str = "guidelines_merge",
) -> dict | None:
    """Merge guidelines text into an existing style guide via a Sonnet call.

    Args:
        guidelines_text: Raw text of the resume generation guidelines document.
        existing_guide: Current style guide dict (may be empty).
        client: Anthropic client instance (injected for testability).
        model: Full model identifier, e.g. "claude-sonnet-4-6".
        conn: Open SQLite connection for cost recording.
        config: Application YAML config dict.
        mode: Merge mode — "populate_new" only fills missing/empty fields;
              "merge_updates" also overwrites fields where guidelines provide
              different or improved guidance.
        purpose: Cost attribution label. Defaults to "guidelines_merge".
                 Pass "style_guide_migration" when called from migrate_style_guide.

    Returns:
        Merged style guide dict matching STYLE_GUIDE_SCHEMA, or None on error.
    """
    try:
        if mode == "populate_new":
            system = (
                "You are a resume style analyst. You have a resume generation guidelines document "
                "and an existing style guide JSON. Your task is to MERGE the guidelines into the "
                "style guide by populating the new structured fields (summary_formula, skills_format, "
                "bullet_formula, bullet_counts, confidentiality_rules, typography_rules, "
                "jd_mirroring_rules, anti_patterns, role_archetype) based on the guidelines document. "
                "PRESERVE all existing field values exactly as they are. Only populate new fields "
                "that are currently missing or empty. Return the complete merged style guide."
            )
        else:  # merge_updates
            system = (
                "You are a resume style analyst. You have updated resume generation guidelines "
                "and an existing style guide JSON. Your task is to MERGE the updated guidelines "
                "into the style guide. Update fields where the new guidelines provide different "
                "or improved guidance. Preserve fields the new guidelines don't address. "
                "Overwrite fields that the new guidelines explicitly change. "
                "Return the complete merged style guide."
            )

        user_message = (
            f"## Resume Generation Guidelines\n\n"
            f"{guidelines_text}\n\n"
            f"---\n\n"
            f"## Existing Style Guide\n\n"
            f"```json\n{json.dumps(existing_guide, indent=2)}\n```\n\n"
            f"Merge the guidelines into the style guide."
        )

        result_obj = call_model(
            tier="sonnet",
            system=system,
            messages=[{"role": "user", "content": user_message}],
            conn=conn,
            config=config,
            output_schema=STYLE_GUIDE_SCHEMA,
            job_id=None,
            purpose=purpose,
            max_tokens=2048,
            client=client,
        )

        return result_obj.data

    except Exception as e:
        logger.warning("merge_guidelines_into_guide: failed (mode=%s): %s", mode, e)
        return None


def migrate_style_guide(
    config: dict,
    conn: sqlite3.Connection,
    style_guide_path: str = _STYLE_GUIDE_PATH,
) -> dict | None:
    """Migrate existing style guide to include new structured guideline fields.

    Reads docs/resume_generation_guidelines.md and the existing style guide,
    sends both to Sonnet with output_schema=STYLE_GUIDE_SCHEMA, and saves
    the merged result. Idempotent -- safe to run multiple times.

    Args:
        config: Application YAML config dict.
        conn: Open SQLite connection for cost recording.
        style_guide_path: Path to resume_style_guide.json. Defaults to _STYLE_GUIDE_PATH.

    Returns:
        Merged style guide dict, or None on error.
    """
    try:
        from pathlib import Path

        guidelines_path = Path(__file__).resolve().parent.parent.parent / "docs" / "resume_generation_guidelines.md"
        guidelines_text = guidelines_path.read_text(encoding="utf-8")

        existing_guide = load_style_guide(style_guide_path)

        client = anthropic.Anthropic()

        result = merge_guidelines_into_guide(
            guidelines_text=guidelines_text,
            existing_guide=existing_guide,
            client=client,
            model="",
            conn=conn,
            config=config,
            mode="populate_new",
            purpose="style_guide_migration",
        )

        if result:
            save_style_guide(result, style_guide_path)
            logger.info("migrate_style_guide: saved merged guide with %d fields", len(result))

        return result

    except Exception as e:
        logger.warning("migrate_style_guide: failed: %s", e)
        return None
