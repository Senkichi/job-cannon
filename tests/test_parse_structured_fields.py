"""Tests for parse_structured_fields() — Haiku extraction of salary/location
from a fully-fetched jd_full, post-cascade.

Replaces the salary-extraction side-effect of the deleted Haiku/Sonnet
synthesis tiers (Phase 2b sub-fix RC4). Schema deliberately excludes
jd_full so the model cannot summarize the job description.
"""

from unittest.mock import MagicMock

from job_finder.web.enrichment_tiers import parse_structured_fields


def test_extracts_salary_range_from_text(monkeypatch):
    """parse_structured_fields returns a dict shaped from the model response."""
    fake_call = MagicMock(
        return_value=MagicMock(
            data={"salary_min": 150000, "salary_max": 200000, "location": "Remote US"},
            schema_valid=True,
        )
    )
    monkeypatch.setattr("job_finder.web.enrichment_tiers.call_model", fake_call)
    out = parse_structured_fields(
        jd_full="...The salary range is $150,000 - $200,000..." + ("x" * 200),
        job_row={"dedup_key": "x|y", "title": "T", "company": "C"},
        conn=MagicMock(),
        config={},
    )
    assert out == {"salary_min": 150000, "salary_max": 200000, "location": "Remote US"}


def test_does_not_emit_jd_full_field(monkeypatch):
    """Schema MUST NOT include jd_full — the model cannot summarize the description."""
    from job_finder.web.enrichment_tiers import _STRUCTURED_FIELDS_SCHEMA

    assert "jd_full" not in _STRUCTURED_FIELDS_SCHEMA["properties"]


def test_returns_empty_dict_on_no_signal(monkeypatch):
    """When the model returns an empty data dict, the function returns {}."""
    fake_call = MagicMock(return_value=MagicMock(data={}, schema_valid=True))
    monkeypatch.setattr("job_finder.web.enrichment_tiers.call_model", fake_call)
    out = parse_structured_fields(
        jd_full="A description with no salary mentioned. " * 10,  # > 200 chars
        job_row={"dedup_key": "x|y", "title": "T", "company": "C"},
        conn=MagicMock(),
        config={},
    )
    assert out == {}


def test_runs_on_full_jd_not_truncated_fragments(monkeypatch):
    """Confirm we send the full jd_full, not a truncated 2000-char prefix."""
    captured = {}

    def fake_call(**kwargs):
        # Concatenate user message contents to verify the full text reached
        msg = kwargs["messages"][0]["content"]
        captured["msg_len"] = len(msg)
        return MagicMock(data={}, schema_valid=True)

    monkeypatch.setattr("job_finder.web.enrichment_tiers.call_model", fake_call)
    long_jd = "Lorem ipsum " * 800  # ~9600 chars
    parse_structured_fields(
        jd_full=long_jd,
        job_row={"dedup_key": "x|y", "title": "T", "company": "C"},
        conn=MagicMock(),
        config={},
    )
    # Allow some prompt overhead but message must include most of the JD
    assert captured["msg_len"] >= 8000


# ---------------------------------------------------------------------------
# Integration tests: enrich_job wires parse_structured_fields after fetch
# ---------------------------------------------------------------------------


def _make_job_row(**overrides):
    base = {
        "dedup_key": "acme|ds|remote",
        "title": "Data Scientist",
        "company": "Acme Corp",
        "location": None,
        "jd_full": None,
        "salary_min": None,
        "salary_max": None,
        "source_urls": '["https://example.com/job/123"]',
        "company_id": None,
        "enrichment_tier": None,
        "description": None,
    }
    base.update(overrides)
    return base


def test_enrich_job_invokes_parse_structured_fields_after_fetch(monkeypatch):
    """When fetch yields a JD and salary is missing, parse_structured_fields runs once."""
    from job_finder.web import data_enricher

    # Free tier returns a real (long) JD via direct URL fetch
    long_jd = "We are hiring a Data Scientist. " * 100  # ~3300 chars
    monkeypatch.setattr(data_enricher, "fetch_direct_jd", lambda url: long_jd)

    calls = []

    def fake_parse(jd_full, job_row, conn, config):
        calls.append({"jd_full": jd_full, "job_row": job_row})
        return {"salary_min": 150000, "salary_max": 200000, "location": "Remote US"}

    monkeypatch.setattr(data_enricher, "parse_structured_fields", fake_parse)

    row = _make_job_row()
    result = data_enricher.enrich_job(row, serpapi_key=None, conn=None, config={})

    assert len(calls) == 1, "parse_structured_fields must be invoked exactly once"
    assert calls[0]["jd_full"] == long_jd
    assert result.get("salary_min") == 150000
    assert result.get("salary_max") == 200000
    assert result.get("location") == "Remote US"
    assert result.get("jd_full") == long_jd


def test_enrich_job_does_not_overwrite_existing_salary(monkeypatch):
    """When salary is already on the row, parse_structured_fields must not overwrite."""
    from job_finder.web import data_enricher

    long_jd = "Senior role, fully remote. " * 100
    monkeypatch.setattr(data_enricher, "fetch_direct_jd", lambda url: long_jd)

    def fake_parse(jd_full, job_row, conn, config):
        # Even if Haiku claims a salary, existing values must win
        return {"salary_min": 999_999, "salary_max": 999_999, "location": "Mars"}

    monkeypatch.setattr(data_enricher, "parse_structured_fields", fake_parse)

    row = _make_job_row(
        salary_min=180_000,
        salary_max=220_000,
        location="San Francisco",
    )
    result = data_enricher.enrich_job(row, serpapi_key=None, conn=None, config={})

    # Existing values on the row stay — _persist semantics + UI both honor row values
    assert "salary_min" not in result
    assert "salary_max" not in result
    assert "location" not in result
    assert result.get("jd_full") == long_jd


def test_enrich_job_skips_parse_when_no_field_is_empty(monkeypatch):
    """When salary AND location are already populated, parse_structured_fields is not called."""
    from job_finder.web import data_enricher

    long_jd = "We are hiring. " * 100
    monkeypatch.setattr(data_enricher, "fetch_direct_jd", lambda url: long_jd)

    invoked = {"called": False}

    def fake_parse(jd_full, job_row, conn, config):
        invoked["called"] = True
        return {"salary_min": 1, "salary_max": 2, "location": "Z"}

    monkeypatch.setattr(data_enricher, "parse_structured_fields", fake_parse)

    row = _make_job_row(
        salary_min=150_000,
        salary_max=200_000,
        location="Remote US",
    )
    data_enricher.enrich_job(row, serpapi_key=None, conn=None, config={})

    assert invoked["called"] is False, "no missing fields => no parse_structured_fields call"


def test_enrich_job_fills_only_empty_fields(monkeypatch):
    """When salary_min is set but location is empty, only location is filled."""
    from job_finder.web import data_enricher

    long_jd = "Senior data scientist role. " * 100
    monkeypatch.setattr(data_enricher, "fetch_direct_jd", lambda url: long_jd)

    def fake_parse(jd_full, job_row, conn, config):
        return {"salary_min": 999_999, "location": "Remote US"}

    monkeypatch.setattr(data_enricher, "parse_structured_fields", fake_parse)

    row = _make_job_row(salary_min=180_000)  # salary_min set; location empty
    result = data_enricher.enrich_job(row, serpapi_key=None, conn=None, config={})

    # salary_min preserved (existing), location filled (was empty)
    assert "salary_min" not in result, "must not overwrite existing salary_min"
    assert result.get("location") == "Remote US"
    assert result.get("jd_full") == long_jd
