import sqlite3

from job_finder.web.autoheal import BREAK_THRESHOLD
from job_finder.web.autoheal import health_monitor as hm
from job_finder.web.db_migrate import run_migrations


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
