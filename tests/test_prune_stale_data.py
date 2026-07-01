"""Tests for _prune_stale_data — prune-growth bug fix (D6 in issue #649)."""

import sqlite3
from datetime import datetime, timedelta

import pytest

from job_finder.web.ingestion_runner import _prune_stale_data


@pytest.fixture
def db_conn(tmp_path):
    """Create an in-memory SQLite database with email_parse_log and runs tables."""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS email_parse_log (
            message_id TEXT PRIMARY KEY,
            processed_at TIMESTAMP,
            sender TEXT
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY,
            timestamp TIMESTAMP,
            source TEXT
        )"""
    )
    conn.commit()
    yield conn
    conn.close()


def test_prune_stale_data_deletes_imap_rows(db_conn):
    """Prune-growth fix: _prune_stale_data deletes sender='imap' rows (not just 'gmail')."""
    # Seed email_parse_log with sender='imap' rows older than TTL
    lookback_days = 7
    ttl_days = max(lookback_days * 2, 14)  # 14 days

    # Insert old imap rows (older than TTL)
    old_date = (datetime.now() - timedelta(days=ttl_days + 1)).strftime("%Y-%m-%d %H:%M:%S")
    db_conn.execute(
        "INSERT INTO email_parse_log (message_id, processed_at, sender) VALUES (?, ?, ?)",
        ("old_imap_1", old_date, "imap"),
    )
    db_conn.execute(
        "INSERT INTO email_parse_log (message_id, processed_at, sender) VALUES (?, ?, ?)",
        ("old_imap_2", old_date, "imap"),
    )

    # Insert recent imap rows (within TTL)
    recent_date = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    db_conn.execute(
        "INSERT INTO email_parse_log (message_id, processed_at, sender) VALUES (?, ?, ?)",
        ("recent_imap_1", recent_date, "imap"),
    )

    # Insert old gmail rows (older than TTL) - for historical comparison
    db_conn.execute(
        "INSERT INTO email_parse_log (message_id, processed_at, sender) VALUES (?, ?, ?)",
        ("old_gmail_1", old_date, "gmail"),
    )

    db_conn.commit()

    # Before prune: 4 rows total
    cursor = db_conn.execute("SELECT COUNT(*) FROM email_parse_log")
    assert cursor.fetchone()[0] == 4

    # Run prune
    _prune_stale_data(db_conn, lookback_days)

    # After prune: only recent_imap_1 remains (old imap and old gmail deleted)
    cursor = db_conn.execute("SELECT COUNT(*) FROM email_parse_log")
    assert cursor.fetchone()[0] == 1

    # Verify the remaining row is the recent one
    cursor = db_conn.execute("SELECT message_id FROM email_parse_log")
    assert cursor.fetchone()[0] == "recent_imap_1"


def test_prune_stale_data_respects_ttl(db_conn):
    """Rows within TTL are preserved regardless of sender."""
    lookback_days = 7
    ttl_days = max(lookback_days * 2, 14)  # 14 days

    # Insert rows at various ages
    now = datetime.now()
    ages = [
        (ttl_days - 1, "within_ttl_imap", "imap"),  # Just within TTL
        (ttl_days, "at_ttl_imap", "imap"),  # At TTL boundary (deleted by < condition)
        (ttl_days + 1, "just_over_ttl_imap", "imap"),  # Just over TTL
        (ttl_days + 10, "well_over_ttl_gmail", "gmail"),  # Well over TTL
    ]

    for days_ago, msg_id, sender in ages:
        date = (now - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")
        db_conn.execute(
            "INSERT INTO email_parse_log (message_id, processed_at, sender) VALUES (?, ?, ?)",
            (msg_id, date, sender),
        )

    db_conn.commit()

    # Before prune: 4 rows
    cursor = db_conn.execute("SELECT COUNT(*) FROM email_parse_log")
    assert cursor.fetchone()[0] == 4

    # Run prune
    _prune_stale_data(db_conn, lookback_days)

    # After prune: only rows strictly within TTL remain (at_ttl boundary is deleted by <)
    cursor = db_conn.execute("SELECT COUNT(*) FROM email_parse_log")
    assert cursor.fetchone()[0] == 1

    # Verify the remaining row
    cursor = db_conn.execute("SELECT message_id FROM email_parse_log ORDER BY message_id")
    remaining = [row[0] for row in cursor.fetchall()]
    assert set(remaining) == {"within_ttl_imap"}


def test_prune_stale_data_empty_table(db_conn):
    """Prune on empty table is safe (no-op)."""
    _prune_stale_data(db_conn, lookback_days=7)
    cursor = db_conn.execute("SELECT COUNT(*) FROM email_parse_log")
    assert cursor.fetchone()[0] == 0
