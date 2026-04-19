"""Post-hoc score calibration via isotonic regression breakpoints.

Loads (provider, tier)-specific calibration tables fitted offline from eval
data and applies linear interpolation to map raw model scores back onto the
baseline scale. Calibration files live alongside this module as
`calibration_<provider>_<tier>.json` and must declare `provider`, `tier`, and
`breakpoints` (a list of `[raw, calibrated]` pairs sorted by raw score).

Tables are scoped to a `(provider, tier)` pair because the calibration curve
for Ollama's Sonnet-scale output differs from its Haiku-scale output. Prior
wiring keyed on provider alone and applied a Sonnet-fit table to Haiku
scores, which silently miscalibrated the fast-filter tier — the tier-scoped
lookup makes that mismatch impossible to reintroduce.

Usage:

    from job_finder.web.score_calibration import calibrate_score

    raw = 72  # raw Ollama Sonnet score
    calibrated = calibrate_score(raw, "ollama", tier="sonnet")  # ~57
"""

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Lazy-loaded calibration tables keyed by (provider, tier) tuple.
_tables: dict[tuple[str, str], list[list[float]]] = {}
_loaded = False

_CALIBRATION_DIR = Path(__file__).parent


def _load_tables() -> None:
    """Load all calibration_*.json files from the web package directory.

    Each file must define provider, tier, and at least two breakpoints.
    Malformed or incomplete files are skipped with a warning so a bad
    calibration artifact cannot break startup.
    """
    global _tables, _loaded
    _tables = {}
    for path in _CALIBRATION_DIR.glob("calibration_*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Failed to load calibration file %s: %s", path.name, exc)
            continue
        provider = data.get("provider")
        tier = data.get("tier")
        breakpoints = data.get("breakpoints")
        if not provider or not tier:
            logger.warning(
                "Calibration file %s missing provider/tier; skipping", path.name,
            )
            continue
        if not isinstance(breakpoints, list) or len(breakpoints) < 2:
            logger.warning(
                "Calibration file %s has fewer than 2 breakpoints; skipping", path.name,
            )
            continue
        _tables[(provider, tier)] = breakpoints
        logger.debug(
            "Loaded calibration for %s/%s (%d breakpoints)",
            provider, tier, len(breakpoints),
        )
    _loaded = True


def _interpolate(score: float, breakpoints: list[list[float]]) -> float:
    """Linearly interpolate between breakpoints; clamp to endpoints."""
    if score <= breakpoints[0][0]:
        return breakpoints[0][1]
    if score >= breakpoints[-1][0]:
        return breakpoints[-1][1]
    for i in range(len(breakpoints) - 1):
        x0, y0 = breakpoints[i]
        x1, y1 = breakpoints[i + 1]
        if x0 <= score <= x1:
            t = (score - x0) / (x1 - x0) if x1 != x0 else 0.0
            return y0 + t * (y1 - y0)
    return score  # unreachable given endpoint clamps


def calibrate_score(raw_score: float | int, provider: str, tier: str) -> float:
    """Apply isotonic calibration to a raw model score.

    Args:
        raw_score: Raw score from the model (0-100 scale).
        provider: Provider name (e.g. "ollama", "gemini").
        tier: Logical tier the score came from ("haiku" | "sonnet" | "opus").

    Returns:
        Calibrated score on the baseline scale. Returns the raw score
        unchanged if no table exists for the (provider, tier) pair.
    """
    global _loaded
    if not _loaded:
        _load_tables()
    breakpoints = _tables.get((provider, tier))
    if breakpoints is None:
        return float(raw_score)
    return round(_interpolate(float(raw_score), breakpoints), 1)


def has_calibration(provider: str, tier: str) -> bool:
    """Return True iff a calibration table is registered for (provider, tier)."""
    global _loaded
    if not _loaded:
        _load_tables()
    return (provider, tier) in _tables


def reload_tables() -> None:
    """Force a fresh load — useful for tests that write new calibration files."""
    global _loaded
    _loaded = False
    _load_tables()
