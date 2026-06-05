"""CI grep gate: all jd_full writes route through set_jd_full().

Scans job_finder/web/ and job_finder/db/ for raw
``UPDATE jobs SET jd_full = ...`` statements, excluding:
  - job_finder/db/_jd_full.py  (the sanctioned write path itself)
  - job_finder/web/migrations/  (historical one-off data fixes)

Any match → test fails with a message listing offending file:line so the
developer knows exactly what to fix (route through set_jd_full()).

Uses pathlib + re (no rg dependency) so it passes in all CI environments.

Reference: Phase 46.03 acceptance criteria.
"""

from __future__ import annotations

import re
from pathlib import Path

_JOB_FINDER_ROOT = Path(__file__).resolve().parents[1] / "job_finder"

# Matches UPDATE jobs SET jd_full = (with optional whitespace between tokens).
# Intentionally does NOT use re.DOTALL so multi-line strings where jd_full is
# NOT the first SET column are not accidentally matched.
_RAW_JD_WRITE_RE = re.compile(
    r"UPDATE\s+jobs\s+SET\s+jd_full\s*=",
    re.IGNORECASE,
)


def test_no_raw_jd_full_writes_outside_helper() -> None:
    """Fail if any file outside the helper / migrations issues a raw jd_full UPDATE."""
    offenders: list[str] = []

    for sub in ("web", "db"):
        search_root = _JOB_FINDER_ROOT / sub
        if not search_root.is_dir():
            continue
        for py in search_root.rglob("*.py"):
            # Skip byte-compiled cache dirs
            if "__pycache__" in py.parts:
                continue
            # Skip the sanctioned write path itself
            if py.name == "_jd_full.py":
                continue
            # Skip historical migration scripts (one-off data fixes)
            if "migrations" in py.parts:
                continue

            text = py.read_text(encoding="utf-8")
            for lineno, line in enumerate(text.splitlines(), start=1):
                if _RAW_JD_WRITE_RE.search(line):
                    rel = py.relative_to(_JOB_FINDER_ROOT.parent)
                    offenders.append(f"{rel}:{lineno}: {line.strip()}")

    assert not offenders, (
        "Raw UPDATE jobs SET jd_full = found outside _jd_full.py and migrations.\n"
        "Route the write through set_jd_full() (job_finder/db/_jd_full.py) instead:\n\n"
        + "\n".join(f"  {o}" for o in offenders)
    )
