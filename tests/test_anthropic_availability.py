"""Regression tests for ``is_anthropic_available()``.

The predicate replaces the prior pattern of constructing
``anthropic.Anthropic()`` purely as an availability gate (Phase M-2 removed
the SDK from the dispatch path; everything now routes through ``claude -p``).
The CLI honors ``ANTHROPIC_API_KEY`` and the project-namespaced
``JF_ANTHROPIC_API_KEY`` env vars — these tests pin both names.
"""

from __future__ import annotations

import os
from unittest.mock import patch

from job_finder.web.claude_client import is_anthropic_available


def test_is_anthropic_available_with_anthropic_api_key():
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-test"}, clear=False):
        assert is_anthropic_available() is True


def test_is_anthropic_available_with_jf_namespaced_key():
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    env["JF_ANTHROPIC_API_KEY"] = "sk-ant-test"
    with patch.dict(os.environ, env, clear=True):
        assert is_anthropic_available() is True


def test_is_anthropic_available_unset():
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("ANTHROPIC_API_KEY", "JF_ANTHROPIC_API_KEY")
    }
    with patch.dict(os.environ, env, clear=True):
        assert is_anthropic_available() is False


def test_is_anthropic_available_empty_string_is_false():
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("ANTHROPIC_API_KEY", "JF_ANTHROPIC_API_KEY")
    }
    env["ANTHROPIC_API_KEY"] = ""
    with patch.dict(os.environ, env, clear=True):
        assert is_anthropic_available() is False
