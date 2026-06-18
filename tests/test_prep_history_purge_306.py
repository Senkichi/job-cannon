"""Tests for scripts/prep_history_purge_306.py — the read-only #306 purge prep.

These tests pin the two things that, if wrong, would cause real damage when a
human follows the generated runbook:

  1. the strip-command targets ``round_0/jd/`` exactly and never the bare
     ``round_0/`` (which would also delete the intentionally-retained
     ``dedup_keys.json``), and
  2. the script issues no destructive command — no ``filter-repo
     --invert-paths``, no ``push --force``, no branch-protection mutation.

Subprocess is mocked throughout so the suite is hermetic and does not depend on
``git filter-repo`` being installed on the CI runner.
"""

from __future__ import annotations

import subprocess

from scripts import prep_history_purge_306 as prep


# --- Fake git plumbing ------------------------------------------------------
def _fake_added_paths() -> list[str]:
    """Synthetic ``git log --diff-filter=A`` output clearing every floor."""
    planning = [f".planning/note_{i:03d}.md" for i in range(181)]
    docs = ["PLAN.md", "FOLLOWUPS.md", "JD-LAYER2-PLAN.md"]
    jd = [f"evals/cascade_audit/artifacts/round_0/jd/job_{i:02d}.txt" for i in range(24)]
    # The retained sibling is added too — it must never be counted as jd/.
    keep = ["evals/cascade_audit/artifacts/round_0/dedup_keys.json"]
    return planning + docs + jd + keep


def _make_recorder(recorded: list[list[str]]):
    """Build a _run replacement that records calls and returns canned output."""

    def _fake_run(args):
        args = list(args)
        recorded.append(args)
        stdout = ""
        if "log" in args and "--diff-filter=A" in args:
            stdout = "\n".join(_fake_added_paths())
        return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")

    return _fake_run


# --- Strip-target guards (the load-bearing scope guard) ---------------------
def test_strip_paths_include_jd_subdir():
    assert "evals/cascade_audit/artifacts/round_0/jd/" in prep.STRIP_PATHS


def test_strip_paths_exclude_bare_round_0():
    # Exact membership — guards against nuking dedup_keys.json.
    assert "evals/cascade_audit/artifacts/round_0/" not in prep.STRIP_PATHS
    assert "evals/cascade_audit/artifacts/round_0" not in prep.STRIP_PATHS


def test_dedup_keys_never_in_strip_list():
    assert prep.RETAINED_PATH not in prep.STRIP_PATHS
    # No strip prefix may be a parent of the retained file.
    for p in prep.STRIP_PATHS:
        assert not prep.RETAINED_PATH.startswith(p), f"{p} would strip {prep.RETAINED_PATH}"


def test_generated_command_includes_jd_path_excludes_bare_round_0():
    cmd = prep.build_filter_repo_command()
    assert "--path evals/cascade_audit/artifacts/round_0/jd/" in cmd
    # The bare round_0 path must not appear as its own --path token.
    assert "--path evals/cascade_audit/artifacts/round_0 " not in cmd
    assert not cmd.endswith("--path evals/cascade_audit/artifacts/round_0")
    assert prep.RETAINED_PATH not in cmd


def test_generated_command_strips_all_targets():
    cmd = prep.build_filter_repo_command()
    assert cmd.startswith("git filter-repo --invert-paths")
    for p in prep.STRIP_PATHS:
        assert f"--path {p}" in cmd


# --- Non-destructiveness ----------------------------------------------------
def test_no_destructive_command_is_ever_issued(monkeypatch):
    recorded: list[list[str]] = []
    monkeypatch.setattr(prep, "_run", _make_recorder(recorded))

    prep.verify_preconditions()
    prep.build_inventory()
    prep.print_runbook(prep.build_inventory())

    assert recorded, "expected the read-only functions to issue git commands"
    for args in recorded:
        joined = " ".join(args)
        assert "--invert-paths" not in joined, f"destructive rewrite issued: {joined}"
        assert not ("push" in args and "--force" in joined), f"force-push issued: {joined}"
        assert "gh" not in args, f"branch-protection / API mutation issued: {joined}"
        assert "push" not in args, f"push issued: {joined}"


def test_print_runbook_issues_no_commands(monkeypatch):
    recorded: list[list[str]] = []
    monkeypatch.setattr(prep, "_run", _make_recorder(recorded))
    inv = prep.build_inventory()
    recorded.clear()
    prep.print_runbook(inv)
    # Rendering is pure string assembly — it must issue zero commands.
    assert recorded == []


# --- Structured, non-mutating results ---------------------------------------
def test_verify_preconditions_returns_fresh_object(monkeypatch):
    recorded: list[list[str]] = []
    monkeypatch.setattr(prep, "_run", _make_recorder(recorded))
    first = prep.verify_preconditions()
    second = prep.verify_preconditions()
    assert first is not second
    assert isinstance(first, prep.PreconditionResult)
    assert first == second  # same inputs → equal value, distinct identity


def test_precondition_result_is_immutable(monkeypatch):
    monkeypatch.setattr(prep, "_run", _make_recorder([]))
    result = prep.verify_preconditions()
    import dataclasses

    try:
        result.checks = ()  # type: ignore[misc]
    except dataclasses.FrozenInstanceError:
        pass
    else:
        raise AssertionError("PreconditionResult should be frozen")


def test_preconditions_all_pass_under_fake_git(monkeypatch):
    recorded: list[list[str]] = []
    monkeypatch.setattr(prep, "_run", _make_recorder(recorded))
    result = prep.verify_preconditions()
    assert result.ok
    assert [c.name for c in result.checks] == [
        "git filter-repo installed",
        ".planning/ contents gitignored",
        "root planning docs untracked",
    ]


# --- Inventory floors -------------------------------------------------------
def test_inventory_counts_and_floors(monkeypatch):
    monkeypatch.setattr(prep, "_run", _make_recorder([]))
    inv = prep.build_inventory()
    by_label = {c.label: c for c in inv.counts}
    assert by_label[".planning/"].count == 181
    assert by_label["root planning docs"].count == 3
    assert by_label["evals/.../round_0/jd/"].count == 24
    # The retained dedup_keys.json sits under round_0/ but NOT round_0/jd/.
    assert by_label["evals/.../round_0/jd/"].count == 24
    assert inv.ok


def test_main_exits_clean_under_fake_git(monkeypatch):
    monkeypatch.setattr(prep, "_run", _make_recorder([]))
    assert prep.main() == 0


def test_main_aborts_when_inventory_collapses(monkeypatch):
    def _empty_run(args):
        args = list(args)
        # Preconditions pass (returncode 0, ls-files empty), but git log yields
        # nothing → every floor is breached.
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(prep, "_run", _empty_run)
    assert prep.main() == 1


# --- Verification grep ------------------------------------------------------
def test_verification_grep_targets_jd_and_root_docs():
    grep = prep.VERIFICATION_GREP
    assert "round_0/jd/" in grep
    assert "\\.planning/" in grep
    assert "PLAN" in grep and "FOLLOWUPS" in grep and "JD-LAYER2-PLAN" in grep


def test_runbook_text_contains_grep_and_human_warning(monkeypatch):
    monkeypatch.setattr(prep, "_run", _make_recorder([]))
    text = prep.print_runbook(prep.build_inventory())
    assert prep.VERIFICATION_GREP in text
    assert "HUMAN" in text
    assert prep.RETAINED_PATH in text  # retained file is called out
