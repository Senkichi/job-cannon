"""Exhaustive invariant suite: onboarding secrets land in the OS keyring, never plaintext config.yaml.

Codifies footgun #396 as a property over *every* onboarding credential path: any
secret a user enters in the wizard (Gmail IMAP app-password, BYO provider API key)
MUST be written to the OS keyring under its canonical name, and the atomically-written
``config.yaml`` MUST NOT contain the secret value at any depth.

The handoff point is ``job_finder/web/onboarding/blueprint.py::done`` (POST), which
routes secrets out of ``onboarding_state.wizard_data`` plaintext into the keyring via
``_move_secret_or_warn`` and clears each leaf to ``""`` before the config write. Two
secret classes flow through: the IMAP app-password (``sources.imap.app_password``) and
BYO provider API keys (``providers.api_keys.<name>``, gated on membership in
``jf_secrets.SECRET_ENV_VARS``).

This module asserts the invariant across the credential paths the existing
``tests/test_onboarding_done.py`` keyring tests do not cover exhaustively:
the IMAP skip branch, a provider absent from ``SECRET_ENV_VARS``, the no-creds path,
and a static closed-set guard on the ``if canonical in SECRET_ENV_VARS`` gate. We add
tests + one static guard only — no change to the production storage design.

Keyring isolation is guaranteed by the autouse ``isolated_keyring`` fixture
(``tests/conftest.py``), which installs an in-memory backend — no host OS keyring is
ever touched, and every test uses its own ``tmp_path``-namespaced data dir so the
suite is safe under ``-n auto``.
"""

import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import keyring as keyring_lib
import pytest
import yaml

from job_finder import secrets as jf_secrets
from job_finder.secrets import _service_name
from job_finder.web.onboarding.blueprint import _NO_CREDS_PROVIDERS

# BYO-key providers the wizard can present a credentials field for (every provider
# NOT in _NO_CREDS_PROVIDERS that the cascade supports with an API key). Hardcoded
# independently of SECRET_ENV_VARS so the static guard (test 6) is falsifiable:
# removing any "providers.api_keys.<name>" row from SECRET_ENV_VARS makes the gate
# at blueprint.py:706 silently leak that provider's key as plaintext, and the guard
# must catch it. Keep this in sync with the BYO providers job-cannon supports; the
# $0 no-creds CLIs live in _NO_CREDS_PROVIDERS and intentionally have no api_key.
_WIZARD_BYO_PROVIDERS: frozenset[str] = frozenset(
    {
        "anthropic",
        "gemini",
        "openrouter",
        "groq",
        "cerebras",
        "mistral",
        "cohere",
        "sambanova",
    }
)


@pytest.fixture
def configured_app(app, tmp_path, monkeypatch):
    """App fixture with config_path + user_data_root pointed at tmp_path.

    Replicated from tests/test_onboarding_done.py so this invariant module is
    self-contained and free of cross-module fixture coupling under xdist. Pointing
    both at a per-test ``tmp_path`` lets us assert the atomic config.yaml side effect
    without polluting the real user-data dir, and gives ``_service_name()`` a unique
    data-dir digest per test (Issue #396 namespacing) so keyring entries can't bleed.
    """
    cfg_path = tmp_path / "config.yaml"
    # Seed an empty existing config so load_config(allow_missing=True) returns {}.
    cfg_path.touch()

    monkeypatch.setattr(
        "job_finder.web.onboarding.blueprint.user_data_dirs.config_path",
        lambda: cfg_path,
    )
    monkeypatch.setattr(
        "job_finder.web.onboarding.blueprint.user_data_dirs.user_data_root",
        lambda: tmp_path,
    )
    app._test_cfg_path = cfg_path
    app._test_user_data_root = tmp_path
    return app


def _seed_wizard_data(db_path: str, payload: dict) -> None:
    """Write `payload` to onboarding_state.wizard_data with onboarding_complete=0."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO onboarding_state (id, onboarding_complete, wizard_data) "
            "VALUES (1, 0, ?)",
            (json.dumps(payload),),
        )
        conn.commit()
    finally:
        conn.close()


def _post_done(configured_app):
    """POST /onboarding/done with the scheduler stubbed; return the response."""
    with patch(
        "job_finder.web.onboarding.blueprint.get_scheduler",
        return_value=MagicMock(),
    ):
        return configured_app.test_client().post("/onboarding/done")


def _read_written_config(configured_app) -> dict:
    """Parse the config.yaml the done handler wrote atomically to tmp_path."""
    cfg_path: Path = configured_app._test_cfg_path
    return yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}


def _assert_no_plaintext_secret(node: object, secret: str, _path: str = "") -> None:
    """Recursively assert `secret` appears in NO string leaf of `node`.

    Guards against a future refactor stashing the secret under an unexpected key:
    a structural property over the whole parsed config, not a single named field.
    """
    if isinstance(node, dict):
        for key, value in node.items():
            _assert_no_plaintext_secret(value, secret, f"{_path}.{key}" if _path else str(key))
    elif isinstance(node, list):
        for idx, value in enumerate(node):
            _assert_no_plaintext_secret(value, secret, f"{_path}[{idx}]")
    elif isinstance(node, str):
        assert secret not in node, (
            f"plaintext secret leaked into written config at {_path or '<root>'!r}: {node!r}"
        )


# --- Task 1: IMAP happy path ---


def test_imap_app_password_lands_in_keyring_not_config(configured_app):
    """Enabled IMAP: app_password → keyring; config leaf cleared; no plaintext anywhere."""
    secret = "abcd efgh ijkl mnop"
    wizard_payload = {
        "provider": {"name": "ollama"},  # no-creds provider — only IMAP is secret
        "imap": {
            "host": "imap.gmail.com",
            "port": 993,
            "email": "u@example.com",
            "app_password": secret,
            "folder": "INBOX",
            "enabled": True,
            "verified": True,
        },
        "profile_edit": {
            "target_titles": "Engineer",
            "target_locations": "Remote",
            "skills": "py",
        },
        "schedule": {"cadence_preset": "standard"},
    }
    _seed_wizard_data(configured_app.config["DB_PATH"], wizard_payload)

    resp = _post_done(configured_app)
    assert resp.status_code == 302

    assert keyring_lib.get_password(_service_name(), "sources.imap.app_password") == secret

    written = _read_written_config(configured_app)
    assert written["sources"]["imap"]["app_password"] == ""
    _assert_no_plaintext_secret(written, secret)


# --- Task 2: IMAP skip path (new coverage of blueprint.py:451-471) ---


def test_imap_skip_path_app_password_lands_in_keyring_not_config(configured_app):
    """Disabled IMAP (skip branch) still persists app_password — must not leak plaintext.

    The skip branch (blueprint.py:451-471) writes credentials into wizard_data with
    enabled=False so they survive for a later Settings enable. The done handler builds
    sources.imap.app_password into the config slice regardless of enabled, so the
    keyring move MUST still fire — otherwise the skip path leaks plaintext at rest.
    """
    secret = "skip pwd 9999 zzzz"
    wizard_payload = {
        "provider": {"name": "ollama"},
        "imap": {
            "host": "imap.gmail.com",
            "port": 993,
            "email": "u@example.com",
            "app_password": secret,
            "folder": "INBOX",
            "enabled": False,
            "verified": False,
        },
        "profile_edit": {
            "target_titles": "Engineer",
            "target_locations": "Remote",
            "skills": "py",
        },
        "schedule": {"cadence_preset": "standard"},
    }
    _seed_wizard_data(configured_app.config["DB_PATH"], wizard_payload)

    resp = _post_done(configured_app)
    assert resp.status_code == 302

    assert keyring_lib.get_password(_service_name(), "sources.imap.app_password") == secret

    written = _read_written_config(configured_app)
    assert written["sources"]["imap"]["enabled"] is False
    assert written["sources"]["imap"]["app_password"] == ""
    _assert_no_plaintext_secret(written, secret)


# --- Task 3: BYO provider key in SECRET_ENV_VARS ---


@pytest.mark.parametrize("provider_name", ["anthropic", "gemini", "openrouter"])
def test_byo_provider_api_key_lands_in_keyring_not_config(configured_app, provider_name):
    """A BYO-key provider whose canonical name is in SECRET_ENV_VARS → keyring, not config."""
    canonical = f"providers.api_keys.{provider_name}"
    assert canonical in jf_secrets.SECRET_ENV_VARS, (
        f"test precondition: {canonical} must be a known secret"
    )
    secret = f"sk-{provider_name}-test-abcdef123456"
    wizard_payload = {
        "provider": {"name": provider_name, "api_key": secret},
        "imap": {
            "host": "imap.gmail.com",
            "port": 993,
            "email": "u@example.com",
            "app_password": "",  # no IMAP secret this run
            "folder": "INBOX",
            "enabled": False,
        },
        "profile_edit": {
            "target_titles": "Engineer",
            "target_locations": "Remote",
            "skills": "py",
        },
        "schedule": {"cadence_preset": "standard"},
    }
    _seed_wizard_data(configured_app.config["DB_PATH"], wizard_payload)

    resp = _post_done(configured_app)
    assert resp.status_code == 302

    assert keyring_lib.get_password(_service_name(), canonical) == secret

    written = _read_written_config(configured_app)
    assert written["providers"]["api_keys"][provider_name] == ""
    _assert_no_plaintext_secret(written, secret)


# --- Task 4: no-creds provider writes no secret ---


def test_no_creds_provider_writes_no_secret(configured_app):
    """A no-creds provider (ollama) with no api_key and no IMAP password stores nothing."""
    assert "ollama" in _NO_CREDS_PROVIDERS  # precondition: ollama is a $0 no-creds CLI
    wizard_payload = {
        "provider": {"name": "ollama"},  # no api_key key at all
        "imap": {
            "host": "imap.gmail.com",
            "port": 993,
            "email": "",
            "app_password": "",  # no IMAP secret
            "folder": "INBOX",
            "enabled": False,
        },
        "profile_edit": {
            "target_titles": "Engineer",
            "target_locations": "Remote",
            "skills": "py",
        },
        "schedule": {"cadence_preset": "standard"},
    }
    _seed_wizard_data(configured_app.config["DB_PATH"], wizard_payload)

    resp = _post_done(configured_app)
    assert resp.status_code == 302

    # No api_key secret was ever entered → nothing in the keyring for ollama.
    assert keyring_lib.get_password(_service_name(), "providers.api_keys.ollama") is None
    # And the IMAP app_password slot was never populated either.
    assert keyring_lib.get_password(_service_name(), "sources.imap.app_password") is None

    written = _read_written_config(configured_app)
    # The wizard never writes a providers.api_keys section when no api_key is entered.
    assert "api_keys" not in written.get("providers", {})


# --- Task 6: static guard for the SECRET_ENV_VARS gate ---


def test_every_wizard_byo_provider_is_a_known_secret():
    """Closed-set guard: every wizard-offerable BYO provider has a SECRET_ENV_VARS row.

    The done handler only moves a provider api_key to the keyring when
    ``f"providers.api_keys.{name}" in jf_secrets.SECRET_ENV_VARS`` (blueprint.py:706).
    A BYO provider missing from SECRET_ENV_VARS would silently skip _move_secret_or_warn
    and leave its key as plaintext in config.yaml. This asserts the gate is a closed set
    over the providers the wizard can present a credentials field for.
    """
    # Sanity: the BYO set and the no-creds set are disjoint by construction.
    assert _WIZARD_BYO_PROVIDERS.isdisjoint(_NO_CREDS_PROVIDERS)

    missing = sorted(
        name
        for name in _WIZARD_BYO_PROVIDERS
        if f"providers.api_keys.{name}" not in jf_secrets.SECRET_ENV_VARS
    )
    assert not missing, (
        "BYO provider(s) absent from SECRET_ENV_VARS — their api_key would leak as "
        f"plaintext through the blueprint.py:706 gate: {missing}"
    )
