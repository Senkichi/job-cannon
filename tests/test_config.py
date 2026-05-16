"""Tests for config.py functions."""
import pytest
from job_finder.config import ConfigError, ConfigNotFoundError, resolve_triage_enabled, load_config


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
    cfg = {"providers": {"scoring": {"primary": "ollama"}}, "profile": {}, "sources": {}, "scoring": {}, "db": {}}
    with pytest.raises(ConfigError, match="Old config schema detected"):
        validate_required_sections(cfg)
