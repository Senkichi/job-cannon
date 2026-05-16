"""Unit tests for job_finder.web.providers.claude_code_cli.

Tests cover:
- Subclass relationship with BaseProvider
- RuntimeError when claude binary is not on PATH
- call() signature consistency with BaseProvider.call
- ModelResult shape: 7 fields with correct types
- output_schema=None freeform path -> {"text": ...} when result is plain text
- output_schema=None JSON path -> parsed dict when result is JSON
- output_schema dict path -> structured_output preferred over result parse
- output_schema dict path with result-fallback parse
- Empty messages list -> ValueError
- Missing usage block -> 0 tokens
- _run_oneshot RuntimeError propagates through call()
- No subprocess.run in this module (delegation invariant)
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from job_finder.web.model_provider import BaseProvider, ModelResult
from job_finder.web.providers.claude_code_cli import ClaudeCodeCLIProvider

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_provider() -> ClaudeCodeCLIProvider:
    """Construct with claude binary mocked-present."""
    with patch("job_finder.web.providers.claude_code_cli.shutil.which", return_value="/usr/bin/claude"):
        return ClaudeCodeCLIProvider(config={})


def _envelope(
    result: str = "pong",
    structured_output: dict | None = None,
    input_tokens: int = 3,
    output_tokens: int = 5,
    is_error: bool = False,
) -> dict:
    env: dict = {
        "type": "result",
        "subtype": "success",
        "is_error": is_error,
        "result": result,
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
        "total_cost_usd": 0.0,
    }
    if structured_output is not None:
        env["structured_output"] = structured_output
    return env


# ---------------------------------------------------------------------------
# Subclass / init tests
# ---------------------------------------------------------------------------


def test_claude_code_cli_provider_is_base_provider_subclass():
    assert issubclass(ClaudeCodeCLIProvider, BaseProvider)


def test_init_raises_runtime_error_when_claude_not_on_path():
    with patch("job_finder.web.providers.claude_code_cli.shutil.which", return_value=None):
        with pytest.raises(RuntimeError, match="claude CLI not found"):
            ClaudeCodeCLIProvider(config={})


def test_init_succeeds_when_claude_on_path():
    provider = _make_provider()
    assert provider._bin == "/usr/bin/claude"


# ---------------------------------------------------------------------------
# ModelResult shape tests
# ---------------------------------------------------------------------------


def test_call_returns_seven_field_model_result_with_correct_types():
    provider = _make_provider()
    with patch(
        "job_finder.web.providers.claude_code_cli._run_oneshot",
        return_value=_envelope(result="hello world"),
    ):
        result = provider.call(
            model="claude-haiku-4-5",
            system="be brief",
            messages=[{"role": "user", "content": "hi"}],
        )
    assert isinstance(result, ModelResult)
    assert isinstance(result.data, dict)
    assert isinstance(result.cost_usd, float)
    assert isinstance(result.input_tokens, int)
    assert isinstance(result.output_tokens, int)
    assert isinstance(result.model, str)
    assert isinstance(result.provider, str)
    assert isinstance(result.schema_valid, bool)


def test_call_returns_zero_cost_and_claude_code_cli_provider():
    provider = _make_provider()
    with patch(
        "job_finder.web.providers.claude_code_cli._run_oneshot",
        return_value=_envelope(result="ok"),
    ):
        result = provider.call(
            model="claude-haiku-4-5",
            system="s",
            messages=[{"role": "user", "content": "u"}],
        )
    assert result.cost_usd == 0.0
    assert result.provider == "claude_code_cli"
    assert result.model == "claude-haiku-4-5"


# ---------------------------------------------------------------------------
# Freeform vs schema parse-path tests
# ---------------------------------------------------------------------------


def test_freeform_plain_text_returns_text_dict_with_schema_valid_false():
    provider = _make_provider()
    with patch(
        "job_finder.web.providers.claude_code_cli._run_oneshot",
        return_value=_envelope(result="just some plain text"),
    ):
        result = provider.call("m", "s", [{"role": "user", "content": "u"}])
    assert result.data == {"text": "just some plain text"}
    assert result.schema_valid is False


def test_freeform_with_json_string_result_returns_parsed_dict():
    provider = _make_provider()
    with patch(
        "job_finder.web.providers.claude_code_cli._run_oneshot",
        return_value=_envelope(result='{"key": "value"}'),
    ):
        result = provider.call("m", "s", [{"role": "user", "content": "u"}])
    assert result.data == {"key": "value"}
    # Still False because caller did not request schema enforcement
    assert result.schema_valid is False


def test_schema_prefers_structured_output_when_present():
    provider = _make_provider()
    schema = {"type": "object", "properties": {"x": {"type": "integer"}}}
    struct = {"x": 42}
    with patch(
        "job_finder.web.providers.claude_code_cli._run_oneshot",
        return_value=_envelope(result="ignored", structured_output=struct),
    ):
        result = provider.call(
            "m", "s", [{"role": "user", "content": "u"}], output_schema=schema
        )
    assert result.data == {"x": 42}
    assert result.schema_valid is True


def test_schema_falls_back_to_result_parse_when_no_structured_output():
    provider = _make_provider()
    schema = {"type": "object", "properties": {"x": {"type": "integer"}}}
    with patch(
        "job_finder.web.providers.claude_code_cli._run_oneshot",
        return_value=_envelope(result='{"x": 7}'),
    ):
        result = provider.call(
            "m", "s", [{"role": "user", "content": "u"}], output_schema=schema
        )
    assert result.data == {"x": 7}
    assert result.schema_valid is True


# ---------------------------------------------------------------------------
# Error path tests
# ---------------------------------------------------------------------------


def test_run_oneshot_runtime_error_propagates_through_call():
    provider = _make_provider()
    with patch(
        "job_finder.web.providers.claude_code_cli._run_oneshot",
        side_effect=RuntimeError("Claude CLI failed (rc=1): boom"),
    ):
        with pytest.raises(RuntimeError, match="Claude CLI failed"):
            provider.call("m", "s", [{"role": "user", "content": "u"}])


def test_empty_messages_raises_value_error():
    provider = _make_provider()
    with pytest.raises(ValueError, match="at least one message"):
        provider.call("m", "s", [])


def test_missing_usage_block_defaults_to_zero_tokens():
    provider = _make_provider()
    env = _envelope(result="ok")
    del env["usage"]
    with patch(
        "job_finder.web.providers.claude_code_cli._run_oneshot",
        return_value=env,
    ):
        result = provider.call("m", "s", [{"role": "user", "content": "u"}])
    assert result.input_tokens == 0
    assert result.output_tokens == 0


# ---------------------------------------------------------------------------
# Delegation invariant: no direct subprocess in this module
# ---------------------------------------------------------------------------


def test_module_does_not_call_subprocess_directly():
    """Per RESEARCH.md §4 R-04: claude_code_cli.py delegates to _run_oneshot()
    rather than re-implementing subprocess. Enforce by source-grep."""
    import pathlib

    src = pathlib.Path("job_finder/web/providers/claude_code_cli.py").read_text()
    assert "subprocess.run" not in src, "claude_code_cli.py should delegate via _run_oneshot, not call subprocess directly"
    assert "shell=True" not in src
    assert "--bare" not in src
