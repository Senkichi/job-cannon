"""Tests for the normalize_jd boundary enforcement (Issue #256).

Acceptance criteria:
  1. HTML-bloated text fed through set_jd_full() is stored as clean plain text
     (no HTML tags, idempotent on re-application).
  2. Already-plain text is unchanged (idempotent / no data loss).
  3. _JD_JUNK_PREFIXES and _MIN_JD_LENGTH are defined in exactly one module
     (no duplicate literals in m078 or pre_m078_remediation).
  4. build_jd_junk_trigger_sql() output equals the live trigger DDL in a
     freshly-migrated test DB.
  5. An enrichment-write round-trip over HTML fixtures yields zero rows
     matching the m079 HTML-pollution heuristic.

Reference: GitHub issue #256.
"""

from __future__ import annotations

import os
import re
import sqlite3
import tempfile

import pytest

os.environ.setdefault("GSD_BACKUP_CONFIRMED", "1")

# ---------------------------------------------------------------------------
# Helpers shared across tests
# ---------------------------------------------------------------------------

_HTML_TAG_RE = re.compile(r"<[a-zA-Z/][^>]*>")

# m079's HTML signal predicates (Python mirror for the test assertion).
_HTML_SIGNAL_RE = re.compile(
    r"(&lt;|</([\w]+)>|<p[\s>]|<div|<br|<li|<ul|<h[1-6])",
    re.IGNORECASE,
)


def _make_migrated_db() -> tuple[str, sqlite3.Connection]:
    """Return (path, conn) for a fresh DB with all migrations applied."""
    from job_finder.web.db_migrate import run_migrations

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    run_migrations(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return path, conn


def _insert_job(conn: sqlite3.Connection, dedup_key: str) -> None:
    """Insert a minimal job row with jd_full = NULL."""
    conn.execute(
        """INSERT INTO jobs
               (dedup_key, title, company, location, sources, source_urls,
                first_seen, last_seen, score_breakdown, locations_raw,
                unresolved_reasons)
           VALUES (?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'), '{}', '[]', '[]')""",
        (dedup_key, "Test Job", "TestCo", "", '["test"]', "[]"),
    )
    conn.commit()


def _read_jd(conn: sqlite3.Connection, dedup_key: str) -> str | None:
    row = conn.execute("SELECT jd_full FROM jobs WHERE dedup_key = ?", (dedup_key,)).fetchone()
    return row["jd_full"] if row else None


@pytest.fixture()
def db():
    """Yield (path, conn) for a migrated temp DB; clean up after."""
    path, conn = _make_migrated_db()
    try:
        yield path, conn
    finally:
        conn.close()
        if os.path.exists(path):
            os.remove(path)


# ---------------------------------------------------------------------------
# 1. HTML-bloated text is stored as clean plain text
# ---------------------------------------------------------------------------

# A realistic ATS-style JD fragment with entity-escaped HTML tags (>200 chars).
_HTML_JD_ENTITY_ESCAPED = (
    "&lt;p&gt;We are seeking a talented Software Engineer to join our growing team.&lt;/p&gt;"
    "&lt;ul&gt;&lt;li&gt;Build distributed systems&lt;/li&gt;"
    "&lt;li&gt;Mentor junior engineers&lt;/li&gt;"
    "&lt;li&gt;Collaborate with product managers&lt;/li&gt;&lt;/ul&gt;"
    "&lt;p&gt;Requirements: 5+ years Python, strong system design skills, BS/MS CS.&lt;/p&gt;"
)

# A realistic ATS-style JD with real (unescaped) HTML tags (>200 chars).
_HTML_JD_RAW_TAGS = (
    "<p>We are seeking a talented Software Engineer to join our growing team.</p>"
    "<ul><li>Build distributed systems</li>"
    "<li>Mentor junior engineers</li>"
    "<li>Collaborate with product managers</li></ul>"
    "<p>Requirements: 5+ years Python, strong system design skills, BS/MS CS.</p>"
)

# A clean plain-text JD that should pass through unchanged (>200 chars).
_PLAIN_JD = (
    "We are seeking a talented Software Engineer to join our growing team.\n"
    "- Build distributed systems\n"
    "- Mentor junior engineers\n"
    "- Collaborate with product managers\n"
    "Requirements: 5+ years Python, strong system design skills, BS/MS CS."
)


@pytest.mark.parametrize(
    "html_text",
    [_HTML_JD_ENTITY_ESCAPED, _HTML_JD_RAW_TAGS],
    ids=["entity_escaped_html", "raw_html_tags"],
)
def test_html_jd_stored_as_clean_plain_text(db, html_text):
    """HTML-bloated text fed through set_jd_full() is stored without HTML tags."""
    from job_finder.db._jd_full import set_jd_full

    _, conn = db
    dedup_key = "test|html_jd"
    _insert_job(conn, dedup_key)

    result = set_jd_full(conn, dedup_key, html_text, source="test")

    assert result is True, "set_jd_full should accept and write a long HTML JD"
    stored = _read_jd(conn, dedup_key)
    assert stored is not None, "jd_full should be written"
    assert not _HTML_TAG_RE.search(stored), (
        f"Stored jd_full should contain no HTML tags; got: {stored[:120]!r}"
    )
    assert "&lt;" not in stored, "Stored jd_full should contain no entity-escaped tags"
    # Sanity: meaningful content was preserved (not blanked)
    assert len(stored.strip()) >= 50, "Stored jd_full should retain meaningful content"


def test_html_jd_stored_no_nav_chrome(db):
    """HTML with nav chrome (full page structure) is stripped to body content only."""
    from job_finder.db._jd_full import set_jd_full

    _, conn = db
    dedup_key = "test|nav_chrome"
    _insert_job(conn, dedup_key)

    # Simulate soup.get_text() style bloat — nav header + real JD body
    html_with_nav = (
        "<nav>Home | Jobs | About | Contact</nav>"
        "<header><h1>Acme Corp Careers</h1></header>"
        "<div class='job-content'>"
        "<p>We are seeking a talented Software Engineer to join our growing team.</p>"
        "<ul><li>Build distributed systems and mentor engineers.</li>"
        "<li>Collaborate with product managers and deliver high-impact features.</li>"
        "<li>Requirements: 5+ years Python, strong system design skills.</li></ul>"
        "</div>"
        "<footer>Privacy Policy | Cookie Policy</footer>"
    )
    result = set_jd_full(conn, dedup_key, html_with_nav, source="test")

    assert result is True
    stored = _read_jd(conn, dedup_key)
    assert stored is not None
    assert not _HTML_TAG_RE.search(stored), "Should have no HTML tags after normalization"
    assert "Software Engineer" in stored, "Job title content must be preserved"


# ---------------------------------------------------------------------------
# 2. Already-plain text is unchanged (idempotency)
# ---------------------------------------------------------------------------


def test_plain_jd_stored_unchanged(db):
    """Plain-text JD is written to DB unchanged — normalize_jd is idempotent."""
    from job_finder.db._jd_full import set_jd_full

    _, conn = db
    dedup_key = "test|plain_jd"
    _insert_job(conn, dedup_key)

    result = set_jd_full(conn, dedup_key, _PLAIN_JD, source="test")

    assert result is True
    stored = _read_jd(conn, dedup_key)
    assert stored == _PLAIN_JD, "Plain-text JD should be stored byte-for-byte unchanged"


def test_normalize_jd_idempotent_on_already_clean():
    """normalize_jd applied twice to already-clean text returns the same result."""
    from job_finder.db._jd_full import normalize_jd

    result1 = normalize_jd(_PLAIN_JD)
    result2 = normalize_jd(result1)
    assert result1 == result2, "normalize_jd must be idempotent"


def test_normalize_jd_idempotent_on_html():
    """normalize_jd applied twice to HTML text returns the same result."""
    from job_finder.db._jd_full import normalize_jd

    result1 = normalize_jd(_HTML_JD_RAW_TAGS)
    result2 = normalize_jd(result1)
    assert result1 == result2, "normalize_jd must be idempotent on HTML input"


# ---------------------------------------------------------------------------
# 3. Constants defined in exactly one module
# ---------------------------------------------------------------------------


def test_jd_junk_prefixes_defined_once():
    """_JD_JUNK_PREFIXES and _MIN_JD_LENGTH exist only in _jd_full.py.

    Asserts by import: m078_contract_invariants and pre_m078_remediation must
    expose the constants, and they must be the same object (or equal value)
    as the canonical ones in _jd_full.
    """
    from job_finder.db._jd_full import _JD_JUNK_PREFIXES, _MIN_JD_LENGTH
    from scripts.pre_m078_remediation import JD_JUNK_PREFIXES, MIN_JD_LENGTH

    assert JD_JUNK_PREFIXES == _JD_JUNK_PREFIXES, (
        "pre_m078_remediation.JD_JUNK_PREFIXES must equal _jd_full._JD_JUNK_PREFIXES"
    )
    assert MIN_JD_LENGTH == _MIN_JD_LENGTH, (
        "pre_m078_remediation.MIN_JD_LENGTH must equal _jd_full._MIN_JD_LENGTH"
    )


def test_m078_no_local_junk_prefix_literal():
    """m078_contract_invariants must not define its own _JD_JUNK_PREFIXES tuple.

    The constants must be sourced from _jd_full.py.  We verify by checking that
    importing m078_contract_invariants does NOT expose module-level
    ``_JD_JUNK_PREFIXES`` or ``_MIN_JD_LENGTH`` attributes of its own — i.e.
    that those names either don't exist on the module or they are the same
    objects as in _jd_full (re-exported, not redefined).
    """

    import job_finder.db._jd_full as jd_full_mod
    import job_finder.web.migrations.m078_contract_invariants as m078_mod

    # The module should NOT have _JD_JUNK_PREFIXES as a local attribute
    # (it should import from _jd_full, not define its own).
    if hasattr(m078_mod, "_JD_JUNK_PREFIXES"):
        # If it does exist, it must be the same object (re-exported)
        assert m078_mod._JD_JUNK_PREFIXES is jd_full_mod._JD_JUNK_PREFIXES, (
            "m078_contract_invariants._JD_JUNK_PREFIXES must be the same object "
            "as _jd_full._JD_JUNK_PREFIXES, not a local redefinition"
        )


def test_pre_m078_imports_is_jd_junk_from_jd_full():
    """pre_m078_remediation._is_jd_junk is the same function as _jd_full._is_jd_junk."""
    import scripts.pre_m078_remediation as script_mod
    from job_finder.db._jd_full import _is_jd_junk as canonical

    assert script_mod._is_jd_junk is canonical, (
        "pre_m078_remediation._is_jd_junk must be imported from _jd_full, not redefined"
    )


# ---------------------------------------------------------------------------
# 4. build_jd_junk_trigger_sql() matches live trigger DDL in freshly-migrated DB
# ---------------------------------------------------------------------------


def test_trigger_sql_matches_live_db(db):
    """The SQL generated by build_jd_junk_trigger_sql() equals the DDL in a live DB.

    After m078 applies, sqlite_master stores the trigger DDL exactly as
    _create_triggers() built it.  The WHEN clause in that DDL must match what
    build_jd_junk_trigger_sql('NEW.jd_full') produces — any future change to
    the junk constants must regenerate the trigger SQL (or this test fails).
    """
    from job_finder.db._jd_full import build_jd_junk_trigger_sql

    _, conn = db

    # Fetch the live DDL for the I-13 INSERT trigger from the migrated DB.
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' AND name='tg_jobs_jd_full_junk_ins'"
    ).fetchone()
    assert row is not None, "tg_jobs_jd_full_junk_ins trigger must exist after migration"

    live_ddl: str = row["sql"]

    # The WHEN clause in the live DDL must contain the SQL expression generated
    # by build_jd_junk_trigger_sql('NEW.jd_full').
    expected_when = build_jd_junk_trigger_sql("NEW.jd_full")

    # Normalize whitespace for a robust comparison (SQLite may store with
    # slightly different indentation than our Python formatting).
    def _normalize_ws(s: str) -> str:
        return re.sub(r"\s+", " ", s).strip()

    assert _normalize_ws(expected_when) in _normalize_ws(live_ddl), (
        "build_jd_junk_trigger_sql('NEW.jd_full') output not found in live trigger DDL.\n"
        f"Expected WHEN expression:\n{expected_when}\n\n"
        f"Live DDL:\n{live_ddl}"
    )


# ---------------------------------------------------------------------------
# 5. Enrichment-write round-trip: zero rows match m079 HTML-pollution heuristic
# ---------------------------------------------------------------------------

# The _HTML_SIGNAL_SQL predicate from m079 (Python equivalent for the test).
# Detects the same HTML signals the migration scans for.
_HTML_SIGNAL_PATTERNS = (
    "%&lt;%",
    "%</%",
    "%<p>%",
    "%<p %",
    "%<div%",
    "%<br%",
    "%<li%",
    "%<ul%",
    "%<h1%",
    "%<h2%",
    "%<h3%",
)


def _count_html_polluted(conn: sqlite3.Connection) -> int:
    """Count rows whose jd_full contains HTML signals (mirrors m079 _HTML_SIGNAL_SQL)."""
    like_clauses = " OR ".join("jd_full LIKE ?" for _ in _HTML_SIGNAL_PATTERNS)
    sql = f"SELECT COUNT(*) FROM jobs WHERE jd_full IS NOT NULL AND ({like_clauses})"
    return conn.execute(sql, _HTML_SIGNAL_PATTERNS).fetchone()[0]


@pytest.mark.parametrize(
    "html_text,dedup_key",
    [
        (_HTML_JD_ENTITY_ESCAPED, "enrich|entity_escaped"),
        (_HTML_JD_RAW_TAGS, "enrich|raw_tags"),
        (
            "<div><h2>About the Role</h2><p>We are looking for a Principal Engineer "
            "to lead our platform team. You will own the technical roadmap, partner "
            "with engineering directors, and drive cross-team delivery.</p>"
            "<ul><li>10+ years distributed systems experience</li>"
            "<li>Strong leadership and communication skills</li></ul></div>",
            "enrich|div_wrapped",
        ),
    ],
    ids=["entity_escaped", "raw_html", "div_wrapped"],
)
def test_enrichment_write_leaves_no_html_pollution(db, html_text, dedup_key):
    """Writing HTML-sourced jd_full via set_jd_full yields no m079-detectable pollution."""
    from job_finder.db._jd_full import set_jd_full

    _, conn = db
    _insert_job(conn, dedup_key)

    set_jd_full(conn, dedup_key, html_text, source="enrichment")

    polluted = _count_html_polluted(conn)
    assert polluted == 0, (
        f"After enrichment write, {polluted} row(s) still contain HTML pollution "
        f"(m079 heuristic). jd_full stored: {_read_jd(conn, dedup_key)!r}"
    )
