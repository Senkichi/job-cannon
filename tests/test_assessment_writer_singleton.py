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
# Phase 49.04 promoted the writer to _assessment_writer.py (the sole sanctioned
# writer); _persistence.py now only re-exports it.
_ALLOWED_FILENAMES = {"_assessment_writer.py"}

# Match a scoring-owned column ANYWHERE in an `UPDATE jobs SET ...` clause, not
# just as the first column after SET. The earlier anchored form
# (`SET\s+(<col>)`) was evaded by any multi-column UPDATE that led with a
# different column (e.g. the dedup re-key UPDATE led with `dedup_key = ?` and
# slipped `classification = ?` onto a later line). re.DOTALL lets `.` cross
# newlines; the {0,600} bound keeps the match inside one statement's SET clause
# (real SET clauses are < 500 chars) so it can't reach across to unrelated code.
# The leading `\b` before the column alternation avoids matching suffixes like
# `gold_classification`. Mirrors tests/test_location_writers_routed.py.
_SCORING_WRITE_RE = re.compile(
    r"UPDATE\s+jobs\s+SET\b.{0,600}?\b(sub_scores_json|classification|scoring_model|scoring_provider)\s*=",
    re.IGNORECASE | re.DOTALL,
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
