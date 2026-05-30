"""Deterministic salary extraction from job-description text.

Pure functions. No I/O, no LLM, no shared state. Used as a fast-path
ahead of the LLM-based parse_structured_fields in data_enricher's
_apply_post_fetch_extraction. When the regex matches a plausible
range, we skip the LLM call (saves API spend + always-deterministic
result). When it doesn't, the caller falls back to the LLM.

The salary regexes intentionally cover the common JD formats and
reject implausible matches (hourly rates, funding numbers, version
strings) via a [$30K, $5M] plausibility filter on the annual-USD
expansion.

Formats handled:

    $120K - $150K            (explicit dollars both sides, K/M optional)
    $120,000 - $150,000      (full dollars)
    $120K-150K               (dollar only on left, K-suffix elision)
    $120K to $150K           (word 'to' as range separator)
    USD 120,000 - 150,000    (currency-code prefix)
    USD 120K to USD 150K     (currency code both sides)
    "salary range: 120K-150K"  (context-anchored, no $ required)
    "compensation: 140K-180K"
    "pay range: 120K to 150K"

Single-value salaries ('$120K base', 'up to $150K') are NOT extracted —
they could mean min, max, or midpoint, and the caller schema requires
unambiguous min OR max attribution. Future work could add directional
hints ('starting at' -> min, 'up to' -> max) if needed.
"""

from __future__ import annotations

import re

# Plausibility filter for annual-USD salary. Rejects hourly rates ($15/hr
# is ~$31K but the parser shouldn't trip on standalone $15), funding
# numbers ("$10M Series B"), and version strings ("Python 3.12-3.13").
_MIN_PLAUSIBLE_SALARY = 30_000
_MAX_PLAUSIBLE_SALARY = 5_000_000

# Range patterns. Ordered by specificity — the first match wins because
# the more-specific patterns are also less likely to false-positive on
# generic numeric ranges. All capture (low, low_unit, high, high_unit).
_SALARY_PATTERNS: list[re.Pattern[str]] = [
    # "$120K - $150K" / "$120,000 - $150,000" / "$120K to $150K"
    # Both sides have $, K/M optional, range separator is dash or "to".
    re.compile(
        r"\$\s*(?P<low>\d[\d,]*\.?\d*)\s*(?P<low_unit>[KkMm])?"
        r"\s*(?:to|-|–|—)\s*"
        r"\$\s*(?P<high>\d[\d,]*\.?\d*)\s*(?P<high_unit>[KkMm])?",
    ),
    # "$120K-150K" / "$120K to 150K" — single $ on the left side. K/M
    # required on both sides to avoid matching ambiguous numeric ranges
    # like "$5 - 10 employees".
    re.compile(
        r"\$\s*(?P<low>\d[\d,]*\.?\d*)\s*(?P<low_unit>[KkMm])"
        r"\s*(?:to|-|–|—)\s*"
        r"(?P<high>\d[\d,]*\.?\d*)\s*(?P<high_unit>[KkMm])",
    ),
    # "USD 120,000 - 150,000" / "USD 120K to USD 150K". Allow USD prefix
    # on both sides or just the first.
    re.compile(
        r"USD\s*(?P<low>\d[\d,]*\.?\d*)\s*(?P<low_unit>[KkMm])?"
        r"\s*(?:to|-|–|—)\s*"
        r"(?:USD\s*)?(?P<high>\d[\d,]*\.?\d*)\s*(?P<high_unit>[KkMm])?",
    ),
    # Context-anchored no-dollar range: "salary range: 120K-150K" /
    # "compensation: 140K to 180K". Requires K/M suffix on the LOW side
    # to keep false positives down (skip "rotation: 3-5 months", etc.).
    re.compile(
        r"(?:salary|compensation|base\s+pay|pay\s+range|comp\s+range|hiring\s+range|pay\s+band)"
        r"[^\n\r]{0,30}?"  # up to 30 chars of intervening words/punct
        r"(?P<low>\d[\d,]*\.?\d*)\s*(?P<low_unit>[KkMm])"
        r"\s*(?:to|-|–|—)\s*"
        r"(?P<high>\d[\d,]*\.?\d*)\s*(?P<high_unit>[KkMm])?",
        re.IGNORECASE,
    ),
]


def _expand_amount(raw: str, unit: str | None) -> float | None:
    """Convert '120K' / '1.5M' / '150,000' to a float in dollars.

    Returns None on parse error.
    """
    try:
        val = float(raw.replace(",", ""))
    except ValueError:
        return None
    if unit:
        upper = unit.upper()
        if upper == "K":
            val *= 1_000
        elif upper == "M":
            val *= 1_000_000
    return val


def extract_salary_from_text(text: str | None) -> tuple[int | None, int | None]:
    """Heuristic regex pass at salary extraction from JD text.

    Returns (salary_min, salary_max) as integer annual USD when a
    plausible range is found, or (None, None) otherwise. Both values
    are guaranteed populated when the result is not (None, None) —
    callers can rely on either both-present-or-neither semantics.

    Plausibility filter: values must fall within [$30K, $5M]. Range
    must have ``low <= high`` (swapped automatically if the regex
    captures them in the wrong order). Both-under-1000 K-elision
    rule: if both extracted values are < 1000 and no K/M unit was
    captured, assume both are in thousands.
    """
    if not text:
        return None, None
    for pattern in _SALARY_PATTERNS:
        for match in pattern.finditer(text):
            low = _expand_amount(match.group("low"), match.group("low_unit"))
            high = _expand_amount(match.group("high"), match.group("high_unit"))
            if low is None or high is None:
                continue
            # K-elision: "$120-150" with no K/M units on either side and
            # both values plausible-as-thousands.
            both_units_missing = not match.group("low_unit") and not match.group("high_unit")
            if both_units_missing and low < 1000 and high < 1000:
                low *= 1000
                high *= 1000
            # Normalize order
            if low > high:
                low, high = high, low
            # Plausibility filter
            if low < _MIN_PLAUSIBLE_SALARY or high > _MAX_PLAUSIBLE_SALARY:
                continue
            return int(low), int(high)
    return None, None
