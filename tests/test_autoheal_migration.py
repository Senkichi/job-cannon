import sqlite3

from job_finder.web.db_migrate import run_migrations


def _columns(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def test_m084_creates_health_tables(tmp_path):
    db = str(tmp_path / "t.db")
    run_migrations(db)
    conn = sqlite3.connect(db)
    assert _columns(conn, "corpus_sample") >= {
        "id",
        "source",
        "surface",
        "raw_text",
        "output_json",
        "captured_at",
    }
    assert _columns(conn, "source_health") >= {
        "source",
        "surface",
        "status",
        "consecutive_breaks",
        "baseline_yield",
        "last_signal",
        "last_break_at",
        "updated_at",
    }
    assert conn.execute("PRAGMA user_version").fetchone()[0] >= 84
    conn.close()
