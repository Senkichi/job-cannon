"""Unit tests for the set_direct_url gated DB writer."""

from __future__ import annotations

import sqlite3

import pytest

from job_finder.db._direct_link import set_direct_url


@pytest.fixture()
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute(
        "CREATE TABLE jobs (dedup_key TEXT PRIMARY KEY, direct_url TEXT, "
        "direct_url_confidence TEXT)"
    )
    c.execute("INSERT INTO jobs (dedup_key) VALUES ('k')")
    c.commit()
    return c


def _read(conn):
    r = conn.execute(
        "SELECT direct_url, direct_url_confidence FROM jobs WHERE dedup_key='k'"
    ).fetchone()
    return r["direct_url"], r["direct_url_confidence"]


def test_writes_strict_into_null(conn):
    assert set_direct_url(conn, "k", "https://x/strict", "strict") is True
    assert _read(conn) == ("https://x/strict", "strict")


def test_writes_loose_into_null(conn):
    assert set_direct_url(conn, "k", "https://x/loose", "loose") is True
    assert _read(conn) == ("https://x/loose", "loose")


def test_loose_does_not_overwrite_existing_loose(conn):
    set_direct_url(conn, "k", "https://x/first", "loose")
    assert set_direct_url(conn, "k", "https://x/second", "loose") is False
    assert _read(conn) == ("https://x/first", "loose")


def test_loose_does_not_overwrite_strict(conn):
    set_direct_url(conn, "k", "https://x/strict", "strict")
    assert set_direct_url(conn, "k", "https://x/loose", "loose") is False
    assert _read(conn) == ("https://x/strict", "strict")


def test_strict_upgrades_loose(conn):
    set_direct_url(conn, "k", "https://x/loose", "loose")
    assert set_direct_url(conn, "k", "https://x/strict", "strict") is True
    assert _read(conn) == ("https://x/strict", "strict")


def test_strict_does_not_overwrite_existing_strict(conn):
    set_direct_url(conn, "k", "https://x/first", "strict")
    assert set_direct_url(conn, "k", "https://x/second", "strict") is False
    assert _read(conn) == ("https://x/first", "strict")


def test_rejects_empty_url(conn):
    assert set_direct_url(conn, "k", "", "strict") is False
    assert set_direct_url(conn, "k", None, "strict") is False
    assert _read(conn) == (None, None)


def test_rejects_unknown_confidence(conn):
    assert set_direct_url(conn, "k", "https://x", "bogus") is False
    assert _read(conn) == (None, None)


def test_returns_false_for_missing_row(conn):
    assert set_direct_url(conn, "nope", "https://x", "strict") is False
