"""Tests for autoheal email drain helpers: _user_identifiers + _record_email_extractions (Task 7)."""

import sqlite3
import types

from job_finder.web.db_migrate import run_migrations
from job_finder.web.ingestion_runner import _record_email_extractions, _user_identifiers


def test_user_identifiers_from_config():
    cfg = {"sources": {"imap": {"email": "me@x.com"}}, "profile": {"name": "Jane Doe"}}
    assert _user_identifiers(cfg) == ("me@x.com", "Jane Doe")


def test_user_identifiers_empty_when_absent():
    assert _user_identifiers({}) == ()


def test_drain_records_each_extraction(tmp_path):
    db = str(tmp_path / "t.db")
    run_migrations(db)
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    fake = types.SimpleNamespace(
        extraction_records=[
            {"label": "linkedin", "raw_text": "x" * 400, "job_count": 3},
            {"label": "glassdoor", "raw_text": "y" * 400, "job_count": 0},
        ]
    )
    _record_email_extractions(fake, conn, {})
    assert conn.execute("SELECT COUNT(*) FROM corpus_sample").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM source_health").fetchone()[0] == 2
