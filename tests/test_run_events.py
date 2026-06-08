"""Unit tests for job_finder.web.run_events (structured orchestration log)."""

import json
import sqlite3

import pytest

from job_finder.web import run_events


@pytest.fixture
def events_file(tmp_path, monkeypatch):
    path = tmp_path / "run_events.jsonl"
    monkeypatch.setenv("JC_RUN_EVENTS_PATH", str(path))
    return path


def _read(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_events_path_env_override(events_file):
    assert run_events.events_path() == events_file


def test_start_end_roundtrip_correlates_run_id(events_file):
    run_id = run_events.start(job="enrichment", source="harness", pid=4242)
    run_events.end(
        run_id, job="enrichment", source="harness", disposition="completed",
        pid=4242, duration_s=12.5, exit_code=0, result={"scored": 3},
    )
    records = _read(events_file)
    assert [r["event"] for r in records] == ["run_start", "run_end"]
    assert {r["run_id"] for r in records} == {run_id}
    start, end = records
    assert start["job"] == "enrichment" and start["source"] == "harness" and start["pid"] == 4242
    assert end["disposition"] == "completed" and end["exit_code"] == 0
    assert "scored" in end["result"]  # result clipped to a string
    assert all(r["v"] == run_events.SCHEMA_VERSION for r in records)


def test_make_run_id_deterministic_form_matches_supervisor_reconstruction():
    # The harness uses unique=False so a separate supervisor process can rebuild
    # the same id from (job, pid) alone.
    assert run_events.make_run_id("enrichment", 999, unique=False) == "enrichment:999"
    assert run_events.make_run_id("enrichment", 999, unique=True).startswith("enrichment:999:")


def test_events_are_independent_records(events_file):
    run_events.start(job="a", source="harness", pid=1)
    run_events.start(job="b", source="harness", pid=2)
    records = _read(events_file)
    assert [r["job"] for r in records] == ["a", "b"]
    assert records[0]["ts"] is not None and "T" in records[0]["ts"]  # naive UTC ISO


def test_none_fields_are_dropped(events_file):
    run_events.start(job="a", source="harness", pid=1, cmd=None)
    rec = _read(events_file)[0]
    assert "cmd" not in rec  # None extras omitted for compact lines


def test_db_counters_reads_jobs_table(tmp_path):
    db = tmp_path / "jobs.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE jobs (jd_full TEXT, classification TEXT, pipeline_status TEXT, first_seen TEXT)"
    )
    # 1 scorable (jd_full + null classification + not archived), 1 archived, 1 scored.
    conn.executemany(
        "INSERT INTO jobs VALUES (?,?,?,?)",
        [
            ("body", None, None, "2020-01-01"),
            ("body", None, "archived", "2020-01-01"),
            ("body", "apply", None, "2020-01-01"),
            (None, None, None, "2020-01-01"),  # missing jd_full
        ],
    )
    conn.commit()
    conn.close()
    counters = run_events.db_counters(str(db))
    assert counters["total_jobs"] == 4
    assert counters["scoring_backlog"] == 1  # only the first row qualifies
    assert counters["classification_null"] == 3
    assert counters["missing_jd_full"] == 1


def test_db_counters_bad_path_returns_error_not_raises():
    out = run_events.db_counters("C:/nonexistent/nope.db")
    assert "error" in out  # never raises


def test_db_counters_none_path_is_none():
    assert run_events.db_counters(None) is None


def test_delta_computes_integer_field_diffs():
    before = {"total_jobs": 100, "scoring_backlog": 196, "note": "x"}
    after = {"total_jobs": 100, "scoring_backlog": 0, "note": "y"}
    assert run_events._delta(before, after) == {"total_jobs": 0, "scoring_backlog": -196}


def test_delta_handles_missing_or_nondict():
    assert run_events._delta(None, {"a": 1}) is None
    assert run_events._delta({"a": "x"}, {"a": "y"}) is None  # non-int -> no delta keys


def test_find_terminal_detects_run_end(events_file):
    run_id = run_events.start(job="x", source="harness", pid=7)
    assert run_events.find_terminal(run_id) is None  # no terminal yet
    run_events.end(run_id, job="x", source="harness", disposition="failed", error="Boom")
    assert run_events.find_terminal(run_id) == "failed"


def test_find_terminal_detects_supervisor_reaped(events_file):
    run_id = "enrichment:4242"
    run_events.start(run_id=run_id, job="enrichment", source="harness", pid=4242)
    run_events.mark("reaped", run_id, job="enrichment", source="supervisor", pid=4242)
    assert run_events.find_terminal(run_id) == "reaped"


def test_emit_never_raises_when_path_unwritable(tmp_path, monkeypatch):
    # Point the events path at a directory: open(dir, "a") fails -> _append must
    # swallow so the job is never broken by instrumentation.
    blocker = tmp_path / "is_a_dir"
    blocker.mkdir()
    monkeypatch.setenv("JC_RUN_EVENTS_PATH", str(blocker))
    run_id = run_events.start(job="x", source="harness", pid=1)  # must not raise
    assert run_id == run_events.make_run_id("x", 1)
