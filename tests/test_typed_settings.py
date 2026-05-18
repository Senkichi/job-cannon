"""Tests for job_finder.settings — the typed Settings dataclass skeleton.

Session 5 introduces this module without migrating any caller. The
tests here verify the construction surface that Session 8 will rely on.
File name disambiguates from tests/test_settings.py, which tests the
web/blueprints/settings.py URL-route surface (different scope).
"""

import dataclasses

import pytest

from job_finder.config import (
    DEFAULT_CANDIDATE_SCORE_THRESHOLD,
    DEFAULT_DAILY_BUDGET_USD,
    DEFAULT_DB_PATH,
    DEFAULT_LOOKBACK_DAYS,
    DEFAULT_MAX_RESULTS,
    DEFAULT_MIN_SCORE_THRESHOLD,
    DEFAULT_SERVER_DEBUG,
    DEFAULT_SERVER_HOST,
    DEFAULT_SERVER_PORT,
)
from job_finder.settings import (
    IngestionSettings,
    ScoringSettings,
    ServerSettings,
    Settings,
)


def test_default_settings_match_config_py_defaults():
    """Settings() picks up DEFAULT_* constants from job_finder.config so
    the typed view and the legacy dict view see the same baseline."""
    s = Settings()
    assert s.server.host == DEFAULT_SERVER_HOST
    assert s.server.port == DEFAULT_SERVER_PORT
    assert s.server.debug == DEFAULT_SERVER_DEBUG
    assert s.scoring.candidate_score_threshold == DEFAULT_CANDIDATE_SCORE_THRESHOLD
    assert s.scoring.daily_budget_usd == DEFAULT_DAILY_BUDGET_USD
    assert s.scoring.min_score_threshold == DEFAULT_MIN_SCORE_THRESHOLD
    assert s.ingestion.lookback_days == DEFAULT_LOOKBACK_DAYS
    assert s.ingestion.max_results == DEFAULT_MAX_RESULTS
    assert s.db_path == DEFAULT_DB_PATH


def test_from_dict_typed_round_trip():
    """from_dict -> to_dict preserves the typed fields exactly."""
    cfg = {
        "server": {"host": "0.0.0.0", "port": 8080, "debug": False},
        "scoring": {
            "candidate_score_threshold": 60,
            "daily_budget_usd": 5.5,
            "min_score_threshold": 35,
        },
        "ingestion": {"lookback_days": 14, "max_results": 100},
        "db": {"path": "/tmp/jobs.db"},
    }
    s = Settings.from_dict(cfg)
    assert s.server.host == "0.0.0.0"
    assert s.server.port == 8080
    assert s.server.debug is False
    assert s.scoring.candidate_score_threshold == 60
    assert s.scoring.daily_budget_usd == 5.5
    assert s.scoring.min_score_threshold == 35
    assert s.ingestion.lookback_days == 14
    assert s.ingestion.max_results == 100
    assert s.db_path == "/tmp/jobs.db"
    assert s.to_dict() == cfg


def test_from_dict_falls_back_to_defaults_for_missing_or_null_sections():
    """Empty / partial / None-valued sections fall through to DEFAULT_*."""
    s_empty = Settings.from_dict({})
    assert s_empty.server.host == DEFAULT_SERVER_HOST
    assert s_empty.scoring.candidate_score_threshold == DEFAULT_CANDIDATE_SCORE_THRESHOLD
    assert s_empty.db_path == DEFAULT_DB_PATH

    # Partial section: scoring exists but only sets one field; others fall back.
    s_partial = Settings.from_dict({"scoring": {"candidate_score_threshold": 99}})
    assert s_partial.scoring.candidate_score_threshold == 99
    assert s_partial.scoring.daily_budget_usd == DEFAULT_DAILY_BUDGET_USD

    # YAML-style null (None) values: cfg.get("server") returns None, not {}.
    # The from_dict implementation must treat None and missing identically.
    s_null = Settings.from_dict({"server": None, "scoring": None, "ingestion": None})
    assert s_null.server.port == DEFAULT_SERVER_PORT
    assert s_null.scoring.daily_budget_usd == DEFAULT_DAILY_BUDGET_USD
    assert s_null.ingestion.max_results == DEFAULT_MAX_RESULTS


def test_validate_rejects_invalid_port():
    """Port must be in the standard 1-65535 range."""
    s_low = Settings(server=ServerSettings(port=0))
    with pytest.raises(ValueError, match=r"server\.port"):
        s_low.validate()

    s_high = Settings(server=ServerSettings(port=70000))
    with pytest.raises(ValueError, match=r"server\.port"):
        s_high.validate()


def test_validate_rejects_out_of_contract_scoring_fields():
    """Negative budget, threshold > 100, etc. are caught at validate()."""
    s_neg_budget = Settings(scoring=ScoringSettings(daily_budget_usd=-1.0))
    with pytest.raises(ValueError, match="daily_budget_usd"):
        s_neg_budget.validate()

    s_high_threshold = Settings(scoring=ScoringSettings(candidate_score_threshold=150))
    with pytest.raises(ValueError, match="candidate_score_threshold"):
        s_high_threshold.validate()

    s_neg_lookback = Settings(ingestion=IngestionSettings(lookback_days=-1))
    with pytest.raises(ValueError, match="lookback_days"):
        s_neg_lookback.validate()


def test_settings_and_substructures_are_frozen():
    """frozen=True propagates — mutation must raise FrozenInstanceError."""
    s = Settings()
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.db_path = "/other/path.db"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.server = ServerSettings(host="evil")  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.server.port = 9999  # type: ignore[misc]


def test_raw_proxy_rejects_mutation():
    """raw is a MappingProxyType view — item assignment raises TypeError."""
    s = Settings.from_dict({"profile": {"target_titles": ["staff"]}})
    with pytest.raises(TypeError):
        s.raw["new_key"] = "value"  # type: ignore[index]
    with pytest.raises(TypeError):
        del s.raw["profile"]  # type: ignore[attr-defined]


def test_raw_proxy_is_defensive_copy_at_top_level():
    """Mutating the original cfg after from_dict() must not affect s.raw,
    which is what the dict(cfg) defensive copy in from_dict buys."""
    from typing import Any

    cfg: dict[str, Any] = {"profile": {"target_titles": ["staff"]}}
    s = Settings.from_dict(cfg)
    cfg["new_top_level"] = "added_after_construction"
    # The proxy still sees the snapshot taken at construction time.
    assert "new_top_level" not in s.raw
    # Top-level keys are isolated via the dict() copy. Nested mutable
    # objects (lists, dicts) ARE shared — documented limitation; deep
    # copy would be a perf hit and Session 8 caller migration removes
    # the need for raw entirely.


def test_loads_legacy_tier_keys(tmp_path):
    """Legacy providers.* / scoring.haiku_threshold keys migrate on disk (ruamel)."""
    cfg_path = tmp_path / "legacy.yaml"
    cfg_path.write_text(
        "# tier rename migration comment\n"
        "profile:\n"
        "  target_titles: [staff]  # required by validate_target_titles\n"
        "sources: {}\n"
        "providers:\n"
        "  haiku:  # inline\n"
        "    provider: anthropic\n"
        "    model: claude-haiku-4-5\n"
        "scoring:\n"
        "  haiku_threshold: 77\n"
        "  models:\n"
        "    haiku: claude-haiku-4-5\n"
        "    sonnet: claude-sonnet-4-6\n"
        "db:\n"
        "  path: jobs.db\n",
        encoding="utf-8",
    )
    s = Settings.load_from_yaml(str(cfg_path))
    assert s.scoring.candidate_score_threshold == 77
    assert s.raw["providers"]["low"]["model"] == "claude-haiku-4-5"
    assert s.raw["scoring"]["models"]["low"] == "claude-haiku-4-5"
    text = cfg_path.read_text(encoding="utf-8")
    assert "candidate_score_threshold" in text
    assert "haiku_threshold" not in text
    assert "tier rename migration comment" in text
