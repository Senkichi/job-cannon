"""Tests for the company-batched primary-source resolver (Phase 3).

Board fetches are intercepted by patching
``job_finder.web.ats_platforms._registry.run_platform_scan`` — the resolver
imports it at call time, and patching one level above the scanner keeps the
canonical-posting-dict fixtures from being reshaped by posting_to_job.
"""

from __future__ import annotations

import sqlite3
from unittest.mock import MagicMock, patch

from job_finder.web.db_migrate import run_migrations

_SCAN = "job_finder.web.ats_platforms._registry.run_platform_scan"


def _migrated_db(tmp_path) -> sqlite3.Connection:
    db_path = tmp_path / "jobs.db"
    run_migrations(str(db_path))
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _insert_company(conn, cid, name, *, platform="lever", slug=None, status="hit"):
    conn.execute(
        "INSERT INTO companies "
        "(id, name, name_raw, ats_platform, ats_slug, ats_probe_status, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, "
        "'2026-01-01T00:00:00', '2026-01-01T00:00:00')",
        (cid, name, name, platform, slug if slug is not None else name.lower(), status),
    )


def _insert_job(conn, dedup_key, title, company, *, company_id=None, **cols):
    fields = {
        "dedup_key": dedup_key,
        "title": title,
        "company": company,
        "location": "Remote",
        "first_seen": "2026-01-01",
        "last_seen": "2026-01-01",
        "source_urls": '["https://www.linkedin.com/jobs/view/1"]',
        "company_id": company_id,
        **cols,
    }
    names = ", ".join(fields)
    marks = ", ".join("?" for _ in fields)
    conn.execute(f"INSERT INTO jobs ({names}) VALUES ({marks})", list(fields.values()))


def _resolve(conn, config=None, **kwargs):
    from job_finder.web.primary_source_resolver import resolve_primary_sources

    kwargs.setdefault("delay_range", (0.0, 0.0))
    if config is None:
        # The Phase 4 LLM tie-breaker defaults ON in production; these tests
        # exercise the heuristic path, so keep model calls out of them
        # (tie-break behavior is covered in test_primary_source_tiebreak.py).
        config = {"direct_link": {"resolver": {"llm_tiebreak": False}}}
    return resolve_primary_sources(conn, config, **kwargs)


def _posting(title, url, **extra):
    return {"title": title, "source_url": url, "description": "x" * 300, **extra}


# ── candidate selection ───────────────────────────────────────────────────────


def test_one_board_fetch_per_company(tmp_path):
    """N jobs at one company must trigger exactly one board fetch."""
    conn = _migrated_db(tmp_path)
    _insert_company(conn, 1, "Acme")
    for i in range(3):
        _insert_job(conn, f"j{i}", f"Engineer {i}", "Acme", company_id=1)
    conn.commit()

    with patch(_SCAN, return_value=[]) as scan:
        stats = _resolve(conn)

    assert scan.call_count == 1
    assert stats["companies_scanned"] == 1
    assert stats["jobs_checked"] == 3
    conn.close()


def test_probe_status_gating_excludes_pending_and_miss(tmp_path):
    """Only ats_probe_status='hit' companies are consulted (P2)."""
    conn = _migrated_db(tmp_path)
    _insert_company(conn, 1, "Hit", status="hit")
    _insert_company(conn, 2, "Pending", status="pending")
    _insert_company(conn, 3, "Miss", status="miss")
    for cid, key in ((1, "a"), (2, "b"), (3, "c")):
        _insert_job(conn, key, "Data Scientist", f"C{cid}", company_id=cid)
    conn.commit()

    with patch(_SCAN, return_value=[]) as scan:
        stats = _resolve(conn)

    assert scan.call_count == 1  # only the hit company
    assert stats["jobs_checked"] == 1
    # Non-hit companies' jobs burned no attempts.
    row = conn.execute(
        "SELECT direct_url_attempts, direct_url_checked_at FROM jobs WHERE dedup_key='b'"
    ).fetchone()
    assert (row["direct_url_attempts"] or 0) == 0
    assert row["direct_url_checked_at"] is None
    conn.close()


def test_attempt_exhaustion_and_decay_reeligibility(tmp_path):
    """Rows at max_attempts are skipped — until checked_at ages past the
    decay window, then they re-enter candidacy (P7 without hooks)."""
    conn = _migrated_db(tmp_path)
    _insert_company(conn, 1, "Acme")
    _insert_job(
        conn,
        "exhausted-fresh",
        "Engineer",
        "Acme",
        company_id=1,
        direct_url_attempts=3,
        direct_url_checked_at="2099-01-01T00:00:00",
    )
    _insert_job(
        conn,
        "exhausted-stale",
        "Engineer",
        "Acme",
        company_id=1,
        direct_url_attempts=3,
        direct_url_checked_at="2020-01-01T00:00:00",
    )
    conn.commit()

    with patch(_SCAN, return_value=[]):
        stats = _resolve(conn)

    assert stats["jobs_checked"] == 1
    row = conn.execute(
        "SELECT direct_url_attempts FROM jobs WHERE dedup_key='exhausted-stale'"
    ).fetchone()
    assert row["direct_url_attempts"] == 4
    row = conn.execute(
        "SELECT direct_url_attempts FROM jobs WHERE dedup_key='exhausted-fresh'"
    ).fetchone()
    assert row["direct_url_attempts"] == 3
    conn.close()


def test_expired_and_closed_rows_excluded(tmp_path):
    conn = _migrated_db(tmp_path)
    _insert_company(conn, 1, "Acme")
    _insert_job(conn, "live", "Engineer", "Acme", company_id=1)
    _insert_job(conn, "expired", "Engineer", "Acme", company_id=1, expiry_status="expired")
    _insert_job(conn, "archived", "Engineer", "Acme", company_id=1, pipeline_status="archived")
    _insert_job(conn, "rejected", "Engineer", "Acme", company_id=1, pipeline_status="rejected")
    conn.commit()

    with patch(_SCAN, return_value=[]):
        stats = _resolve(conn)

    assert stats["jobs_checked"] == 1
    conn.close()


def test_unsupported_platform_skipped_without_attempt_burn(tmp_path):
    """jobvite (no public API) and unknown platforms burn no attempts."""
    conn = _migrated_db(tmp_path)
    _insert_company(conn, 1, "JV", platform="jobvite")
    _insert_company(conn, 2, "Mystery", platform="icims")
    _insert_job(conn, "a", "Engineer", "JV", company_id=1)
    _insert_job(conn, "b", "Engineer", "Mystery", company_id=2)
    conn.commit()

    with patch(_SCAN, return_value=[]) as scan:
        stats = _resolve(conn)

    assert scan.call_count == 0
    assert stats["companies_skipped"] == 2
    assert stats["jobs_checked"] == 0
    rows = conn.execute("SELECT direct_url_attempts FROM jobs").fetchall()
    assert all((r["direct_url_attempts"] or 0) == 0 for r in rows)
    conn.close()


def test_max_companies_cap(tmp_path):
    conn = _migrated_db(tmp_path)
    for cid, name in ((1, "Aaa"), (2, "Bbb")):
        _insert_company(conn, cid, name)
        _insert_job(conn, f"j{cid}", "Engineer", name, company_id=cid)
    conn.commit()

    with patch(_SCAN, return_value=[]) as scan:
        stats = _resolve(conn, max_companies=1)

    assert scan.call_count == 1
    assert stats["companies_scanned"] == 1
    conn.close()


# ── resolution + merge behavior ───────────────────────────────────────────────


def test_strict_match_resolves_and_merges(tmp_path):
    conn = _migrated_db(tmp_path)
    _insert_company(conn, 1, "Acme")
    _insert_job(conn, "j1", "Senior Data Scientist", "Acme", company_id=1)
    conn.commit()

    postings = [
        _posting(
            "Senior Data Scientist",
            "https://jobs.lever.co/acme/1",
            salary_min=150000,
            salary_max=190000,
        ),
        _posting("Staff Engineer", "https://jobs.lever.co/acme/2"),
    ]
    with patch(_SCAN, return_value=postings):
        stats = _resolve(conn)

    assert stats["resolved"] == 1
    assert stats["strict"] == 1
    assert stats["merged"] == 1
    row = conn.execute(
        "SELECT direct_url, direct_url_confidence, salary_min, direct_url_attempts "
        "FROM jobs WHERE dedup_key='j1'"
    ).fetchone()
    assert row["direct_url"] == "https://jobs.lever.co/acme/1"
    assert row["direct_url_confidence"] == "strict"
    assert row["salary_min"] == 150000
    assert row["direct_url_attempts"] == 1
    conn.close()


def test_ambiguous_match_links_loose_and_merges_nothing(tmp_path):
    """Contamination invariant survives the batched path: an ambiguous title
    match yields a loose link and zero data merge."""
    conn = _migrated_db(tmp_path)
    _insert_company(conn, 1, "Acme")
    _insert_job(conn, "j1", "Senior Data Scientist", "Acme", company_id=1)
    conn.commit()

    postings = [
        _posting("Senior Data Scientist", "https://jobs.lever.co/acme/1", salary_min=100000),
        _posting("Senior Data Scientist", "https://jobs.lever.co/acme/2", salary_min=90000),
    ]
    with patch(_SCAN, return_value=postings):
        stats = _resolve(conn)

    assert stats["loose"] == 1
    assert stats["merged"] == 0
    row = conn.execute(
        "SELECT direct_url_confidence, salary_min FROM jobs WHERE dedup_key='j1'"
    ).fetchone()
    assert row["direct_url_confidence"] == "loose"
    assert row["salary_min"] is None
    conn.close()


def test_promotion_runs_without_company_linkage(tmp_path):
    """A job whose source_urls already hold an ATS link promotes for free,
    even with no company row (legacy backfill contract)."""
    conn = _migrated_db(tmp_path)
    _insert_job(
        conn,
        "j1",
        "Engineer",
        "Acme",
        source_urls='["https://jobs.lever.co/acme/1"]',
    )
    conn.commit()

    stats = _resolve(conn)

    assert stats["promoted"] == 1
    assert stats["strict"] == 1
    row = conn.execute("SELECT direct_url FROM jobs WHERE dedup_key='j1'").fetchone()
    assert row["direct_url"] == "https://jobs.lever.co/acme/1"
    conn.close()


def test_second_run_is_noop_after_resolution(tmp_path):
    """Idempotency: resolved rows leave the candidate pool."""
    conn = _migrated_db(tmp_path)
    _insert_company(conn, 1, "Acme")
    _insert_job(conn, "j1", "Senior Data Scientist", "Acme", company_id=1)
    conn.commit()

    postings = [_posting("Senior Data Scientist", "https://jobs.lever.co/acme/1")]
    with patch(_SCAN, return_value=postings) as scan:
        first = _resolve(conn)
        second = _resolve(conn)

    assert first["resolved"] == 1
    assert second["resolved"] == 0
    assert second["jobs_checked"] == 0
    assert scan.call_count == 1  # second run fetched no boards
    conn.close()


# ── scheduler + entry-point wiring ───────────────────────────────────────────


def test_run_primary_source_resolution_opens_own_connection(tmp_path):
    from job_finder.web.primary_source_resolver import run_primary_source_resolution

    db_path = tmp_path / "jobs.db"
    run_migrations(str(db_path))
    result = run_primary_source_resolution(str(db_path), {})
    assert result["scanned"] == 0


def test_scheduler_registers_primary_source_resolution(app):
    """register_all_jobs wires the 5:45 AM job with the expected id."""
    from job_finder.web.scheduler._jobs import register_all_jobs

    scheduler = MagicMock()
    register_all_jobs(scheduler, app)
    job_ids = [call.kwargs.get("id") for call in scheduler.add_job.call_args_list]
    assert "primary_source_resolution" in job_ids

    idx = job_ids.index("primary_source_resolution")
    trigger = str(scheduler.add_job.call_args_list[idx].kwargs["trigger"])
    assert "hour='5'" in trigger and "minute='45'" in trigger


def test_resolver_disabled_guard_skips_run(app):
    """direct_link.resolver.enabled=false short-circuits the scheduled job
    before the resolver module is even invoked."""
    from job_finder.web.scheduler._jobs import register_primary_source_resolution

    captured = {}

    class FakeScheduler:
        def add_job(self, func, **kwargs):
            captured["func"] = func

    register_primary_source_resolution(FakeScheduler(), app)

    app.config["JF_CONFIG"] = {
        **app.config.get("JF_CONFIG", {}),
        "direct_link": {"resolver": {"enabled": False}},
    }
    with patch("job_finder.web.primary_source_resolver.run_primary_source_resolution") as run:
        captured["func"]()
    assert run.call_count == 0


def test_backfill_delegates_to_resolver(tmp_path):
    """The legacy backfill entry point routes through the resolver and keeps
    its summary keys."""
    conn = _migrated_db(tmp_path)
    _insert_job(
        conn,
        "a",
        "DS",
        "Acme",
        source_urls='["https://jobs.lever.co/acme/1"]',
    )
    _insert_job(conn, "b", "DS", "Beta")
    conn.commit()

    from job_finder.web.backfill_direct_links import backfill_direct_links

    summary = backfill_direct_links(conn, {})
    assert summary["resolved"] == 1
    assert summary["strict"] == 1
    assert {"scanned", "resolved", "strict", "loose"} <= set(summary)
    conn.close()
