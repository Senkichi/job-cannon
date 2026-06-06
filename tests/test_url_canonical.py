"""Tests for Phase 49.01 — URL canonicalization + m080 source_urls_raw."""

from __future__ import annotations

import json
import sqlite3

from job_finder.models import Job
from job_finder.parsed_job import ParsedJob
from job_finder.web.migrations._runner import _apply_migration
from job_finder.web.migrations.m080_source_urls_canonical import MIGRATION as M080
from job_finder.web.migrations.types import MigrationContext
from job_finder.web.url_canonical import canonicalize_url


# ---------------------------------------------------------------------------
# canonicalize_url
# ---------------------------------------------------------------------------


def test_strips_utm_tracking_param():
    canonical, raw = canonicalize_url("https://example.com/job?utm_source=foo&id=42")
    assert canonical == "https://example.com/job?id=42"
    assert raw == "https://example.com/job?utm_source=foo&id=42"


def test_strips_wildcard_mc_family():
    canonical, _ = canonicalize_url("https://example.com/job?mc_cid=abc&id=42")
    assert canonical == "https://example.com/job?id=42"


def test_strips_exact_allowlist_keys():
    # gh_jid, fbclid, refId, trk, lipi, ref, _hsenc, _hsmi
    url = "https://example.com/x?gh_jid=1&fbclid=2&refId=3&trk=4&lipi=5&ref=6&_hsenc=7&_hsmi=8&keep=ok"
    canonical, _ = canonicalize_url(url)
    assert canonical == "https://example.com/x?keep=ok"


def test_query_order_normalization_is_stable():
    a, _ = canonicalize_url("https://example.com/job?b=2&a=1")
    b, _ = canonicalize_url("https://example.com/job?a=1&b=2")
    assert a == b == "https://example.com/job?a=1&b=2"


def test_lowercases_scheme_and_host_preserves_path():
    canonical, _ = canonicalize_url("HTTPS://Example.COM/Jobs/View/42?utm_term=x")
    assert canonical == "https://example.com/Jobs/View/42"


def test_unparseable_returns_raw_without_raising():
    bad = "http://[::1"  # malformed IPv6 host → urlsplit raises ValueError
    canonical, raw = canonicalize_url(bad)
    assert canonical == bad
    assert raw == bad


def test_empty_string_roundtrips():
    assert canonicalize_url("") == ("", "")


# ---------------------------------------------------------------------------
# ParsedJob integration — canonicalization at construction
# ---------------------------------------------------------------------------


def test_parsed_job_canonicalizes_source_urls_and_preserves_raw():
    job = Job(
        title="Data Scientist",
        company="Acme",
        location="Remote",
        source="greenhouse",
        source_url="https://acme.com/jobs/1?utm_source=foo&id=1",
    )
    parsed = ParsedJob.from_job(
        job,
        source_meta={
            "source_urls": [
                "https://acme.com/jobs/1?utm_source=foo&id=1",
                "https://Boards.Greenhouse.io/acme/jobs/2?gh_jid=9&b=2&a=1",
            ],
        },
    )
    assert parsed.source_urls == [
        "https://acme.com/jobs/1?id=1",
        "https://boards.greenhouse.io/acme/jobs/2?a=1&b=2",
    ]
    assert parsed.source_urls_raw == [
        "https://acme.com/jobs/1?utm_source=foo&id=1",
        "https://Boards.Greenhouse.io/acme/jobs/2?gh_jid=9&b=2&a=1",
    ]


# ---------------------------------------------------------------------------
# m080 migration
# ---------------------------------------------------------------------------


def _pre_m080_db(path: str) -> None:
    """A jobs table at the pre-m080 schema (no source_urls_raw)."""
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """
            CREATE TABLE jobs (
                dedup_key TEXT PRIMARY KEY,
                source_urls TEXT
            )
            """
        )
        conn.executemany(
            "INSERT INTO jobs (dedup_key, source_urls) VALUES (?, ?)",
            [
                ("a", json.dumps(["https://x.com/1?utm_source=foo&id=1"])),
                ("b", json.dumps(["https://X.com/2?b=2&a=1"])),
                ("c", json.dumps([])),
                ("d", None),
            ],
        )
        conn.commit()
    finally:
        conn.close()


def _apply(path: str) -> None:
    conn = sqlite3.connect(path)
    try:
        ctx = MigrationContext(conn=conn, db_path=path, user_data_root=".", initial_version=79)
        _apply_migration(ctx, M080)
    finally:
        conn.close()


def test_m080_adds_column_canonicalizes_and_preserves_raw(tmp_db_path):
    _pre_m080_db(tmp_db_path)
    _apply(tmp_db_path)

    conn = sqlite3.connect(tmp_db_path)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        assert "source_urls_raw" in cols

        rows = dict(
            (dk, (su, sur))
            for dk, su, sur in conn.execute(
                "SELECT dedup_key, source_urls, source_urls_raw FROM jobs"
            ).fetchall()
        )
    finally:
        conn.close()

    # Row a: canonicalized, raw preserved.
    assert json.loads(rows["a"][0]) == ["https://x.com/1?id=1"]
    assert json.loads(rows["a"][1]) == ["https://x.com/1?utm_source=foo&id=1"]
    # Row b: host lowered + query sorted.
    assert json.loads(rows["b"][0]) == ["https://x.com/2?a=1&b=2"]
    assert json.loads(rows["b"][1]) == ["https://X.com/2?b=2&a=1"]


def test_m080_is_idempotent(tmp_db_path):
    _pre_m080_db(tmp_db_path)
    _apply(tmp_db_path)
    # Re-running against the now-canonical table must not change anything.
    conn = sqlite3.connect(tmp_db_path)
    try:
        before = conn.execute(
            "SELECT dedup_key, source_urls, source_urls_raw FROM jobs ORDER BY dedup_key"
        ).fetchall()
    finally:
        conn.close()

    _apply(tmp_db_path)

    conn = sqlite3.connect(tmp_db_path)
    try:
        after = conn.execute(
            "SELECT dedup_key, source_urls, source_urls_raw FROM jobs ORDER BY dedup_key"
        ).fetchall()
    finally:
        conn.close()
    assert before == after
