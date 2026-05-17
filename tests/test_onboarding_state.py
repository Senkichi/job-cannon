"""Unit tests for job_finder.web.onboarding.state (Phase 42, STRANGE-WIZ-01)."""

import json
import sqlite3

import pytest

from job_finder.web.db_migrate import run_migrations
from job_finder.web.onboarding.state import (
    _deep_merge,
    is_onboarding_complete,
    mark_onboarding_complete,
    read_wizard_data,
    write_wizard_data,
)


@pytest.fixture
def migrated_conn(tmp_db_path):
    """Fresh DB with all migrations applied; raw sqlite3.Connection (no Flask)."""
    run_migrations(tmp_db_path)
    conn = sqlite3.connect(tmp_db_path)
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


def test_read_wizard_data_returns_empty_dict_on_fresh_row(migrated_conn):
    """Default wizard_data='{}' from Migration 54 round-trips to an empty dict."""
    data = read_wizard_data(migrated_conn)
    assert data == {}


def test_write_wizard_data_deep_merges_into_existing(migrated_conn):
    """Writing a slice deep-merges into the existing dict (D-14: re-submit overwrites the slice)."""
    write_wizard_data(migrated_conn, {"provider": {"name": "ollama", "model": "qwen2.5:14b"}})
    write_wizard_data(migrated_conn, {"provider": {"model": "qwen2.5:32b"}, "imap": {"host": "imap.gmail.com"}})

    data = read_wizard_data(migrated_conn)
    assert data["provider"]["name"] == "ollama"  # preserved
    assert data["provider"]["model"] == "qwen2.5:32b"  # overwritten
    assert data["imap"]["host"] == "imap.gmail.com"  # new branch added


def test_write_wizard_data_persists_across_reads(migrated_conn):
    """JSON round-trip is lossless for nested dicts + lists + scalars."""
    payload = {
        "resume_profile": {
            "positions": [{"title": "Staff Engineer", "company": "Acme"}],
            "skills": ["python", "sqlite"],
        },
        "schedule": {"cadence_preset": "standard"},
    }
    write_wizard_data(migrated_conn, payload)
    data = read_wizard_data(migrated_conn)
    assert data == payload


def test_is_onboarding_complete_false_by_default(migrated_conn):
    """After migrations alone (no fixture seed), onboarding_complete is 0/False."""
    # Insert the singleton row explicitly so we test the explicit-false path
    migrated_conn.execute(
        "INSERT OR REPLACE INTO onboarding_state (id, onboarding_complete) VALUES (1, 0)"
    )
    migrated_conn.commit()
    assert is_onboarding_complete(migrated_conn) is False


def test_mark_onboarding_complete_sets_flag_and_clears_wizard_data(migrated_conn):
    """D-16: mark_onboarding_complete sets onboarding_complete=1 AND wizard_data='{}'."""
    write_wizard_data(migrated_conn, {"scratch": "data"})
    mark_onboarding_complete(migrated_conn)

    assert is_onboarding_complete(migrated_conn) is True
    assert read_wizard_data(migrated_conn) == {}


def test_deep_merge_immutability(migrated_conn):
    """_deep_merge must return a new dict, never mutate the base (Python immutability rule)."""
    base = {"a": 1, "b": {"c": 2}}
    overrides = {"b": {"d": 3}}
    result = _deep_merge(base, overrides)
    assert base == {"a": 1, "b": {"c": 2}}  # unchanged
    assert result == {"a": 1, "b": {"c": 2, "d": 3}}


def test_malformed_wizard_data_resets_to_empty(migrated_conn):
    """If wizard_data is corrupted JSON (e.g. truncated), read_wizard_data must return {} not raise."""
    migrated_conn.execute("INSERT OR REPLACE INTO onboarding_state (id, onboarding_complete, wizard_data) VALUES (1, 0, 'not valid json')")
    migrated_conn.commit()
    assert read_wizard_data(migrated_conn) == {}


def test_write_config_atomic_temp_rename(tmp_path):
    """_write_config must (a) write content via temp+rename, (b) derive `.tmp` from the
    target's suffix (no hardcoded `.yaml.tmp` when target is e.g. `.json`), and
    (c) leave the target untouched if the serializer crashes mid-write.

    Covers Warning 3 (suffix generalization) + Warning 4 (the helper was DOA - first
    production caller is Plan 42-06; this test exercises it in Wave 1).
    """
    import builtins

    import yaml

    from job_finder.web.onboarding.state import _write_config

    # --- (a) happy path: file gets written with expected YAML content ---
    target = tmp_path / "config.yaml"
    _write_config({"providers": {"primary": "ollama"}}, target)
    assert target.exists()
    loaded = yaml.safe_load(target.read_text(encoding="utf-8"))
    assert loaded == {"providers": {"primary": "ollama"}}

    # --- (b) no .tmp artifact persists after success ---
    leftover = list(tmp_path.glob("*.tmp"))
    assert leftover == [], f"leftover temp files after successful write: {leftover}"

    # --- (b) suffix generalization: target=`.json` -> `.json.tmp`, not `.yaml.tmp` ---
    # Spy on builtins.open to observe which temp path is opened for write.
    json_target = tmp_path / "out.json"
    observed_temp_paths: list[str] = []
    real_open = builtins.open

    def spy_open(p, *a, **kw):
        observed_temp_paths.append(str(p))
        return real_open(p, *a, **kw)

    builtins.open = spy_open
    try:
        _write_config({"x": 1}, json_target)
    finally:
        builtins.open = real_open

    # The observed temp path should end in `.json.tmp`, NOT `.yaml.tmp`.
    json_tmps = [p for p in observed_temp_paths if p.endswith(".json.tmp")]
    yaml_tmps = [p for p in observed_temp_paths if p.endswith(".yaml.tmp") and "out.json" in p]
    assert json_tmps, f"expected .json.tmp temp path; observed: {observed_temp_paths}"
    assert not yaml_tmps, f"hardcoded .yaml.tmp suffix on .json target: {yaml_tmps}"

    # --- (c) crash mid-write leaves target untouched ---
    crash_target = tmp_path / "crash.yaml"
    crash_target.write_text("original: content\n", encoding="utf-8")
    original_content = crash_target.read_text(encoding="utf-8")

    def raise_oserror(*a, **kw):
        raise OSError("simulated disk full")

    # Patch yaml.dump on the module reference used by state.py
    import job_finder.web.onboarding.state as state_mod
    original_yaml_dump = state_mod.yaml.dump
    state_mod.yaml.dump = raise_oserror
    try:
        with pytest.raises(OSError):
            _write_config({"new": "data"}, crash_target)
    finally:
        state_mod.yaml.dump = original_yaml_dump

    # Target untouched
    assert crash_target.read_text(encoding="utf-8") == original_content
    # No .tmp debris
    crash_tmps = list(tmp_path.glob("crash.yaml.tmp"))
    assert crash_tmps == [], f"crash-cleanup failed: {crash_tmps}"
