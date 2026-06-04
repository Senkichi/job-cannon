"""CI grep gate: parser-owned columns are written only through upsert_job.

The parser-owned source columns (sources / source_urls / source_id) must be
written exclusively at the upsert_job boundary (``job_finder/db/_jobs.py``) or
in a migration. A direct ``UPDATE jobs SET source_urls = ...`` anywhere else is
a D-15 bypass — it skips the typed contract (and, post Phase 49, the URL
canonicalizer). This test is the structural defense that fails CI the moment a
new bypass appears (mirrors Phase 46.03's writer-routing gates).

Closed in Phase 47.09: ``enrichment_sources.merge_apply_urls`` (was a raw
``UPDATE jobs SET source_urls``) now routes through upsert_job.
"""

from __future__ import annotations

import re
from pathlib import Path

_JOB_FINDER_ROOT = Path(__file__).resolve().parents[1] / "job_finder"

# Match `UPDATE jobs SET <col>` where <col> is a parser-owned source column,
# tolerating arbitrary whitespace/newlines between tokens. \s spans newlines so
# multi-line UPDATE statements are caught too.
_SOURCE_WRITE_RE = re.compile(
    r"UPDATE\s+jobs\s+SET\s+(sources|source_urls|source_id)\b", re.IGNORECASE
)


def test_no_parser_owned_source_writes_outside_upsert():
    offenders: list[str] = []
    for sub in ("web", "db"):
        for py in (_JOB_FINDER_ROOT / sub).rglob("*.py"):
            if "__pycache__" in py.parts:
                continue
            if py.name == "_jobs.py" or "migrations" in py.parts:
                continue
            if _SOURCE_WRITE_RE.search(py.read_text(encoding="utf-8")):
                offenders.append(str(py.relative_to(_JOB_FINDER_ROOT)))
    assert not offenders, (
        "Direct UPDATE of a parser-owned source column (sources/source_urls/"
        f"source_id) found outside _jobs.py and migrations/: {offenders}. "
        "Route the write through upsert_job (D-15)."
    )
