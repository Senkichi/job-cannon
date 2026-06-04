"""CI grep gate: scoring columns are written only by the assessment writer.

Scoring-owned columns (sub_scores_json / classification / scoring_model /
scoring_provider) must be written exclusively by ``persist_job_assessment``
(today in ``job_finder/db/_persistence.py``; promoted to ``_assessment_writer.py``
in Phase 49.04) or a migration. A direct ``UPDATE jobs SET classification = ...``
anywhere else fragments the single-writer invariant and risks emitting an
LLM-attribution shape that violates the m078 D-17 triggers.

This is the structural defense; when Phase 49.04 extracts the writer, update the
allowlist filename below to point at ``_assessment_writer.py``.
"""

from __future__ import annotations

import re
from pathlib import Path

_JOB_FINDER_ROOT = Path(__file__).resolve().parents[1] / "job_finder"

# Files permitted to lead an `UPDATE jobs SET` with a scoring-owned column.
_ALLOWED_FILENAMES = {"_persistence.py"}

_SCORING_WRITE_RE = re.compile(
    r"UPDATE\s+jobs\s+SET\s+(sub_scores_json|classification|scoring_model|scoring_provider)\b",
    re.IGNORECASE,
)


def test_no_scoring_writes_outside_assessment_writer():
    offenders: list[str] = []
    for py in _JOB_FINDER_ROOT.rglob("*.py"):
        if "__pycache__" in py.parts:
            continue
        if py.name in _ALLOWED_FILENAMES or "migrations" in py.parts:
            continue
        if _SCORING_WRITE_RE.search(py.read_text(encoding="utf-8")):
            offenders.append(str(py.relative_to(_JOB_FINDER_ROOT)))
    assert not offenders, (
        "Direct UPDATE of a scoring-owned column (sub_scores_json/classification/"
        f"scoring_model/scoring_provider) found outside {_ALLOWED_FILENAMES} and "
        f"migrations/: {offenders}. Route the write through persist_job_assessment."
    )
