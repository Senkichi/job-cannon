"""Commit D smoke tests: country + workplace_type dropdowns + filter routing.

Covers:
  - get_distinct_country_codes / get_distinct_workplace_types return the
    populated values and skip NULLs.
  - get_filtered_jobs respects the new country + workplace_type kwargs,
    including the alpha-2 sanity check and the four-value enum allowlist.
  - /jobs route renders both dropdowns with the populated values.
  - format_canonical_location Jinja filter renders the canonical pill text.

These are smoke tests — they confirm wiring + SQL shape. Visual rendering
(pill styling, hover tooltip animation) is browser-only territory per
CLAUDE.md "Verification Standards".
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from job_finder.db import (
    get_distinct_country_codes,
    get_distinct_workplace_types,
    get_filtered_jobs,
)


def _seed_job(
    conn: sqlite3.Connection,
    *,
    dedup_key: str,
    title: str = "Senior Engineer",
    company: str = "Acme",
    location: str = "San Francisco, CA",
    locations_structured: str | None = None,
    workplace_type: str | None = None,
    primary_country_code: str | None = None,
    pipeline_status: str = "discovered",
) -> None:
    """Insert one job row with the m066 columns populated as given."""
    conn.execute(
        """INSERT INTO jobs (
            dedup_key, title, company, location, locations_raw,
            locations_structured, workplace_type, primary_country_code,
            sources, source_urls, pipeline_status, first_seen, last_seen
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, '[]', '[]', ?, '2026-05-27', '2026-05-27')""",
        (
            dedup_key,
            title,
            company,
            location,
            json.dumps([location]),
            locations_structured,
            workplace_type,
            primary_country_code,
            pipeline_status,
        ),
    )
    conn.commit()


# ─── get_distinct_country_codes ──────────────────────────────────────


def test_get_distinct_country_codes_returns_populated_only(app) -> None:
    """NULL country codes are excluded; populated values returned sorted."""
    db_path = app.config["DB_PATH"]
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    _seed_job(conn, dedup_key="us1", primary_country_code="US")
    _seed_job(conn, dedup_key="us2", primary_country_code="US")
    _seed_job(conn, dedup_key="gb1", primary_country_code="GB")
    _seed_job(conn, dedup_key="null1", primary_country_code=None)
    countries = get_distinct_country_codes(conn)
    conn.close()
    assert countries == ["GB", "US"]


def test_get_distinct_country_codes_empty_when_no_data(app) -> None:
    """Empty DB / all-NULL column → empty list (no crash)."""
    db_path = app.config["DB_PATH"]
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    countries = get_distinct_country_codes(conn)
    conn.close()
    assert countries == []


# ─── get_distinct_workplace_types ────────────────────────────────────


def test_get_distinct_workplace_types_returns_populated_only(app) -> None:
    """Same NULL-skip + sort behavior as country_codes."""
    db_path = app.config["DB_PATH"]
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    _seed_job(conn, dedup_key="r1", workplace_type="REMOTE")
    _seed_job(conn, dedup_key="r2", workplace_type="REMOTE")
    _seed_job(conn, dedup_key="h1", workplace_type="HYBRID")
    _seed_job(conn, dedup_key="o1", workplace_type="ONSITE")
    _seed_job(conn, dedup_key="null1", workplace_type=None)
    types = get_distinct_workplace_types(conn)
    conn.close()
    assert types == ["HYBRID", "ONSITE", "REMOTE"]


# ─── get_filtered_jobs (country + workplace_type kwargs) ──────────────


def test_get_filtered_jobs_country_filter(app) -> None:
    """`country='US'` returns only US rows."""
    db_path = app.config["DB_PATH"]
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    _seed_job(conn, dedup_key="us1", primary_country_code="US")
    _seed_job(conn, dedup_key="gb1", primary_country_code="GB")
    rows = get_filtered_jobs(conn, country="US")
    conn.close()
    keys = {r["dedup_key"] for r in rows}
    assert keys == {"us1"}


def test_get_filtered_jobs_country_lowercased_normalizes_to_upper(app) -> None:
    """Lower-case 2-letter input is normalized to upper before query."""
    db_path = app.config["DB_PATH"]
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    _seed_job(conn, dedup_key="us1", primary_country_code="US")
    rows = get_filtered_jobs(conn, country="us")
    conn.close()
    assert {r["dedup_key"] for r in rows} == {"us1"}


@pytest.mark.parametrize("bad_input", ["USA", "U", "1S", "United States"])
def test_get_filtered_jobs_country_garbage_is_ignored(app, bad_input) -> None:
    """Non-alpha-2 input bypasses the filter (returns all rows, not zero).

    The sanity check is defensive — malformed query strings get ignored
    rather than producing a SQL error or unexpected zero-result page.
    """
    db_path = app.config["DB_PATH"]
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    _seed_job(conn, dedup_key="us1", primary_country_code="US")
    rows = get_filtered_jobs(conn, country=bad_input)
    conn.close()
    assert len(rows) == 1


def test_get_filtered_jobs_workplace_type_filter(app) -> None:
    """`workplace_type='REMOTE'` returns only REMOTE rows."""
    db_path = app.config["DB_PATH"]
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    _seed_job(conn, dedup_key="r1", workplace_type="REMOTE")
    _seed_job(conn, dedup_key="h1", workplace_type="HYBRID")
    rows = get_filtered_jobs(conn, workplace_type="REMOTE")
    conn.close()
    assert {r["dedup_key"] for r in rows} == {"r1"}


def test_get_filtered_jobs_workplace_type_invalid_is_ignored(app) -> None:
    """Values outside the four-enum are ignored (not a SQL error)."""
    db_path = app.config["DB_PATH"]
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    _seed_job(conn, dedup_key="r1", workplace_type="REMOTE")
    rows = get_filtered_jobs(conn, workplace_type="bogus")
    conn.close()
    assert len(rows) == 1


def test_get_filtered_jobs_country_and_workplace_type_combine(app) -> None:
    """AND-combined: both filters narrow the result set."""
    db_path = app.config["DB_PATH"]
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    _seed_job(conn, dedup_key="us_remote", primary_country_code="US", workplace_type="REMOTE")
    _seed_job(conn, dedup_key="us_hybrid", primary_country_code="US", workplace_type="HYBRID")
    _seed_job(conn, dedup_key="gb_remote", primary_country_code="GB", workplace_type="REMOTE")
    rows = get_filtered_jobs(conn, country="US", workplace_type="REMOTE")
    conn.close()
    assert {r["dedup_key"] for r in rows} == {"us_remote"}


# ─── /jobs route renders the dropdowns ───────────────────────────────


def test_jobs_index_renders_country_dropdown_with_options(app, client) -> None:
    """The country `<select>` is present and lists distinct populated values."""
    db_path = app.config["DB_PATH"]
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    _seed_job(conn, dedup_key="us1", primary_country_code="US")
    _seed_job(conn, dedup_key="gb1", primary_country_code="GB")
    conn.close()
    resp = client.get("/jobs")
    assert resp.status_code == 200
    body = resp.data.decode("utf-8")
    assert 'name="country"' in body
    assert 'id="filter-country"' in body
    assert ">US</option>" in body
    assert ">GB</option>" in body


def test_jobs_index_renders_workplace_type_dropdown_with_options(app, client) -> None:
    """The workplace_type `<select>` lists the distinct populated values."""
    db_path = app.config["DB_PATH"]
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    _seed_job(conn, dedup_key="r1", workplace_type="REMOTE")
    _seed_job(conn, dedup_key="h1", workplace_type="HYBRID")
    conn.close()
    resp = client.get("/jobs")
    assert resp.status_code == 200
    body = resp.data.decode("utf-8")
    assert 'name="workplace_type"' in body
    assert 'id="filter-workplace-type"' in body
    # Templates title-case the workplace enum for display.
    assert ">Remote</option>" in body or ">REMOTE</option>" in body
    assert ">Hybrid</option>" in body or ">HYBRID</option>" in body


def test_jobs_index_country_filter_narrows_results(app, client) -> None:
    """`?country=US` query param actually narrows the job list."""
    db_path = app.config["DB_PATH"]
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    _seed_job(conn, dedup_key="us_only", title="US Job", primary_country_code="US")
    _seed_job(conn, dedup_key="gb_only", title="UK Job", primary_country_code="GB")
    conn.close()
    resp = client.get("/jobs?country=US")
    assert resp.status_code == 200
    body = resp.data.decode("utf-8")
    assert "US Job" in body
    assert "UK Job" not in body


# ─── format_canonical_location Jinja filter ──────────────────────────


def test_format_canonical_location_filter_renders_full_entry(app) -> None:
    """Filter renders ``City, Region · Country · Workplace``."""
    with app.app_context():
        env = app.jinja_env
        template = env.from_string(
            "{{ value | format_canonical_location }}"
        )
        json_str = json.dumps([{
            "city": "San Francisco",
            "region": "California",
            "region_code": "CA",
            "country": "United States",
            "country_code": "US",
            "workplace_type": "REMOTE",
            "raw": "San Francisco, CA",
            "unresolved": False,
        }])
        result = template.render(value=json_str)
        assert result == "San Francisco, CA · US · Remote"


def test_format_canonical_location_filter_omits_unspecified_workplace(app) -> None:
    """UNSPECIFIED workplace_type is omitted from the rendered string."""
    with app.app_context():
        env = app.jinja_env
        template = env.from_string(
            "{{ value | format_canonical_location }}"
        )
        json_str = json.dumps([{
            "city": "Toronto",
            "region_code": "ON",
            "country_code": "CA",
            "workplace_type": "UNSPECIFIED",
            "raw": "Toronto, ON",
            "unresolved": False,
        }])
        result = template.render(value=json_str)
        assert result == "Toronto, ON · CA"


def test_format_canonical_location_filter_caps_at_max_entries(app) -> None:
    """Overflow appears as ``+N more``; default max is 3 entries."""
    with app.app_context():
        env = app.jinja_env
        template = env.from_string(
            "{{ value | format_canonical_location }}"
        )
        entries = [
            {
                "city": f"City{i}",
                "country_code": "US",
                "workplace_type": "UNSPECIFIED",
                "raw": f"City{i}",
                "unresolved": False,
            }
            for i in range(5)
        ]
        result = template.render(value=json.dumps(entries))
        # First 3 entries rendered + overflow tail.
        assert "City0 · US" in result
        assert "City1 · US" in result
        assert "City2 · US" in result
        assert "+2 more" in result


def test_format_canonical_location_filter_empty_input_returns_empty(app) -> None:
    """None / empty string / invalid JSON → empty string."""
    with app.app_context():
        env = app.jinja_env
        template = env.from_string(
            "{{ value | format_canonical_location }}"
        )
        assert template.render(value=None) == ""
        assert template.render(value="") == ""
        assert template.render(value="not-json") == ""
        assert template.render(value="[]") == ""
