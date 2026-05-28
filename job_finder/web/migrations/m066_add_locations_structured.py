"""Migration 66 — add canonical location columns to `jobs`.

Lands the schema half of the location-parsing SPEC
(`.planning/SPEC-location-parsing.md`). Commit A shipped the parser
(`job_finder/web/location_parser.py` + `location_canonical.py`); this
migration adds the three nullable columns the parser's output is destined
for. No call site reaches the new columns yet — Commit C wires
`upsert_job` to write them and the Layer-1 scanners to bypass the parser
with structured data.

After this migration:

  - `locations_structured` (TEXT) — JSON-serialized `list[JobLocation]`,
    the one source of truth for everything structured. Read via
    `JobLocation.from_json` on demand; write via `to_json`.
  - `workplace_type` (TEXT) — denormalized convenience column equal to
    `locations_structured[0].workplace_type` when present. Lets the
    existing job-list filter use a direct WHERE without JSON parsing.
  - `primary_country_code` (TEXT) — denormalized convenience column equal
    to `locations_structured[0].country_code` when present. Enables the
    upcoming country-filter dropdown without per-row JSON parsing.

All three are NULL on existing rows. `location` / `locations_raw` are
untouched and remain the display/filter strings every blueprint, template,
and rescue path already reads — m066 is purely additive and zero-risk for
existing reads. A separate opt-in backfill (Commit E / m067) will re-parse
`locations_raw` for legacy rows once Commit C has been live for at least
one ingestion cycle and the parser is trusted on fresh data first.
"""

from __future__ import annotations

from job_finder.web.migrations.types import Migration

MIGRATION = Migration(
    version=66,
    description="add locations_structured/workplace_type/primary_country_code to jobs",
    sql=[
        "ALTER TABLE jobs ADD COLUMN locations_structured TEXT",
        "ALTER TABLE jobs ADD COLUMN workplace_type TEXT",
        "ALTER TABLE jobs ADD COLUMN primary_country_code TEXT",
    ],
)
