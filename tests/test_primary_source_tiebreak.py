"""Tests for the quick-tier LLM tie-breaker (Phase 4).

call_model is patched at job_finder.web.model_provider.call_model — the
tie-break module imports it at call time (same pattern as agentic_enricher).
Board fetches are patched one level above the scanners, as in
test_primary_source_resolver.py.
"""

from __future__ import annotations

import json
import sqlite3
from unittest.mock import MagicMock, patch

from job_finder.web.db_migrate import run_migrations
from job_finder.web.primary_source_tiebreak import tiebreak_primary_posting

_SCAN = "job_finder.web.ats_platforms._registry.run_platform_scan"
_CALL_MODEL = "job_finder.web.model_provider.call_model"


def _verdict(match_index, confident):
    return MagicMock(data={"match_index": match_index, "confident": confident})


def _posting(title, url, **extra):
    return {"title": title, "source_url": url, "description": "x" * 300, **extra}


# ── unit: tiebreak_primary_posting ───────────────────────────────────────────


def test_confident_valid_index_returns_posting():
    postings = [
        _posting("Sr. SWE II", "https://jobs.lever.co/acme/1"),
        _posting("Staff Engineer", "https://jobs.lever.co/acme/2"),
    ]
    with patch(_CALL_MODEL, return_value=_verdict(1, True)) as cm:
        chosen = tiebreak_primary_posting(
            postings, "Staff Engineer (Platform)", "Remote", "snippet", None, {}
        )
    assert chosen is postings[1]
    kwargs = cm.call_args.kwargs
    assert kwargs["tier"] == "quick"
    assert kwargs["purpose"] == "primary_source_tiebreak"


def test_not_confident_stays_loose():
    postings = [_posting("Engineer", "https://jobs.lever.co/acme/1")]
    with patch(_CALL_MODEL, return_value=_verdict(0, False)):
        assert tiebreak_primary_posting(postings, "Engineer", "", None, None, {}) is None


def test_null_index_stays_loose():
    """The explicit "none of these / can't tell" exit (P13)."""
    postings = [_posting("Engineer", "https://jobs.lever.co/acme/1")]
    with patch(_CALL_MODEL, return_value=_verdict(None, True)):
        assert tiebreak_primary_posting(postings, "Engineer", "", None, None, {}) is None


def test_out_of_range_index_stays_loose():
    postings = [_posting("Engineer", "https://jobs.lever.co/acme/1")]
    with patch(_CALL_MODEL, return_value=_verdict(5, True)):
        assert tiebreak_primary_posting(postings, "Engineer", "", None, None, {}) is None


def test_boolean_index_stays_loose():
    """bool is an int subclass — true must not silently index posting 1."""
    postings = [
        _posting("Engineer", "https://jobs.lever.co/acme/1"),
        _posting("Designer", "https://jobs.lever.co/acme/2"),
    ]
    with patch(_CALL_MODEL, return_value=_verdict(True, True)):
        assert tiebreak_primary_posting(postings, "Engineer", "", None, None, {}) is None


def test_oversized_board_skips_model_call():
    postings = [_posting(f"Role {i}", f"https://jobs.lever.co/acme/{i}") for i in range(41)]
    with patch(_CALL_MODEL) as cm:
        assert tiebreak_primary_posting(postings, "Role 3", "", None, None, {}) is None
    assert cm.call_count == 0


def test_no_linked_candidates_skips_model_call():
    with patch(_CALL_MODEL) as cm:
        result = tiebreak_primary_posting([{"title": "Engineer"}], "Engineer", "", None, None, {})
    assert result is None
    assert cm.call_count == 0


# ── integration: resolver upgrade path ───────────────────────────────────────


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


def _resolve(conn, **kwargs):
    from job_finder.web.primary_source_resolver import resolve_primary_sources

    kwargs.setdefault("delay_range", (0.0, 0.0))
    return resolve_primary_sources(conn, {}, **kwargs)  # llm_tiebreak defaults ON


def test_tiebreak_upgrades_loose_to_strict_and_merges(tmp_path):
    """Title drift the heuristic can't bridge: a confident verdict upgrades
    the link to strict, unlocks the data merge, and tags the row."""
    conn = _migrated_db(tmp_path)
    _insert_company(conn, 1, "Acme")
    _insert_job(conn, "j1", "Sr. Data Scientist II", "Acme", company_id=1)
    conn.commit()

    postings = [
        _posting(
            "Senior Data Scientist",
            "https://jobs.lever.co/acme/1",
            salary_min=150000,
            salary_max=190000,
            company_source="Lever",
        ),
        _posting("Staff Engineer", "https://jobs.lever.co/acme/2", company_source="Lever"),
    ]
    with patch(_SCAN, return_value=postings), patch(_CALL_MODEL, return_value=_verdict(0, True)):
        stats = _resolve(conn)

    assert stats["llm_checked"] == 1
    assert stats["llm_upgraded"] == 1
    assert stats["strict"] == 1
    assert stats["loose"] == 0
    assert stats["merged"] == 1
    row = conn.execute(
        "SELECT direct_url, direct_url_confidence, salary_min, sources "
        "FROM jobs WHERE dedup_key='j1'"
    ).fetchone()
    assert row["direct_url"] == "https://jobs.lever.co/acme/1"
    assert row["direct_url_confidence"] == "strict"
    assert row["salary_min"] == 150000
    assert "primary_source_llm" in json.loads(row["sources"])  # P13 audit tag
    conn.close()


def test_tiebreak_declines_and_match_stays_loose(tmp_path):
    conn = _migrated_db(tmp_path)
    _insert_company(conn, 1, "Acme")
    _insert_job(conn, "j1", "Sr. Data Scientist II", "Acme", company_id=1)
    conn.commit()

    postings = [_posting("Senior Data Scientist", "https://jobs.lever.co/acme/1", salary_min=1)]
    with (
        patch(_SCAN, return_value=postings),
        patch(_CALL_MODEL, return_value=_verdict(None, False)),
    ):
        stats = _resolve(conn)

    assert stats["llm_checked"] == 1
    assert stats["llm_upgraded"] == 0
    assert stats["loose"] == 1
    row = conn.execute(
        "SELECT direct_url_confidence, salary_min FROM jobs WHERE dedup_key='j1'"
    ).fetchone()
    assert row["direct_url_confidence"] == "loose"
    assert row["salary_min"] is None  # contamination invariant holds
    conn.close()


def test_model_failure_disables_tiebreak_for_rest_of_run(tmp_path):
    """A dead cascade must not turn into a per-job timeout storm: the first
    exception disables tie-breaking, later loose jobs skip the model."""
    conn = _migrated_db(tmp_path)
    for cid, name in ((1, "Aaa"), (2, "Bbb")):
        _insert_company(conn, cid, name)
        _insert_job(conn, f"j{cid}", "Sr. Eng II", name, company_id=cid)
    conn.commit()

    def _board(_scanner, slug, *_args, **_kwargs):
        return [_posting("Senior Engineer", f"https://jobs.lever.co/{slug}/1")]

    with (
        patch(_SCAN, side_effect=_board),
        patch(_CALL_MODEL, side_effect=RuntimeError("cascade exhausted")) as cm,
    ):
        stats = _resolve(conn)

    assert cm.call_count == 1  # second company never consulted the model
    assert stats["llm_checked"] == 1
    assert stats["llm_upgraded"] == 0
    assert stats["loose"] == 2  # both jobs still got their loose links
    conn.close()


def test_config_disables_tiebreak(tmp_path):
    from job_finder.web.primary_source_resolver import resolve_primary_sources

    conn = _migrated_db(tmp_path)
    _insert_company(conn, 1, "Acme")
    _insert_job(conn, "j1", "Sr. Eng II", "Acme", company_id=1)
    conn.commit()

    postings = [_posting("Senior Engineer", "https://jobs.lever.co/acme/1")]
    config = {"direct_link": {"resolver": {"llm_tiebreak": False}}}
    with patch(_SCAN, return_value=postings), patch(_CALL_MODEL) as cm:
        stats = resolve_primary_sources(conn, config, delay_range=(0.0, 0.0))

    assert cm.call_count == 0
    assert stats["llm_checked"] == 0
    assert stats["loose"] == 1
    conn.close()


def test_strict_heuristic_match_never_consults_model(tmp_path):
    conn = _migrated_db(tmp_path)
    _insert_company(conn, 1, "Acme")
    _insert_job(conn, "j1", "Senior Data Scientist", "Acme", company_id=1)
    conn.commit()

    postings = [_posting("Senior Data Scientist", "https://jobs.lever.co/acme/1")]
    with patch(_SCAN, return_value=postings), patch(_CALL_MODEL) as cm:
        stats = _resolve(conn)

    assert cm.call_count == 0
    assert stats["strict"] == 1
    assert stats["llm_checked"] == 0
    conn.close()
