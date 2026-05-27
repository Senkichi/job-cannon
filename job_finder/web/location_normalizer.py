"""Location normalization for job listings.

Pure functions. No I/O, no shared state. Used at two boundaries:

  - **Ingestion (write):** ``upsert_job`` calls ``normalize_location`` on
    each entry before storing in ``jobs.locations_raw`` so subsequent
    ingestions of the same job don't accumulate whitespace / casing /
    placeholder variants as distinct entries.

  - **Filter dropdown (read):** ``get_distinct_locations`` iterates each
    job's ``locations_raw`` JSON array (rather than the merged
    ``location`` string), normalizes every entry, and lower-case-dedupes
    so the dropdown shows a manageable set of canonical location values.

Conservative on purpose: trim + collapse whitespace, strip surrounding
punctuation, drop placeholder-only values ("Unknown", "TBD", ...). NO
aggressive case rewriting and NO multi-location splitting on plain
commas — both have ambiguous failure modes (mangling "CA" → "Ca",
splitting "Suite 100, San Francisco, CA" into garbage). Future work
can tighten this if dropdown pollution remains.

Parsers that produced multi-location strings via UNAMBIGUOUS separators
(`|`, ` / `, `;`, ` & `) ARE split here — those separators don't appear
inside real city/state pairs and produce clean per-location entries.
"""

from __future__ import annotations

import re

# Placeholder values that mean "no real location" — drop entirely.
# Match must be the entire normalized value (case-insensitive). Conservative:
# only includes unambiguous junk. "Anywhere", "Worldwide", "Global",
# "US" etc. are NOT placeholders — they're meaningful filter values.
_PLACEHOLDER_VALUES = frozenset(
    {
        "n/a",
        "na",
        "tbd",
        "tba",
        "unknown",
        "various",
        "varies",
        "multiple locations",
        "see job description",
        "see jd",
        "see description",
        "not specified",
        "none",
        "-",
        "--",
    }
)

# Collapse runs of whitespace to a single space.
_WS_RE = re.compile(r"\s+")

# Strip surrounding whitespace + punctuation. Includes hyphens and dashes
# because some parsers emit bare "- " or "—" as a location placeholder.
_TRAILING_PUNCT_RE = re.compile(r"^[\s\-–—.,;:|]+|[\s\-–—.,;:|]+$")

# Multi-location separators that NEVER appear inside a real "City, State"
# fragment. Splitting on these is safe; splitting on plain "," is not
# (would break city/state pairs).
_MULTI_LOC_SEP_RE = re.compile(r"\s*[|;]\s*|\s+/\s+|\s+&\s+|\s+or\s+", re.IGNORECASE)


def normalize_location(raw: str | None) -> str | None:
    """Canonicalize a single location string.

    Args:
        raw: The location text as captured by a parser.

    Returns:
        The normalized location, or ``None`` when the input is empty or a
        recognized placeholder ("Unknown", "N/A", "TBD", ...). The
        returned value is whitespace-trimmed, whitespace-collapsed, and
        stripped of leading / trailing punctuation. Case is preserved
        (no aggressive title-casing — see module docstring).
    """
    if not raw:
        return None
    cleaned = _TRAILING_PUNCT_RE.sub("", raw)
    cleaned = _WS_RE.sub(" ", cleaned).strip()
    if not cleaned:
        return None
    if cleaned.lower() in _PLACEHOLDER_VALUES:
        return None
    return cleaned


def split_multi_locations(raw: str | None) -> list[str]:
    """Split a parser-captured location on unambiguous multi-location
    separators (`|`, `;`, ` / `, ` & `, ` or `), then normalize each part.

    Returns the de-duplicated list of normalized entries (preserving
    first-seen order). Returns an empty list when the input is empty or
    a pure placeholder.

    Plain commas are NOT a separator — they're part of "City, State"
    fragments. Comma-separated lists of cities will appear as a single
    entry; that's an acceptable trade-off (rare in practice; ambiguous
    to split safely).
    """
    if not raw:
        return []
    parts = _MULTI_LOC_SEP_RE.split(raw)
    out: list[str] = []
    seen: set[str] = set()
    for part in parts:
        normalized = normalize_location(part)
        if normalized is None:
            continue
        key = normalized.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(normalized)
    return out
