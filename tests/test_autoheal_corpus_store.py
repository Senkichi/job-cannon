import sqlite3

from job_finder.web.autoheal import corpus_store
from job_finder.web.db_migrate import run_migrations


def _conn(tmp_path):
    db = str(tmp_path / "t.db")
    run_migrations(db)
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    return c


def test_append_inserts_scrubbed_sample(tmp_path):
    conn = _conn(tmp_path)
    corpus_store.append_sample(
        conn,
        "linkedin",
        "email",
        "To: senki@x.com\nSoftware Engineer",
        {"job_count": 1},
    )
    row = conn.execute("SELECT raw_text, output_json FROM corpus_sample").fetchone()
    assert "To: senki@x.com" not in row["raw_text"]
    assert '"job_count": 1' in row["output_json"]


def test_ring_buffer_evicts_oldest(tmp_path):
    conn = _conn(tmp_path)
    for i in range(corpus_store.MAX_SAMPLES_PER_SOURCE + 5):
        corpus_store.append_sample(conn, "linkedin", "email", f"body {i}", {"job_count": 1})
    n = conn.execute("SELECT COUNT(*) FROM corpus_sample WHERE source='linkedin'").fetchone()[0]
    assert n == corpus_store.MAX_SAMPLES_PER_SOURCE


def test_baseline_yield_averages_recent_nonzero(tmp_path):
    conn = _conn(tmp_path)
    for c in (2, 4):
        corpus_store.append_sample(conn, "glassdoor", "email", "x" * 300, {"job_count": c})
    assert corpus_store.baseline_yield(conn, "glassdoor") == 3.0


def test_baseline_yield_zero_when_no_history(tmp_path):
    conn = _conn(tmp_path)
    assert corpus_store.baseline_yield(conn, "unknown") == 0.0
