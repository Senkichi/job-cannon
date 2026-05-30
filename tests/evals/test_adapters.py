"""Unit tests for cascade audit adapters (Phase 36)."""

import sqlite3

from evals.cascade_audit.adapters.ai_nav_discovery_adapter import AiNavDiscoveryAdapter
from evals.cascade_audit.adapters.company_research_adapter import CompanyResearchAdapter
from evals.cascade_audit.adapters.description_reformat_adapter import DescriptionReformatAdapter
from evals.cascade_audit.adapters.extract_jobs_adapter import ExtractJobsAdapter
from evals.cascade_audit.adapters.find_careers_url_adapter import FindCareersUrlAdapter
from evals.cascade_audit.adapters.parse_structured_fields_adapter import (
    ParseStructuredFieldsAdapter,
)


def test_parse_structured_fields_adapter_sample():
    """Test ParseStructuredFieldsAdapter.sample()."""
    adapter = ParseStructuredFieldsAdapter()
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE jobs (dedup_key TEXT, jd_full TEXT)")
    conn.execute("INSERT INTO jobs VALUES ('key1', ?)", ("Full job description. " * 25,))
    conn.commit()

    rows = adapter.sample(1, conn)
    assert len(rows) == 1
    assert "jd_full" in rows[0]


def test_parse_structured_fields_adapter_score():
    """Test ParseStructuredFieldsAdapter.score()."""
    adapter = ParseStructuredFieldsAdapter()
    gold = {"salary_min": 50000, "location": "SF"}
    candidate = {"salary_min": 52000, "location": "SF"}

    metrics = adapter.score(gold, candidate)
    assert metrics["schema_valid"] is True
    assert metrics["salary_mae"] == 2000
    assert metrics["location_match"] is True


def test_find_careers_url_adapter_sample():
    """Test FindCareersUrlAdapter.sample()."""
    adapter = FindCareersUrlAdapter()
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE companies (dedup_key TEXT, homepage_url TEXT)")
    conn.execute("INSERT INTO companies VALUES ('key1', 'https://example.com')")
    conn.commit()

    rows = adapter.sample(1, conn)
    assert len(rows) == 1
    assert "homepage_url" in rows[0]


def test_find_careers_url_adapter_score():
    """Test FindCareersUrlAdapter.score()."""
    adapter = FindCareersUrlAdapter()
    gold = {"url": "https://example.com/careers"}
    candidate = {"url": "https://example.com/jobs"}

    metrics = adapter.score(gold, candidate)
    assert "url_http_200" in metrics
    assert "same_etld1" in metrics
    assert "career_keyword_presence" in metrics


def test_extract_jobs_adapter_sample():
    """Test ExtractJobsAdapter.sample()."""
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmpdir:
        adapter = ExtractJobsAdapter(artifact_dir=Path(tmpdir))
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE companies (dedup_key TEXT, homepage_url TEXT)")
        conn.execute("INSERT INTO companies VALUES ('key1', 'https://example.com')")
        conn.commit()

        rows = adapter.sample(1, conn)
        assert len(rows) == 1
        assert "homepage_url" in rows[0]


def test_extract_jobs_adapter_score():
    """Test ExtractJobsAdapter.score()."""
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmpdir:
        adapter = ExtractJobsAdapter(artifact_dir=Path(tmpdir))
        gold = [{"title": "Engineer", "url": "https://example.com/job1"}]
        candidate = [{"title": "Engineer", "url": "https://example.com/job1"}]

        metrics = adapter.score(gold, candidate)
        assert metrics["schema_valid"] is True
        assert "title_set_jaccard" in metrics


def test_description_reformat_adapter_sample():
    """Test DescriptionReformatAdapter.sample()."""
    adapter = DescriptionReformatAdapter()
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE jobs (dedup_key TEXT, description TEXT)")
    conn.execute("INSERT INTO jobs VALUES ('key1', 'Job description text')")
    conn.commit()

    rows = adapter.sample(1, conn)
    assert len(rows) == 1
    assert "description" in rows[0]


def test_description_reformat_adapter_score():
    """Test DescriptionReformatAdapter.score()."""
    adapter = DescriptionReformatAdapter()
    gold = {"description": "Reformatted text"}
    candidate = {"description": "Reformatted text"}

    metrics = adapter.score(gold, candidate)
    assert "judge_winner" in metrics
    assert "judge_rationale" in metrics
    assert "judge_confidence" in metrics


def test_company_research_adapter_sample():
    """Test CompanyResearchAdapter.sample()."""
    adapter = CompanyResearchAdapter()
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE companies (dedup_key TEXT, name TEXT, domain TEXT)")
    conn.execute("INSERT INTO companies VALUES ('key1', 'Acme Corp', 'acme.com')")
    conn.commit()

    rows = adapter.sample(1, conn)
    assert len(rows) == 1
    assert "name" in rows[0]


def test_company_research_adapter_score():
    """Test CompanyResearchAdapter.score()."""
    adapter = CompanyResearchAdapter()
    gold = {"summary": "Company summary"}
    candidate = {"summary": "Company summary"}

    metrics = adapter.score(gold, candidate)
    assert "judge_winner" in metrics
    assert "judge_rationale" in metrics


def test_ai_nav_discovery_adapter_sample():
    """Test AiNavDiscoveryAdapter.sample()."""
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmpdir:
        adapter = AiNavDiscoveryAdapter(artifact_dir=Path(tmpdir))
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE companies (dedup_key TEXT, careers_nav_recipe TEXT)")
        conn.execute("INSERT INTO companies VALUES ('key1', '{\"steps\": []}')")
        conn.commit()

        rows = adapter.sample(1, conn)
        assert len(rows) == 1
        assert "careers_nav_recipe" in rows[0]


def test_ai_nav_discovery_adapter_score():
    """Test AiNavDiscoveryAdapter.score()."""
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmpdir:
        adapter = AiNavDiscoveryAdapter(artifact_dir=Path(tmpdir))
        gold = {"step_count": 5, "duration_ms": 1000}
        candidate = {"step_count": 6, "duration_ms": 1200}

        metrics = adapter.score(gold, candidate)
        assert metrics["step_count_delta"] == 1
        assert metrics["replay_duration_ratio"] == 1.2
