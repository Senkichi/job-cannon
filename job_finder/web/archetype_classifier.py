"""Deterministic job archetype classification.

Classifies jobs into archetypes (e.g. platform_engineering, ml_engineering,
analytics_lead) based on keyword matching against job title and description.
Uses 'job_archetype' naming to avoid collision with resume-generation
'role_archetype' concepts.

Exports:
    classify_job_archetype: Classify a job into an archetype or None.
    get_job_archetype_weights: Get weight overrides for an archetype.
"""

import re


def classify_job_archetype(
    title: str,
    description: str,
    config: dict,
) -> str | None:
    """Classify a job into an archetype based on keyword matching.

    Scans title and description (case-insensitive) for configured keyword
    lists. Returns the first matching archetype, or None if no match.

    Args:
        title: Job title string.
        description: Job description text (may be truncated).
        config: Application config dict. Reads profile.job_archetypes.

    Returns:
        Archetype key string (e.g. 'platform_engineering'), or None.
    """
    archetypes = config.get("profile", {}).get("job_archetypes", {})
    if not archetypes:
        return None

    text = f"{title} {description}".lower()

    for archetype_key, archetype_cfg in archetypes.items():
        if not isinstance(archetype_cfg, dict):
            continue
        keywords = archetype_cfg.get("keywords", [])
        if not keywords:
            continue
        for keyword in keywords:
            if re.search(r"\b" + re.escape(keyword.lower()) + r"\b", text):
                return archetype_key

    return None


def get_job_archetype_weights(
    job_archetype: str | None,
    config: dict,
) -> dict:
    """Get weight overrides for a given archetype.

    Args:
        job_archetype: Archetype key, or None.
        config: Application config dict.

    Returns:
        Weight overrides dict, or empty dict if archetype is None or
        not found or has no overrides.
    """
    if not job_archetype:
        return {}

    archetypes = config.get("profile", {}).get("job_archetypes", {})
    archetype_cfg = archetypes.get(job_archetype, {})
    if not isinstance(archetype_cfg, dict):
        return {}

    return archetype_cfg.get("weight_overrides", {}) or {}
