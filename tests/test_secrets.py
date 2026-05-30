"""Regression tests for job_finder/secrets.py precedence stack.

Every test runs against the autouse `isolated_keyring` fixture from
conftest.py, which installs an in-memory backend so writes never reach
the host OS keyring. The fixture also resets the module-level
deprecation-warning memo and the _KEYRING_UNAVAILABLE flag.
"""

import pytest

# ---------------------------------------------------------------------------
# Precedence stack
# ---------------------------------------------------------------------------


def test_env_var_wins_over_keyring(isolated_keyring, monkeypatch):
    """Step 1 (env var) takes precedence over step 2 (keyring)."""
    from job_finder.secrets import get_secret

    isolated_keyring.set_password("job-cannon", "sources.serpapi.api_key", "from-keyring")
    monkeypatch.setenv("SERPAPI_API_KEY", "from-env")

    assert get_secret("sources.serpapi.api_key") == "from-env"


def test_keyring_wins_over_config(isolated_keyring, monkeypatch):
    """Step 2 (keyring) takes precedence over step 3 (config.yaml plaintext)."""
    from job_finder.secrets import get_secret

    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)
    isolated_keyring.set_password("job-cannon", "sources.serpapi.api_key", "from-keyring")
    cfg = {"sources": {"serpapi": {"api_key": "from-config"}}}

    assert get_secret("sources.serpapi.api_key", config=cfg) == "from-keyring"


def test_config_fallback_used_when_env_and_keyring_unset(monkeypatch):
    """Step 3 (config.yaml plaintext) is hit when steps 1 and 2 are empty."""
    from job_finder.secrets import get_secret

    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)
    cfg = {"sources": {"serpapi": {"api_key": "from-config"}}}

    assert get_secret("sources.serpapi.api_key", config=cfg) == "from-config"


def test_config_fallback_emits_deprecation_warning(monkeypatch, caplog):
    """First config-fallback hit per secret per process emits a WARNING."""
    from job_finder.secrets import get_secret

    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)
    cfg = {"sources": {"serpapi": {"api_key": "from-config"}}}

    with caplog.at_level("WARNING", logger="job_finder.secrets"):
        assert get_secret("sources.serpapi.api_key", config=cfg) == "from-config"

    assert "plaintext at rest" in caplog.text
    assert "migrate_secrets" in caplog.text


def test_config_fallback_warning_emitted_once_per_secret(monkeypatch, caplog):
    """Second config-fallback hit for the same secret is silent (memoized)."""
    from job_finder.secrets import get_secret

    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)
    cfg = {"sources": {"serpapi": {"api_key": "from-config"}}}

    with caplog.at_level("WARNING", logger="job_finder.secrets"):
        get_secret("sources.serpapi.api_key", config=cfg)
        caplog.clear()
        get_secret("sources.serpapi.api_key", config=cfg)

    assert "plaintext at rest" not in caplog.text


def test_all_steps_unset_returns_none(monkeypatch):
    """When env, keyring, and config are all empty, get_secret returns None."""
    from job_finder.secrets import get_secret

    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)
    assert get_secret("sources.serpapi.api_key") is None
    assert get_secret("sources.serpapi.api_key", config={}) is None


# ---------------------------------------------------------------------------
# Multiple-env-var fallback (anthropic has two env vars)
# ---------------------------------------------------------------------------


def test_first_env_var_wins_in_tuple(monkeypatch):
    """When SECRET_ENV_VARS lists multiple env vars, the first non-empty wins."""
    from job_finder.secrets import get_secret

    monkeypatch.setenv("ANTHROPIC_API_KEY", "primary")
    monkeypatch.setenv("JF_ANTHROPIC_API_KEY", "secondary")

    assert get_secret("providers.api_keys.anthropic") == "primary"


def test_second_env_var_used_when_first_unset(monkeypatch):
    """Second env var in the tuple is consulted when the first is missing."""
    from job_finder.secrets import get_secret

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("JF_ANTHROPIC_API_KEY", "from-jf-var")

    assert get_secret("providers.api_keys.anthropic") == "from-jf-var"


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


def test_unknown_secret_name_raises_value_error():
    """get_secret rejects names that aren't in SECRET_ENV_VARS."""
    from job_finder.secrets import get_secret

    with pytest.raises(ValueError, match="Unknown secret name"):
        get_secret("sources.bogus.fake_key")


def test_set_secret_unknown_name_raises():
    """set_secret also rejects unknown names."""
    from job_finder.secrets import set_secret

    with pytest.raises(ValueError, match="Unknown secret name"):
        set_secret("sources.bogus.fake_key", "value")


def test_keyring_unavailable_skips_step_2(monkeypatch):
    """When _KEYRING_UNAVAILABLE is set, get_secret skips step 2 cleanly."""
    from job_finder import secrets as secrets_mod

    monkeypatch.setattr(secrets_mod, "_KEYRING_UNAVAILABLE", True)
    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)
    cfg = {"sources": {"serpapi": {"api_key": "from-config"}}}

    # Step 2 is skipped — falls through to step 3 (config).
    assert secrets_mod.get_secret("sources.serpapi.api_key", config=cfg) == "from-config"


def test_set_secret_raises_when_keyring_unavailable(monkeypatch):
    """set_secret raises RuntimeError so callers can flash a UI warning."""
    from job_finder import secrets as secrets_mod

    monkeypatch.setattr(secrets_mod, "_KEYRING_UNAVAILABLE", True)

    with pytest.raises(RuntimeError, match="OS keyring is unavailable"):
        secrets_mod.set_secret("sources.serpapi.api_key", "value")


# ---------------------------------------------------------------------------
# Write / delete / list
# ---------------------------------------------------------------------------


def test_set_and_get_secret_roundtrip(monkeypatch):
    """set_secret writes to the keyring; subsequent get_secret reads it back."""
    from job_finder.secrets import get_secret, set_secret

    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)
    set_secret("sources.serpapi.api_key", "sk-stored")

    assert get_secret("sources.serpapi.api_key") == "sk-stored"


def test_delete_secret_removes_keyring_entry(monkeypatch):
    """delete_secret removes a previously written entry."""
    from job_finder.secrets import delete_secret, get_secret, set_secret

    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)
    set_secret("sources.serpapi.api_key", "sk-stored")
    delete_secret("sources.serpapi.api_key")

    assert get_secret("sources.serpapi.api_key") is None


def test_delete_secret_idempotent_on_missing_entry():
    """delete_secret does not raise when no entry exists."""
    from job_finder.secrets import delete_secret

    # Should not raise.
    delete_secret("sources.serpapi.api_key")


def test_list_secrets_returns_canonical_names_present_in_keyring(monkeypatch):
    """list_secrets enumerates canonical names with non-empty keyring values."""
    from job_finder.secrets import list_secrets, set_secret

    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)
    monkeypatch.delenv("THORDATA_API_KEY", raising=False)

    set_secret("sources.serpapi.api_key", "sk-1")
    set_secret("sources.thordata.api_key", "sk-2")

    found = set(list_secrets())
    assert "sources.serpapi.api_key" in found
    assert "sources.thordata.api_key" in found


def test_list_secrets_empty_when_keyring_unavailable(monkeypatch):
    """list_secrets returns an empty list when no backend is reachable."""
    from job_finder import secrets as secrets_mod

    monkeypatch.setattr(secrets_mod, "_KEYRING_UNAVAILABLE", True)
    assert secrets_mod.list_secrets() == []


# ---------------------------------------------------------------------------
# _walk_config edge cases
# ---------------------------------------------------------------------------


def test_walk_config_returns_none_for_missing_key():
    from job_finder.secrets import _walk_config

    assert _walk_config({"a": {}}, "a.b") is None


def test_walk_config_returns_none_for_non_dict_intermediate():
    from job_finder.secrets import _walk_config

    assert _walk_config({"a": "not-a-dict"}, "a.b") is None


def test_walk_config_returns_none_for_empty_string():
    """Empty-string values are treated as missing so the precedence stack continues."""
    from job_finder.secrets import _walk_config

    assert _walk_config({"a": {"b": ""}}, "a.b") is None


def test_walk_config_returns_non_empty_string():
    from job_finder.secrets import _walk_config

    assert _walk_config({"a": {"b": "value"}}, "a.b") == "value"


# ---------------------------------------------------------------------------
# Boot probe
# ---------------------------------------------------------------------------


def test_probe_keyring_backend_returns_true_against_in_memory_backend():
    """The autouse fixture installs an in-memory backend, so probe succeeds."""
    from job_finder.secrets import probe_keyring_backend

    assert probe_keyring_backend() is True


def test_probe_clears_unavailable_flag(monkeypatch):
    """A successful probe resets _KEYRING_UNAVAILABLE to False."""
    from job_finder import secrets as secrets_mod

    monkeypatch.setattr(secrets_mod, "_KEYRING_UNAVAILABLE", True)
    secrets_mod.probe_keyring_backend()

    assert secrets_mod._KEYRING_UNAVAILABLE is False
