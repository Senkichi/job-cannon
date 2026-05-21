"""Unit tests for scripts/benchmark_sources.py.

Stage 0 of the no-key compensation plan. Pure-function unit tests; the live
HTTP calls and real DB reads are exercised only when the script is invoked
manually to produce the committed baseline report.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

# Make scripts/ importable as a top-level module.
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"


@pytest.fixture(autouse=True)
def _add_scripts_to_syspath(monkeypatch):
    monkeypatch.syspath_prepend(str(_SCRIPTS_DIR))


# ---------------------------------------------------------------------------
# load_existing_dedup_keys
# ---------------------------------------------------------------------------


def _make_jobs_table(db_path: str, rows: list[tuple[str, str, str]]) -> None:
    """Helper: create a minimal jobs table with the columns the benchmark reads.

    rows: list of (dedup_key, title, company).
    """
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE jobs ("
            "  dedup_key TEXT PRIMARY KEY,"
            "  title TEXT NOT NULL,"
            "  company TEXT NOT NULL"
            ")"
        )
        conn.executemany("INSERT INTO jobs (dedup_key, title, company) VALUES (?, ?, ?)", rows)
        conn.commit()
    finally:
        conn.close()


def test_load_existing_dedup_keys_reads_jobs_table(tmp_path):
    import benchmark_sources

    db_path = tmp_path / "jobs.db"
    _make_jobs_table(
        str(db_path),
        [
            ("acme|data scientist", "Data Scientist", "Acme"),
            ("globex|ml engineer", "ML Engineer", "Globex"),
        ],
    )

    keys = benchmark_sources.load_existing_dedup_keys(str(db_path))
    assert keys == {"acme|data scientist", "globex|ml engineer"}


def test_load_existing_dedup_keys_empty_db_returns_empty_set(tmp_path):
    import benchmark_sources

    db_path = tmp_path / "jobs.db"
    _make_jobs_table(str(db_path), [])

    assert benchmark_sources.load_existing_dedup_keys(str(db_path)) == set()


def test_load_existing_dedup_keys_missing_db_returns_empty_set(tmp_path):
    import benchmark_sources

    db_path = tmp_path / "does_not_exist.db"
    assert benchmark_sources.load_existing_dedup_keys(str(db_path)) == set()


# ---------------------------------------------------------------------------
# benchmark_one_source
# ---------------------------------------------------------------------------


def _make_job(title: str, company: str, location: str = "Remote"):
    """Build a Job that won't fail validation, with a deterministic source/url."""
    from job_finder.models import Job

    return Job(
        title=title,
        company=company,
        location=location,
        source="test_fixture",
        source_url=f"https://example.com/{company.lower()}/{title.lower().replace(' ', '-')}",
    )


def test_benchmark_one_source_happy_path():
    import benchmark_sources

    jobs = [
        _make_job("Data Scientist", "Acme"),
        _make_job("ML Engineer", "Globex"),
        _make_job("Senior Data Scientist", "Initech"),
    ]
    existing = {
        # Match Job.normalized_dedup_key output for Acme + Globex; Initech is novel.
        _make_job("Data Scientist", "Acme").dedup_key,
        _make_job("ML Engineer", "Globex").dedup_key,
    }

    result = benchmark_sources.benchmark_one_source(
        "fake_source",
        lambda: jobs,
        target_titles=["Data Scientist", "ML Engineer"],
        existing_keys=existing,
    )

    assert result.source == "fake_source"
    assert result.raw_count == 3
    assert result.parse_ok == 3
    assert result.novel_count == 1
    # 2 of 3 overlap → 66.7%
    assert result.overlap_pct == pytest.approx(66.7, abs=0.1)
    assert result.fetch_seconds >= 0.0
    assert result.notes == ""


def test_benchmark_one_source_fetch_error_is_captured_in_notes():
    import benchmark_sources

    def boom():
        raise RuntimeError("network down")

    result = benchmark_sources.benchmark_one_source(
        "broken_source",
        boom,
        target_titles=["Data Scientist"],
        existing_keys=set(),
    )

    assert result.raw_count == 0
    assert result.parse_ok == 0
    assert result.novel_count == 0
    assert result.overlap_pct == 0.0
    assert "network down" in result.notes
    assert "RuntimeError" in result.notes


def test_benchmark_one_source_empty_results():
    import benchmark_sources

    result = benchmark_sources.benchmark_one_source(
        "empty_source",
        lambda: [],
        target_titles=["Data Scientist"],
        existing_keys=set(),
    )

    assert result.raw_count == 0
    assert result.title_match_count == 0
    assert result.novel_count == 0
    # Division-by-zero guard: overlap is 0 when raw is 0.
    assert result.overlap_pct == 0.0
    assert result.notes == ""


def test_benchmark_one_source_title_filter_applied():
    import benchmark_sources

    jobs = [
        _make_job("Data Scientist", "Acme"),
        _make_job("Marketing Manager", "Globex"),
        _make_job("Backend Engineer", "Initech"),
    ]
    result = benchmark_sources.benchmark_one_source(
        "fake_source",
        lambda: jobs,
        target_titles=["Data Scientist"],
        existing_keys=set(),
    )

    assert result.raw_count == 3
    # Only one of the three matches "Data Scientist"
    assert result.title_match_count == 1


# ---------------------------------------------------------------------------
# format_markdown_report
# ---------------------------------------------------------------------------


def _make_result(name: str, raw: int = 5, novel: int = 2, notes: str = ""):
    import benchmark_sources

    return benchmark_sources.SourceResult(
        source=name,
        raw_count=raw,
        parse_ok=raw,
        title_match_count=raw,
        novel_count=novel,
        overlap_pct=0.0 if raw == 0 else round(100.0 * (raw - novel) / raw, 1),
        fetch_seconds=1.2,
        sample_titles=("Foo @ Acme", "Bar @ Globex"),
        notes=notes,
    )


def test_format_markdown_report_includes_all_columns():
    import benchmark_sources

    results = [_make_result("gmail"), _make_result("portal_remoteok", raw=0, novel=0)]
    md = benchmark_sources.format_markdown_report(
        results,
        target_titles=["Data Scientist"],
        existing_count=11330,
        no_paid=False,
    )

    # Header columns
    for col in (
        "source",
        "raw",
        "parse_ok",
        "title_match",
        "novel",
        "overlap_pct",
        "fetch_s",
        "notes",
    ):
        assert col in md, f"column {col!r} missing from report"

    # Per-source rows
    assert "| gmail |" in md
    assert "| portal_remoteok |" in md

    # Sample-titles section
    assert "## Sample titles" in md
    assert "### gmail" in md
    assert "Foo @ Acme" in md

    # Context
    assert "Data Scientist" in md
    assert "11330" in md


def test_format_markdown_report_no_paid_banner():
    import benchmark_sources

    md = benchmark_sources.format_markdown_report(
        [_make_result("gmail")],
        target_titles=["X"],
        existing_count=0,
        no_paid=True,
    )
    assert "no-paid" in md.lower()


# ---------------------------------------------------------------------------
# main() CLI
# ---------------------------------------------------------------------------


def _write_minimal_config(path, *, enable_serpapi: bool = False, target_titles=None) -> None:
    """Write a config.yaml with just enough structure for the benchmark.

    Mirrors the required-sections list in ``job_finder.config.validate_required_sections``
    so ``load_config`` accepts the fixture file.
    """
    import yaml

    cfg = {
        "profile": {
            "target_titles": ["Data Scientist"] if target_titles is None else target_titles,
            "target_locations": ["Remote"],
            "exclusions": {"title_keywords": [], "companies": []},
        },
        "sources": {
            "imap": {"enabled": False},
            "gmail": {"enabled": False},
            "serpapi": {"enabled": enable_serpapi, "api_key": "fake", "queries": []},
            "thordata": {"enabled": False},
            "dataforseo": {"enabled": False},
            "portal_search": {"enabled": False, "keywords": []},
        },
        "scoring": {"daily_budget_usd": 0},
        "db": {"path": "jobs.db"},
    }
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f)


def test_main_writes_to_output_path(tmp_path, monkeypatch):
    """End-to-end CLI invocation with all live sources disabled.

    The portal RemoteOK/Remotive/Himalayas fetchers are stubbed out to keep
    the test hermetic — those are normally always-on in main() because
    they're keyless, so we must inject a no-network stub.
    """
    import benchmark_sources

    cfg_path = tmp_path / "config.yaml"
    out_path = tmp_path / "report.md"
    db_path = tmp_path / "jobs.db"
    _make_jobs_table(str(db_path), [])
    _write_minimal_config(cfg_path)

    monkeypatch.setattr(benchmark_sources, "_PORTAL_FETCHERS", {})  # no live HTTP

    rc = benchmark_sources.main(
        [
            "--output",
            str(out_path),
            "--config",
            str(cfg_path),
            "--db",
            str(db_path),
        ]
    )

    assert rc == 0
    body = out_path.read_text(encoding="utf-8")
    assert "## Per-source counts" in body


def test_main_with_no_paid_skips_keyed_sources(tmp_path, monkeypatch):
    """--no-paid suppresses serpapi even when config has it enabled."""
    import benchmark_sources

    cfg_path = tmp_path / "config.yaml"
    out_path = tmp_path / "report.md"
    db_path = tmp_path / "jobs.db"
    _make_jobs_table(str(db_path), [])
    _write_minimal_config(cfg_path, enable_serpapi=True)

    monkeypatch.setattr(benchmark_sources, "_PORTAL_FETCHERS", {})
    # Sentinel that fails the test if --no-paid lets serpapi through
    monkeypatch.setattr(
        benchmark_sources,
        "_fetch_serpapi_for_benchmark",
        lambda *_a, **_kw: pytest.fail("serpapi should not be invoked under --no-paid"),
    )

    rc = benchmark_sources.main(
        [
            "--no-paid",
            "--output",
            str(out_path),
            "--config",
            str(cfg_path),
            "--db",
            str(db_path),
        ]
    )

    assert rc == 0
    body = out_path.read_text(encoding="utf-8")
    # Serpapi row should be absent from the table.
    assert "| serpapi |" not in body
    # Banner should advertise the no-paid simulation.
    assert "no-paid" in body.lower()


def test_main_missing_target_titles_returns_nonzero(tmp_path, monkeypatch):
    """Empty target_titles is a hard error — benchmark has nothing to measure."""
    import benchmark_sources

    # job_finder.config.validate_target_titles ALSO rejects an empty list (raises
    # ConfigError) — that's a perfectly acceptable failure mode, so the test
    # accepts either path: the benchmark's own guard or load_config's guard.
    # The contract is "rc != 0", regardless of which layer enforced it.
    cfg_path = tmp_path / "config.yaml"
    _write_minimal_config(cfg_path, target_titles=[])

    monkeypatch.setattr(benchmark_sources, "_PORTAL_FETCHERS", {})

    rc = benchmark_sources.main(["--config", str(cfg_path), "--db", str(tmp_path / "missing.db")])
    assert rc != 0
