"""CI grep gate: all canonical-location writes route through the funnel.

Mirrors ``tests/test_jd_full_writers_routed.py`` (the jd_full single-writer
gate) and the assessment-writer singleton gate. Scans ``job_finder/web/`` and
``job_finder/db/`` for raw ``UPDATE jobs SET ... <location column> = ...``
statements that touch any of the five canonical location columns:

    location, locations_raw, locations_structured, workplace_type,
    primary_country_code

The only sanctioned writers (D-5, issue #386):
  - ``job_finder/db/_locations.py``  — ``apply_location_observation`` funnel.
  - ``job_finder/db/_jobs.py``       — ``upsert_job`` INSERT/UPDATE branch.
  - ``job_finder/web/migrations/``   — historical / healing one-off data fixes.

A small set of pre-existing writers are explicitly exempted with a documented
reason — each is a NON-observation path (an admin correction, an N-way dedup
key-merge, or a one-time sentinel-gated backfill) that a later task in the
Data-Integrity cohort (#393) folds into the funnel. They are pinned here so the
exemption is auditable and any NEW raw location write outside them fails loud.

Uses pathlib + re (no rg dependency) so it passes in all CI environments.
"""

from __future__ import annotations

import re
from pathlib import Path

_JOB_FINDER_ROOT = Path(__file__).resolve().parents[1] / "job_finder"

# The five canonical location columns the funnel owns.
_LOCATION_COLUMNS = (
    "locations_raw",
    "locations_structured",
    "workplace_type",
    "primary_country_code",
    "location",  # plain `location` last so the alternation prefers the longer names
)

# Matches `UPDATE jobs SET ... <col> = ` spanning newlines (multi-column
# UPDATEs put location columns on later lines). re.DOTALL so `.` crosses lines.
_RAW_LOCATION_WRITE_RE = re.compile(
    r"UPDATE\s+jobs\s+SET\b.*?\b(?:" + "|".join(_LOCATION_COLUMNS) + r")\s*=",
    re.IGNORECASE | re.DOTALL,
)

# Sanctioned write paths — the funnel module + the upsert module itself.
_SANCTIONED_FILES = frozenset({"_locations.py", "_jobs.py"})

# Pre-existing NON-observation writers, exempted with a documented reason.
# Each is folded into the funnel by a later cohort task (#393); pinned here so
# the exemption stays auditable and shrinks as those tasks land.
_EXEMPT_RELATIVE = {
    # Admin review "approve" clears the per-location `unresolved` flag — an
    # operator correction, not an ingestion observation.
    "job_finder/web/blueprints/admin.py",
    # N-way dedup merge rewrites the whole canonical row (incl. dedup_key) when
    # collapsing duplicate rows — a key-rewrite merge, not an observation.
    "job_finder/web/dedup_normalizer.py",
    # One-time, sentinel-gated startup backfill (locations_raw = json_array(location)).
    "job_finder/web/startup_backfills.py",
}


def test_no_raw_location_writes_outside_funnel() -> None:
    """Fail if any NEW file issues a raw location-column UPDATE outside the funnel."""
    offenders: list[str] = []

    for sub in ("web", "db"):
        search_root = _JOB_FINDER_ROOT / sub
        if not search_root.is_dir():
            continue
        for py in search_root.rglob("*.py"):
            if "__pycache__" in py.parts:
                continue
            if py.name in _SANCTIONED_FILES:
                continue
            if "migrations" in py.parts:
                continue
            rel = py.relative_to(_JOB_FINDER_ROOT.parent).as_posix()
            if rel in _EXEMPT_RELATIVE:
                continue

            text = py.read_text(encoding="utf-8")
            for match in _RAW_LOCATION_WRITE_RE.finditer(text):
                lineno = text.count("\n", 0, match.start()) + 1
                offenders.append(f"{rel}:{lineno}")

    assert not offenders, (
        "Raw UPDATE jobs SET <location column> = found outside the sanctioned "
        "funnel. Route the write through apply_location_observation() "
        "(job_finder/db/_locations.py) instead:\n\n" + "\n".join(f"  {o}" for o in offenders)
    )
