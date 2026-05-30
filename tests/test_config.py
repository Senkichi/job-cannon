"""Tests for config.py functions."""

import pytest

from job_finder.config import ConfigError, load_config, resolve_triage_enabled


def test_resolve_triage_enabled_auto_true_for_paid_primaries():
    cfg = {"providers": {"primary": "claude_code_cli", "triage": {"enabled": "auto"}}}
    assert resolve_triage_enabled(cfg) is True

    cfg["providers"]["primary"] = "gemini"
    assert resolve_triage_enabled(cfg) is True

    cfg["providers"]["primary"] = "anthropic"
    assert resolve_triage_enabled(cfg) is True


def test_resolve_triage_enabled_auto_false_for_local_primaries():
    cfg = {"providers": {"primary": "ollama", "triage": {"enabled": "auto"}}}
    assert resolve_triage_enabled(cfg) is False

    cfg["providers"]["primary"] = "local_bundled"
    assert resolve_triage_enabled(cfg) is False


def test_resolve_triage_enabled_explicit_true():
    cfg = {"providers": {"primary": "ollama", "triage": {"enabled": True}}}
    assert resolve_triage_enabled(cfg) is True


def test_resolve_triage_enabled_explicit_false():
    cfg = {"providers": {"primary": "claude_code_cli", "triage": {"enabled": False}}}
    assert resolve_triage_enabled(cfg) is False


def test_old_providers_scoring_schema_raises_error():
    from job_finder.config import validate_required_sections

    cfg = {
        "providers": {"scoring": {"primary": "ollama"}},
        "profile": {},
        "sources": {},
        "scoring": {},
        "db": {},
    }
    with pytest.raises(ConfigError, match="Old config schema detected"):
        validate_required_sections(cfg)


def test_load_config_validates_populated_config_even_with_allow_missing(tmp_path):
    """Pins Fix 1 (2026-05-17 hotfix): allow_missing=True must only suppress
    the "file is missing" error, not skip schema validation on a populated
    config. Before the fix, a populated old-shape config loaded silently and
    the broken cascade went undetected for ~24h.
    """
    import yaml

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "profile": {},
                "sources": {},
                "scoring": {},
                "db": {},
                # The Phase 40 trap: nested providers.scoring shape.
                "providers": {"scoring": {"provider": "ollama"}},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="Old config schema detected"):
        load_config(cfg_path, allow_missing=True)


def test_load_config_returns_empty_when_file_missing_and_allow_missing(tmp_path):
    """Counterpart to the above: the file-not-found case must still return
    {} silently when allow_missing=True. This is the onboarding wizard's
    legitimate use case — preserved by Fix 1's split.
    """
    missing = tmp_path / "does_not_exist.yaml"
    assert load_config(missing, allow_missing=True) == {}
