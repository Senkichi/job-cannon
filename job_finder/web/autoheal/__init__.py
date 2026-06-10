"""Parser auto-heal — Phase A (observability only).

Captures a PII-scrubbed rolling corpus of real parser inputs/outputs and tracks
per-source health so a structural break surfaces on the dashboard. No heal, no
LLM in this phase. See .planning/specs/2026-06-06-parser-auto-heal-design.md.
"""

# Detection tuning (see plan "Break rule").
MIN_MEANINGFUL_LEN = 200  # inputs shorter than this never count as a break (meta/empty emails)
BREAK_THRESHOLD = 3  # consecutive baseline-violating zero-yields → DEGRADED
BASELINE_WINDOW = 20  # how many recent non-zero samples define baseline_yield


def surface_for_source(source: str) -> str:
    """Map a source key to its heal surface: ats:* → ats, careers/careers:* → careers, else email."""
    if source.startswith("ats:"):
        return "ats"
    if source == "careers" or source.startswith("careers:"):
        return "careers"
    return "email"
