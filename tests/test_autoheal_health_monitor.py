import sqlite3

from job_finder.web import notifications
from job_finder.web.autoheal import BREAK_THRESHOLD
from job_finder.web.autoheal import health_monitor as hm
from job_finder.web.db_migrate import run_migrations

_NOTIFY_CONFIG = {"notifications": {"desktop": {"enabled": True}, "email": {"enabled": False}}}


def _conn(tmp_path):
    db = str(tmp_path / "t.db")
    run_migrations(db)
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    return str(db), c


def _establish_baseline(conn, source):
    for _ in range(3):
        hm.record_extraction(conn, source, "email", "x" * 400, job_count=2)


def test_record_creates_health_row(tmp_path):
    _, conn = _conn(tmp_path)
    hm.record_extraction(conn, "linkedin", "email", "x" * 400, job_count=2)
    row = conn.execute(
        "SELECT status, baseline_yield FROM source_health WHERE source='linkedin'"
    ).fetchone()
    assert row["status"] == "healthy"
    assert row["baseline_yield"] >= 1


def test_consecutive_zero_yields_flip_to_degraded(tmp_path):
    db, conn = _conn(tmp_path)
    _establish_baseline(conn, "linkedin")
    for _ in range(BREAK_THRESHOLD):
        hm.record_extraction(conn, "linkedin", "email", "x" * 400, job_count=0)
    flagged = hm.run_detection(db)
    row = conn.execute(
        "SELECT status, consecutive_breaks FROM source_health WHERE source='linkedin'"
    ).fetchone()
    assert row["status"] == "degraded"
    assert "linkedin" in flagged


def test_short_input_zero_does_not_count_as_break(tmp_path):
    db, conn = _conn(tmp_path)
    _establish_baseline(conn, "linkedin")
    hm.record_extraction(conn, "linkedin", "email", "tiny", job_count=0)
    row = conn.execute(
        "SELECT consecutive_breaks FROM source_health WHERE source='linkedin'"
    ).fetchone()
    assert row["consecutive_breaks"] == 0


def test_nonzero_yield_resets_break_counter(tmp_path):
    db, conn = _conn(tmp_path)
    _establish_baseline(conn, "linkedin")
    hm.record_extraction(conn, "linkedin", "email", "x" * 400, job_count=0)
    hm.record_extraction(conn, "linkedin", "email", "x" * 400, job_count=5)
    row = conn.execute(
        "SELECT consecutive_breaks, status FROM source_health WHERE source='linkedin'"
    ).fetchone()
    assert row["consecutive_breaks"] == 0
    assert row["status"] == "healthy"


def test_no_baseline_never_breaks(tmp_path):
    db, conn = _conn(tmp_path)
    for _ in range(BREAK_THRESHOLD + 2):
        hm.record_extraction(conn, "neversource", "email", "x" * 400, job_count=0)
    row = conn.execute("SELECT status FROM source_health WHERE source='neversource'").fetchone()
    assert row["status"] == "healthy"


def test_degraded_sources_reader(tmp_path):
    db, conn = _conn(tmp_path)
    _establish_baseline(conn, "linkedin")
    for _ in range(BREAK_THRESHOLD):
        hm.record_extraction(conn, "linkedin", "email", "x" * 400, job_count=0)
    hm.run_detection(db)
    degraded = hm.degraded_sources(conn)
    assert any(d["source"] == "linkedin" for d in degraded)


def test_run_detection_notifies_once_when_sources_flagged(tmp_path, monkeypatch):
    """#438: a single notify() fires when ≥1 source newly flips to degraded."""
    db, conn = _conn(tmp_path)
    calls: list[dict] = []
    monkeypatch.setattr(
        notifications,
        "notify",
        lambda *a, **kw: calls.append({"args": a, "kwargs": kw}) or (),
    )
    _establish_baseline(conn, "linkedin")
    for _ in range(BREAK_THRESHOLD):
        hm.record_extraction(conn, "linkedin", "email", "x" * 400, job_count=0)
    flagged = hm.run_detection(db, _NOTIFY_CONFIG)
    assert "linkedin" in flagged
    assert len(calls) == 1
    assert calls[0]["kwargs"]["severity"] == "critical"


def test_run_detection_does_not_notify_when_nothing_flagged(tmp_path, monkeypatch):
    """#438: no notify() when zero sources flip (healthy detection pass)."""
    db, conn = _conn(tmp_path)
    calls: list = []
    monkeypatch.setattr(notifications, "notify", lambda *a, **kw: calls.append(1) or ())
    _establish_baseline(conn, "linkedin")  # healthy, never breaks
    flagged = hm.run_detection(db, _NOTIFY_CONFIG)
    assert flagged == []
    assert calls == []


def test_run_detection_without_config_skips_notify(tmp_path, monkeypatch):
    """#438: legacy callers (config=None) never reach the egress path."""
    db, conn = _conn(tmp_path)
    calls: list = []
    monkeypatch.setattr(notifications, "notify", lambda *a, **kw: calls.append(1) or ())
    _establish_baseline(conn, "linkedin")
    for _ in range(BREAK_THRESHOLD):
        hm.record_extraction(conn, "linkedin", "email", "x" * 400, job_count=0)
    flagged = hm.run_detection(db)
    assert "linkedin" in flagged
    assert calls == []


def test_detect_false_captures_but_never_breaks(tmp_path):
    """ATS/careers capture path: baseline tracked, counter frozen at 0."""
    db, conn = _conn(tmp_path)
    for _ in range(3):
        hm.record_extraction(conn, "ats:greenhouse", "ats", "x" * 400, job_count=5, detect=False)
    for _ in range(BREAK_THRESHOLD + 2):
        hm.record_extraction(conn, "ats:greenhouse", "ats", "x" * 400, job_count=0, detect=False)
    row = conn.execute(
        "SELECT status, consecutive_breaks, baseline_yield FROM source_health WHERE source='ats:greenhouse'"
    ).fetchone()
    assert row["status"] == "healthy"
    assert row["consecutive_breaks"] == 0
    assert row["baseline_yield"] == 5.0  # baseline still recorded
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM corpus_sample WHERE source='ats:greenhouse'"
        ).fetchone()[0]
        > 0
    )
