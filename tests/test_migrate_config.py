"""Tests for job_finder.migrate_config (2026-05-17 hotfix Fix 2).

The migrator translates the pre-Phase-40 nested providers.scoring schema
to the Phase 40 flat providers.{primary, fallback_chain, overrides} shape.
"""

import yaml

from job_finder.migrate_config import migrate_file, ALREADY_MIGRATED, MIGRATED, UNKNOWN_SHAPE


def _write_yaml(path, data):
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


def test_migrates_old_shape_to_flat_with_overrides(tmp_path):
    """Translate providers.scoring → providers.primary + fallback_chain.
    Per-entry model: values are preserved as providers.overrides entries
    (decision recorded in PLAN.md).
    """
    cfg_path = tmp_path / "config.yaml"
    _write_yaml(
        cfg_path,
        {
            "profile": {"target_titles": ["X"]},
            "sources": {"gmail": {"enabled": True}},
            "scoring": {"weights": {"title_match": 0.3}},
            "db": {"path": "jobs.db"},
            "providers": {
                "scoring": {
                    "provider": "ollama",
                    "model": "qwen2.5:14b",
                    "fallback_chain": [
                        {"provider": "anthropic", "model": "claude-sonnet-4-6"},
                        {"provider": "gemini", "model": "gemini-2.0-flash"},
                    ],
                },
                "daily_limits": {"gemini": 1000},
                "throttle_delays": {"anthropic": 2},
            },
        },
    )

    status = migrate_file(cfg_path)
    assert status == MIGRATED

    new_cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    providers = new_cfg["providers"]
    assert providers["primary"] == "ollama"
    assert providers["fallback_chain"] == ["anthropic", "gemini"]
    # Per-entry models preserved as overrides on the score workload.
    assert providers["overrides"]["ollama"]["score"] == "qwen2.5:14b"
    assert providers["overrides"]["anthropic"]["score"] == "claude-sonnet-4-6"
    assert providers["overrides"]["gemini"]["score"] == "gemini-2.0-flash"
    # Sibling keys preserved verbatim.
    assert providers["daily_limits"] == {"gemini": 1000}
    assert providers["throttle_delays"] == {"anthropic": 2}
    # Non-providers top-level keys untouched.
    assert new_cfg["profile"] == {"target_titles": ["X"]}
    assert new_cfg["sources"] == {"gmail": {"enabled": True}}
    assert new_cfg["scoring"] == {"weights": {"title_match": 0.3}}
    assert new_cfg["db"] == {"path": "jobs.db"}

    # Backup created in same directory.
    backups = list(tmp_path.glob("config.yaml.bak.*"))
    assert len(backups) == 1
    # Backup contains the original old-shape.
    bak_cfg = yaml.safe_load(backups[0].read_text(encoding="utf-8"))
    assert "scoring" in bak_cfg["providers"]


def test_already_migrated_is_noop(tmp_path):
    """If providers.primary already exists, exit without writing anything."""
    cfg_path = tmp_path / "config.yaml"
    _write_yaml(
        cfg_path,
        {
            "profile": {},
            "providers": {
                "primary": "ollama",
                "fallback_chain": ["anthropic"],
            },
        },
    )
    original = cfg_path.read_text(encoding="utf-8")
    mtime_before = cfg_path.stat().st_mtime_ns

    status = migrate_file(cfg_path)
    assert status == ALREADY_MIGRATED
    # File unchanged.
    assert cfg_path.read_text(encoding="utf-8") == original
    assert cfg_path.stat().st_mtime_ns == mtime_before
    # No backup created.
    assert list(tmp_path.glob("config.yaml.bak.*")) == []


def test_unknown_shape_returns_status(tmp_path):
    """Neither providers.primary nor providers.scoring → cannot auto-migrate."""
    cfg_path = tmp_path / "config.yaml"
    _write_yaml(cfg_path, {"profile": {}, "providers": {}})
    status = migrate_file(cfg_path)
    assert status == UNKNOWN_SHAPE
    # No backup created.
    assert list(tmp_path.glob("config.yaml.bak.*")) == []


def test_missing_providers_section_returns_unknown(tmp_path):
    """A config without any providers section can't be migrated."""
    cfg_path = tmp_path / "config.yaml"
    _write_yaml(cfg_path, {"profile": {}, "scoring": {}})
    status = migrate_file(cfg_path)
    assert status == UNKNOWN_SHAPE
