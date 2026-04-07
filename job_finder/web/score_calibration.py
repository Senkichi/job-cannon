"""Post-hoc score calibration via isotonic regression breakpoints.

Loads provider-specific calibration tables (fitted offline from eval data)
and applies linear interpolation to map raw model scores to the Opus scale.

Calibration files are JSON with a 'breakpoints' key: list of [raw, calibrated]
pairs, sorted by raw score. Scores between breakpoints are linearly interpolated;
scores outside the range are clamped to the nearest endpoint.

Usage:
    from job_finder.web.score_calibration import calibrate_score

    raw = 72  # from Ollama
    calibrated = calibrate_score(raw, "ollama")  # -> ~57 (Opus-equivalent)
"""

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Lazy-loaded calibration tables: {provider: [[raw, cal], ...]}
_tables: dict[str, list[list[float]]] = {}
_loaded = False

_CALIBRATION_DIR = Path(__file__).parent


def _load_tables() -> None:
    """Load all calibration_*.json files from the web package directory."""
    global _tables, _loaded
    _tables = {}
    for path in _CALIBRATION_DIR.glob("calibration_*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            provider = data.get("provider")
            breakpoints = data.get("breakpoints")
            if provider and breakpoints and len(breakpoints) >= 2:
                _tables[provider] = breakpoints
                logger.debug("Loaded calibration for %s (%d breakpoints)", provider, len(breakpoints))
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            logger.warning("Failed to load calibration file %s: %s", path.name, exc)
    _loaded = True


def _interpolate(score: float, breakpoints: list[list[float]]) -> float:
    """Linearly interpolate between breakpoints."""
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
    return score  # Shouldn't reach here


def calibrate_score(raw_score: float | int, provider: str) -> float:
    """Apply isotonic calibration to a raw model score.

    Args:
        raw_score: Raw score from the model (0-100 scale).
        provider: Provider name (e.g., "ollama", "gemini").

    Returns:
        Calibrated score on the Opus scale. Returns the raw score unchanged
        if no calibration table exists for the provider.
    """
    global _loaded
    if not _loaded:
        _load_tables()

    breakpoints = _tables.get(provider)
    if breakpoints is None:
        return float(raw_score)

    calibrated = _interpolate(float(raw_score), breakpoints)
    return round(calibrated, 1)


def has_calibration(provider: str) -> bool:
    """Check whether a calibration table is available for a provider."""
    global _loaded
    if not _loaded:
        _load_tables()
    return provider in _tables
