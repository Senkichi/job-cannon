"""Schema-correspondence test — Pattern A defense (Phase 47.05).

Guards the field-to-column contract so the next ``posted_date``-shaped drift
(set-on-dataclass, lost-in-persistence) fails CI instead of shipping silently:

  1. Every column in the live ``jobs`` schema is categorized in
     ``COLUMN_CATEGORIES``. A new uncategorized column fails here.
  2. Every ``"parser"``-categorized column has a matching ``ParsedJob`` field.
  3. Every ``ParsedJob`` field maps to a ``"parser"`` column, except the
     documented non-parser transport fields (dedup_key / scoring_provider /
     unresolved_reasons).

The live schema is read via ``PRAGMA table_xinfo(jobs)`` (not ``table_info``):
``table_xinfo`` reports hidden / generated columns (e.g. the Phase 49 VIRTUAL
``computed_status``), so they can't silently escape categorization.
"""

from __future__ import annotations

import dataclasses
import os
import sqlite3
import tempfile

import pytest

from job_finder.db.column_categories import (
    COLUMN_CATEGORIES,
    NON_PARSER_PARSEDJOB_FIELDS,
)
from job_finder.parsed_job import ParsedJob


@pytest.fixture(scope="module")
def live_columns() -> set[str]:
    """Column names in the fully-migrated ``jobs`` table (via table_xinfo)."""
    from job_finder.web.db_migrate import run_migrations

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        run_migrations(path)
        conn = sqlite3.connect(path)
        try:
            # table_xinfo columns: (cid, name, type, notnull, dflt, pk, hidden)
            return {r[1] for r in conn.execute("PRAGMA table_xinfo(jobs)").fetchall()}
        finally:
            conn.close()
    finally:
        if os.path.exists(path):
            os.remove(path)


def _parsed_job_fields() -> set[str]:
    return {f.name for f in dataclasses.fields(ParsedJob) if not f.name.startswith("_")}


def _parser_columns() -> set[str]:
    return {col for col, cat in COLUMN_CATEGORIES.items() if cat == "parser"}


# ---------------------------------------------------------------------------
# Assertion 1 — every live column is categorized
# ---------------------------------------------------------------------------


def test_every_live_column_is_categorized(live_columns):
    uncategorized = live_columns - set(COLUMN_CATEGORIES)
    assert not uncategorized, (
        f"jobs columns missing from COLUMN_CATEGORIES: {sorted(uncategorized)}. "
        "Categorize each new column in job_finder/db/column_categories.py "
        "(and add a matching ParsedJob field if it is parser-owned)."
    )


# ---------------------------------------------------------------------------
# Assertion 2 — every parser column has a ParsedJob field
# ---------------------------------------------------------------------------


def test_every_parser_column_has_parsed_job_field():
    missing = _parser_columns() - _parsed_job_fields()
    assert not missing, (
        f"parser-categorized columns with no matching ParsedJob field: {sorted(missing)}. "
        "Add the field to ParsedJob (and UnresolvedParsedJob) or recategorize the column."
    )


# ---------------------------------------------------------------------------
# Assertion 3 — every ParsedJob field maps to a parser column (modulo exemptions)
# ---------------------------------------------------------------------------


def test_every_parsed_job_field_is_a_parser_column():
    stray = _parsed_job_fields() - _parser_columns() - NON_PARSER_PARSEDJOB_FIELDS
    assert not stray, (
        f"ParsedJob fields without a parser-categorized column: {sorted(stray)}. "
        "Either categorize the column as 'parser' or add the field to "
        "NON_PARSER_PARSEDJOB_FIELDS with a rationale."
    )


# ---------------------------------------------------------------------------
# Hygiene — the exemption set stays honest
# ---------------------------------------------------------------------------


def test_exempt_fields_are_real_parsed_job_fields():
    """Every exempt name is an actual ParsedJob field (no stale exemptions)."""
    stale = NON_PARSER_PARSEDJOB_FIELDS - _parsed_job_fields()
    assert not stale, (
        f"NON_PARSER_PARSEDJOB_FIELDS references non-existent fields: {sorted(stale)}"
    )


def test_exempt_fields_are_categorized_non_parser():
    """Exempt fields must still be known columns, just not parser-owned."""
    for name in NON_PARSER_PARSEDJOB_FIELDS:
        assert name in COLUMN_CATEGORIES, f"exempt field {name!r} is not categorized at all"
        assert COLUMN_CATEGORIES[name] != "parser", (
            f"exempt field {name!r} is categorized 'parser' — remove it from the exemption set"
        )
