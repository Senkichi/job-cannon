"""Unit tests for scripts/_jc_supervise.py (detached run supervisor).

The supervisor's value is its disposition decision (reaped vs stalled vs clean
exit), factored into a pure ``classify`` so it is testable without spawning or
killing real processes.
"""

import importlib.util
import os
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "_jc_supervise.py"


@pytest.fixture(scope="module")
def sup():
    spec = importlib.util.spec_from_file_location("_jc_supervise", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- classify: the disposition precedence ---------------------------------- #
def test_classify_clean_exit_wins_even_if_alive(sup):
    # A terminal run_end on disk means the runner finished; nothing to record.
    assert sup.classify(alive=True, log_age=10, stall_sec=420, run_end_seen=True) == "clean_exit"
    assert (
        sup.classify(alive=False, log_age=9999, stall_sec=420, run_end_seen=True) == "clean_exit"
    )


def test_classify_reaped_when_dead_and_no_terminal(sup):
    assert sup.classify(alive=False, log_age=5, stall_sec=420, run_end_seen=False) == "reaped"


def test_classify_stalled_when_alive_but_log_frozen(sup):
    assert sup.classify(alive=True, log_age=500, stall_sec=420, run_end_seen=False) == "stalled"


def test_classify_continue_when_healthy(sup):
    assert sup.classify(alive=True, log_age=30, stall_sec=420, run_end_seen=False) == "continue"


def test_classify_continue_when_log_age_unknown(sup):
    # No log yet (age None) must not false-trip a stall before the first write.
    assert sup.classify(alive=True, log_age=None, stall_sec=420, run_end_seen=False) == "continue"


# --- liveness -------------------------------------------------------------- #
def test_pid_alive_true_for_self(sup):
    assert sup.pid_alive(os.getpid()) is True


def test_pid_alive_false_for_unused_pid(sup):
    # A very high pid is essentially never live on a desktop.
    assert sup.pid_alive(4_000_000_000) is False


# --- log progress parsing -------------------------------------------------- #
def test_read_log_counts_score_events_and_last_line(sup, tmp_path):
    log = tmp_path / "job.log"
    log.write_text(
        "10:00 start\n"
        "10:01 call_model purpose=score_job job_id=a\n"
        "10:02 call_model purpose=score_job job_id=b\n"
        "10:03 done\n",
        encoding="utf-8",
    )
    age, progress = sup.read_log(str(log))
    assert progress["score_job"] == 2
    assert progress["last_line"].endswith("done")
    assert age is not None and age >= 0


def test_read_log_missing_file_returns_none_age(sup, tmp_path):
    age, progress = sup.read_log(str(tmp_path / "nope.log"))
    assert age is None
    assert progress["log_age_s"] is None
