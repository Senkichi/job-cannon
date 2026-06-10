"""Shared field-alias helpers for JSON job-posting extraction.

Provides the canonical key lists and first-match-wins extractors shared
between ``careers_page_interactions.py`` (generic careers-page AI navigator)
and the ATS platform scanners (Greenhouse, Lever, …).

**Key ordering matters — first-match-wins.**
Each platform's real key must appear *before* any alias:

- ``JOB_TITLE_FIELDS``: ``title`` (Greenhouse), ``text`` (Lever), then
  broader aliases.
- ``JOB_URL_FIELDS``: ``url`` generic, then ``hostedUrl`` (Lever),
  ``absolute_url`` (Greenhouse), then broader aliases.

Adding a new key that belongs between existing ones? Insert it in the
correct position — do not append to the end.

C2 additions — override-aware resolvers:
``resolve_title``, ``resolve_url``, ``resolve_job_array`` consult
``override_loader.ats_alias(f"ats:{platform}")`` and merge any extra aliases
**after** the canonical list (first-match-wins on un-renamed data preserved).
``extract_field`` / ``find_job_array`` themselves are unchanged.
``careers_page_interactions.py`` stays on the canonical helpers — do not wire
it to the resolvers (it is not platform-keyed and is out of ATS-override scope).
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Canonical key lists (shared across surfaces)
# ---------------------------------------------------------------------------

JOB_ARRAY_KEYS: list[str] = [
    "jobs",
    "results",
    "data",
    "positions",
    "openings",
    "postings",
    "items",
    "jobPostings",
    "records",
    "hits",
]

# Title key priority:
#   title        — Greenhouse, generic
#   text         — Lever
#   name         — generic fallback
#   jobTitle     — schema.org / some custom platforms
#   job_title    — underscore variant
#   positionTitle — some legacy APIs
#   role         — informal alias
#   position     — generic fallback
JOB_TITLE_FIELDS: list[str] = [
    "title",
    "text",
    "name",
    "jobTitle",
    "job_title",
    "positionTitle",
    "role",
    "position",
]

# URL key priority:
#   url           — generic
#   hostedUrl     — Lever
#   absolute_url  — Greenhouse
#   applyUrl      — schema.org / some custom platforms
#   apply_url     — underscore variant
#   link / href   — generic HTML-derived
#   detailUrl / detail_url / jobUrl / canonicalUrl — misc aliases
JOB_URL_FIELDS: list[str] = [
    "url",
    "hostedUrl",
    "absolute_url",
    "applyUrl",
    "apply_url",
    "link",
    "href",
    "detailUrl",
    "detail_url",
    "jobUrl",
    "canonicalUrl",
]


# ---------------------------------------------------------------------------
# Extraction helpers
# ---------------------------------------------------------------------------


def extract_field(obj: dict, field_names: list[str]):
    """Return the first non-falsy value from *obj* keyed by *field_names*.

    Returns ``None`` when none of the keys are present or all mapped values
    are falsy.  Callers that need a guaranteed ``str`` should coalesce:
    ``extract_field(obj, JOB_TITLE_FIELDS) or ""``.
    """
    for name in field_names:
        if obj.get(name):
            return obj[name]
    return None


def find_job_array(data) -> list | None:
    """Return the array of job-posting dicts from a parsed JSON response.

    Handles three shapes:
    - Direct list: ``[{...}, ...]``
    - Top-level keyed: ``{"jobs": [...]}``
    - One-level nested: ``{"data": {"jobs": [...]}}``

    Returns ``None`` when no recognisable job array is found.
    """
    if isinstance(data, list):
        return data if data and isinstance(data[0], dict) else None

    if isinstance(data, dict):
        for key in JOB_ARRAY_KEYS:
            if key in data and isinstance(data[key], list):
                return data[key]

        # Check nested: {data: {jobs: [...]}} or {results: {items: [...]}}
        for outer_key in ("data", "results", "response", "body"):
            if outer_key in data and isinstance(data[outer_key], dict):
                inner = data[outer_key]
                for key in JOB_ARRAY_KEYS:
                    if key in inner and isinstance(inner[key], list):
                        return inner[key]

    return None


# ---------------------------------------------------------------------------
# Override-aware resolvers (C2) — ATS platform surface only
# ---------------------------------------------------------------------------
# These are the resolvers used by Greenhouse and Lever platform scanners.
# ``careers_page_interactions.py`` stays on the canonical helpers above —
# it is not platform-keyed and is out of ATS-override scope.
#
# Contract: canonical list is searched FIRST; override extras are appended
# after, so first-match-wins on un-renamed data is always preserved.
# With no override file the result is byte-for-byte identical to calling
# ``extract_field`` / ``find_job_array`` directly.


def resolve_title(posting: dict, platform: str):
    """Return the job title from *posting*, consulting the ATS alias override for *platform*.

    Searches canonical ``JOB_TITLE_FIELDS`` first, then any extra ``title_fields``
    from the platform's ATS alias recipe.  With no override the result is identical
    to ``extract_field(posting, JOB_TITLE_FIELDS)``.

    Args:
        posting: A single job-posting dict.
        platform: Platform name without the ``ats:`` prefix (e.g. ``"lever"``).

    Returns:
        The first non-falsy title value, or ``None`` when none are found.
    """
    from job_finder.web.autoheal import override_loader

    recipe = override_loader.ats_alias(f"ats:{platform}")
    extra = recipe.title_fields if recipe is not None else []
    return extract_field(posting, JOB_TITLE_FIELDS + extra)


def resolve_url(posting: dict, platform: str):
    """Return the job URL from *posting*, consulting the ATS alias override for *platform*.

    Searches canonical ``JOB_URL_FIELDS`` first, then any extra ``url_fields``
    from the platform's ATS alias recipe.  With no override the result is identical
    to ``extract_field(posting, JOB_URL_FIELDS)``.

    Args:
        posting: A single job-posting dict.
        platform: Platform name without the ``ats:`` prefix (e.g. ``"greenhouse"``).

    Returns:
        The first non-falsy URL value, or ``None`` when none are found.
    """
    from job_finder.web.autoheal import override_loader

    recipe = override_loader.ats_alias(f"ats:{platform}")
    extra = recipe.url_fields if recipe is not None else []
    return extract_field(posting, JOB_URL_FIELDS + extra)


def resolve_job_array(data, platform: str) -> list | None:
    """Return the job array from *data*, consulting the ATS alias override for *platform*.

    Searches canonical ``JOB_ARRAY_KEYS`` via ``find_job_array`` first, then tries
    any extra ``array_keys`` from the platform's ATS alias recipe on the top-level
    dict keys.  With no override the result is identical to ``find_job_array(data)``.

    Note: the nested-dict path (``data → outer_key → inner_key``) uses the canonical
    ``find_job_array`` only — override extras are applied at the top level only.

    Args:
        data: A parsed JSON response (list or dict).
        platform: Platform name without the ``ats:`` prefix (e.g. ``"greenhouse"``).

    Returns:
        The job array, or ``None`` when none are found.
    """
    from job_finder.web.autoheal import override_loader

    # Canonical path first — preserves first-match-wins on un-renamed data.
    canonical_result = find_job_array(data)
    if canonical_result is not None:
        return canonical_result

    recipe = override_loader.ats_alias(f"ats:{platform}")
    if recipe is None or not recipe.array_keys:
        return None

    # Try override extra keys at the top level only (dict shape).
    if isinstance(data, dict):
        for key in recipe.array_keys:
            if key in data and isinstance(data[key], list):
                return data[key]

    return None
