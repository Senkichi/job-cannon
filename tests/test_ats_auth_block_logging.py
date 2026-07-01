"""Tests for auth/anti-bot wall logging promotion (issue #593).

Verifies that scanners emit WARNING logs for auth-block statuses (401/403/429)
instead of silent DEBUG, so a blocked board isn't mistaken for an empty one.
"""

from __future__ import annotations

import logging
from unittest.mock import Mock, patch

import pytest
from flask import Flask

from job_finder.web.ats_platforms._registry import _auth_block_statuses, _http_get_json
from job_finder.web.ats_platforms._platforms_amazon import _fetch_postings as fetch_amazon


@pytest.fixture
def app_with_config():
    """Flask app with JF_CONFIG containing default auth_block_statuses."""
    app = Flask(__name__)
    app.config["JF_CONFIG"] = {"health": {"auth_block_statuses": [401, 403, 429]}}
    return app


def test_auth_block_statuses_from_config(app_with_config):
    """Config-driven: the accessor reads from JF_CONFIG when available."""
    with app_with_config.app_context():
        assert _auth_block_statuses() == frozenset({401, 403, 429})


def test_auth_block_statuses_default_when_no_context():
    """When no app context, falls back to default {401, 403, 429}."""
    assert _auth_block_statuses() == frozenset({401, 403, 429})


def test_auth_block_statuses_custom_config():
    """Custom config override: only 429 is in the set."""
    app = Flask(__name__)
    app.config["JF_CONFIG"] = {"health": {"auth_block_statuses": [429]}}
    with app.app_context():
        assert _auth_block_statuses() == frozenset({429})


def test_http_get_json_auth_block_warns(app_with_config, caplog):
    """_http_get_json emits WARNING for 403 (auth-block status) and returns None."""
    with app_with_config.app_context():
        with caplog.at_level(logging.WARNING):
            with patch("job_finder.web.ats_platforms._registry.requests.get") as mock_get:
                mock_resp = Mock()
                mock_resp.status_code = 403
                mock_get.return_value = mock_resp
                result = _http_get_json("http://example.com", "test_label", "test_slug")
                assert result is None
                assert any("possible auth/anti-bot wall: HTTP 403" in record.message for record in caplog.records)


def test_http_get_json_502_stays_debug(app_with_config, caplog):
    """_http_get_json does NOT emit WARNING for 502 (not in auth-block set)."""
    with app_with_config.app_context():
        with caplog.at_level(logging.WARNING):
            with patch("job_finder.web.ats_platforms._registry.requests.get") as mock_get:
                mock_resp = Mock()
                mock_resp.status_code = 502
                mock_get.return_value = mock_resp
                result = _http_get_json("http://example.com", "test_label", "test_slug")
                assert result is None
                # No WARNING log for 502
                assert not any("502" in record.message for record in caplog.records if record.levelno == logging.WARNING)


def test_scanner_403_emits_warning(app_with_config, caplog):
    """A scanner receiving 403 on its first fetch emits a WARNING containing the status and slug."""
    with app_with_config.app_context():
        with caplog.at_level(logging.WARNING):
            with patch("job_finder.web.ats_platforms._platforms_amazon.requests.get") as mock_get:
                mock_resp = Mock()
                mock_resp.status_code = 403
                mock_resp.json.return_value = {"jobs": []}
                mock_get.return_value = mock_resp
                result = fetch_amazon("test-slug")
                assert result == []
                assert any("possible auth/anti-bot wall: HTTP 403" in record.message and "test-slug" in record.message for record in caplog.records)


def test_scanner_500_stays_debug(app_with_config, caplog):
    """A scanner receiving 500 does NOT emit a WARNING (only DEBUG)."""
    with app_with_config.app_context():
        with caplog.at_level(logging.WARNING):
            with patch("job_finder.web.ats_platforms._platforms_amazon.requests.get") as mock_get:
                mock_resp = Mock()
                mock_resp.status_code = 500
                mock_get.return_value = mock_resp
                result = fetch_amazon("test-slug")
                assert result == []
                # No WARNING log for 500
                assert not any("500" in record.message for record in caplog.records if record.levelno == logging.WARNING)


def test_auth_block_statuses_config_override(app_with_config, caplog):
    """Custom config: 403 stays DEBUG (not in set), 429 WARNs (in set)."""
    app_with_config.config["JF_CONFIG"]["health"]["auth_block_statuses"] = [429]
    with app_with_config.app_context():
        with caplog.at_level(logging.WARNING):
            with patch("job_finder.web.ats_platforms._registry.requests.get") as mock_get:
                # Test 403 (not in custom set)
                mock_resp = Mock()
                mock_resp.status_code = 403
                mock_get.return_value = mock_resp
                _http_get_json("http://example.com", "test_label", "test_slug")
                # No WARNING for 403
                assert not any("403" in record.message for record in caplog.records if record.levelno == logging.WARNING)

                # Test 429 (in custom set)
                mock_resp.status_code = 429
                _http_get_json("http://example.com", "test_label", "test_slug")
                # WARNING for 429
                assert any("possible auth/anti-bot wall: HTTP 429" in record.message for record in caplog.records)
