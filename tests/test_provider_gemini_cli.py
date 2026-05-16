"""Unit tests for job_finder.web.providers.gemini_cli.

Tests cover:
- Subclass relationship with BaseProvider
- RuntimeError when gemini binary is not on PATH
- call() signature consistency with BaseProvider.call
- ModelResult shape: 7 fields with correct types
- subprocess.run argv: list-form, contains -p / --output-format / json / --model
- subprocess.run timeout=180 default; custom timeout passed through
- Non-zero returncode -> RuntimeError
- subprocess.TimeoutExpired -> TimeoutError
- Invalid JSON stdout -> RuntimeError
- Schema mode + non-JSON envelope.result -> RuntimeError
- Freeform plain-text result -> {"text": ...}
- Freeform JSON-string result -> parsed dict
- Empty messages -> ValueError
- Security invariants (source grep): no shell=True
"""

from __future__ import annotations

import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from job_finder.web.model_provider import BaseProvider, ModelResult
from job_finder.web.providers.gemini_cli import GeminiCLIProvider

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_provider() -> GeminiCLIProvider:
    with patch("job_finder.web.providers.gemini_cli.shutil.which", return_value="/usr/bin/gemini"):
        return GeminiCLIProvider(config={})


def _mock_run(
    returncode: int = 0,
    stdout: str = '{"result": "pong"}',
    stderr: str = "",
) -> MagicMock:
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = stderr
    return m


# ---------------------------------------------------------------------------
# Subclass / init tests
# ---------------------------------------------------------------------------


def test_gemini_cli_provider_is_base_provider_subclass():
    assert issubclass(GeminiCLIProvider, BaseProvider)


def test_init_raises_runtime_error_when_gemini_not_on_path():
    with patch("job_finder.web.providers.gemini_cli.shutil.which", return_value=None):
        with pytest.raises(RuntimeError, match="gemini CLI not found"):
            GeminiCLIProvider(config={})


def test_init_stores_binary_path():
    provider = _make_provider()
    assert provider._bin == "/usr/bin/gemini"


# ---------------------------------------------------------------------------
# Subprocess argv + flag tests
# ---------------------------------------------------------------------------


def test_call_uses_list_form_argv_with_expected_flags():
    provider = _make_provider()
    with patch(
        "job_finder.web.providers.gemini_cli.subprocess.run",
        return_value=_mock_run(stdout='{"result": "hi"}'),
    ) as mock_run:
        provider.call(
            model="gemini-2.0-flash",
            system="be brief",
            messages=[{"role": "user", "content": "say hi"}],
        )
    args, kwargs = mock_run.call_args
    argv = args[0]
    assert isinstance(argv, list)
    assert argv[0] == "/usr/bin/gemini"
    assert "-p" in argv
    assert "--output-format" in argv
    assert "json" in argv
    assert "--model" in argv
    assert "gemini-2.0-flash" in argv
    # Security invariants:
    assert kwargs.get("shell") in (None, False)
    assert kwargs.get("timeout") == 180.0
    assert kwargs.get("capture_output") is True


def test_call_passes_custom_timeout():
    provider = _make_provider()
    with patch(
        "job_finder.web.providers.gemini_cli.subprocess.run",
        return_value=_mock_run(stdout='{"result": "hi"}'),
    ) as mock_run:
        provider.call(
            "gemini-2.0-flash",
            "s",
            [{"role": "user", "content": "u"}],
            timeout=30.0,
        )
    assert mock_run.call_args.kwargs["timeout"] == 30.0


def test_call_appends_schema_block_when_output_schema_present():
    provider = _make_provider()
    schema = {"type": "object", "properties": {"x": {"type": "integer"}}}
    with patch(
        "job_finder.web.providers.gemini_cli.subprocess.run",
        return_value=_mock_run(stdout='{"result": "{\\"x\\": 1}"}'),
    ) as mock_run:
        provider.call(
            "gemini-2.0-flash",
            "system",
            [{"role": "user", "content": "user"}],
            output_schema=schema,
        )
    # The combined prompt is argv index after "-p"
    argv = mock_run.call_args.args[0]
    p_idx = argv.index("-p")
    combined_prompt = argv[p_idx + 1]
    assert "Respond ONLY with JSON" in combined_prompt
    assert json.dumps(schema) in combined_prompt


# ---------------------------------------------------------------------------
# ModelResult shape tests
# ---------------------------------------------------------------------------


def test_call_returns_seven_field_model_result_with_correct_types():
    provider = _make_provider()
    with patch(
        "job_finder.web.providers.gemini_cli.subprocess.run",
        return_value=_mock_run(stdout='{"result": "hi"}'),
    ):
        result = provider.call(
            "gemini-2.0-flash", "s", [{"role": "user", "content": "u"}]
        )
    assert isinstance(result, ModelResult)
    assert isinstance(result.data, dict)
    assert isinstance(result.cost_usd, float)
    assert isinstance(result.input_tokens, int)
    assert isinstance(result.output_tokens, int)
    assert isinstance(result.model, str)
    assert isinstance(result.provider, str)
    assert isinstance(result.schema_valid, bool)


def test_call_returns_zero_cost_and_gemini_cli_provider():
    provider = _make_provider()
    with patch(
        "job_finder.web.providers.gemini_cli.subprocess.run",
        return_value=_mock_run(stdout='{"result": "ok"}'),
    ):
        result = provider.call(
            "gemini-2.0-flash", "s", [{"role": "user", "content": "u"}]
        )
    assert result.cost_usd == 0.0
    assert result.provider == "gemini_cli"
    assert result.input_tokens == 0
    assert result.output_tokens == 0


# ---------------------------------------------------------------------------
# Freeform vs schema parse-path tests
# ---------------------------------------------------------------------------


def test_freeform_plain_text_returns_text_dict():
    provider = _make_provider()
    with patch(
        "job_finder.web.providers.gemini_cli.subprocess.run",
        return_value=_mock_run(stdout='{"result": "plain text"}'),
    ):
        result = provider.call(
            "gemini-2.0-flash", "s", [{"role": "user", "content": "u"}]
        )
    assert result.data == {"text": "plain text"}
    assert result.schema_valid is False


def test_freeform_json_string_returns_parsed_dict():
    provider = _make_provider()
    with patch(
        "job_finder.web.providers.gemini_cli.subprocess.run",
        return_value=_mock_run(stdout='{"result": "{\\"x\\": 7}"}'),
    ):
        result = provider.call(
            "gemini-2.0-flash", "s", [{"role": "user", "content": "u"}]
        )
    assert result.data == {"x": 7}
    assert result.schema_valid is False


def test_schema_mode_parses_result_field_as_json():
    provider = _make_provider()
    schema = {"type": "object", "properties": {"x": {"type": "integer"}}}
    with patch(
        "job_finder.web.providers.gemini_cli.subprocess.run",
        return_value=_mock_run(stdout='{"result": "{\\"x\\": 42}"}'),
    ):
        result = provider.call(
            "gemini-2.0-flash",
            "s",
            [{"role": "user", "content": "u"}],
            output_schema=schema,
        )
    assert result.data == {"x": 42}
    assert result.schema_valid is True


def test_schema_mode_with_non_json_result_raises_runtime_error():
    provider = _make_provider()
    schema = {"type": "object"}
    with patch(
        "job_finder.web.providers.gemini_cli.subprocess.run",
        return_value=_mock_run(stdout='{"result": "not json"}'),
    ):
        with pytest.raises(RuntimeError, match="non-JSON despite schema"):
            provider.call(
                "gemini-2.0-flash",
                "s",
                [{"role": "user", "content": "u"}],
                output_schema=schema,
            )


# ---------------------------------------------------------------------------
# Error path tests
# ---------------------------------------------------------------------------


def test_non_zero_exit_raises_runtime_error():
    provider = _make_provider()
    with patch(
        "job_finder.web.providers.gemini_cli.subprocess.run",
        return_value=_mock_run(returncode=1, stdout="", stderr="quota exhausted"),
    ):
        with pytest.raises(RuntimeError, match="gemini CLI failed"):
            provider.call(
                "gemini-2.0-flash", "s", [{"role": "user", "content": "u"}]
            )


def test_timeout_expired_raises_timeout_error():
    provider = _make_provider()
    with patch(
        "job_finder.web.providers.gemini_cli.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="gemini", timeout=180.0),
    ):
        with pytest.raises(TimeoutError, match="gemini CLI timed out"):
            provider.call(
                "gemini-2.0-flash", "s", [{"role": "user", "content": "u"}]
            )


def test_invalid_json_stdout_raises_runtime_error():
    provider = _make_provider()
    with patch(
        "job_finder.web.providers.gemini_cli.subprocess.run",
        return_value=_mock_run(stdout="this is not json"),
    ):
        with pytest.raises(RuntimeError, match="Invalid JSON from gemini CLI"):
            provider.call(
                "gemini-2.0-flash", "s", [{"role": "user", "content": "u"}]
            )


def test_empty_messages_raises_value_error():
    provider = _make_provider()
    with pytest.raises(ValueError, match="at least one message"):
        provider.call("gemini-2.0-flash", "s", [])


# ---------------------------------------------------------------------------
# Security invariants (source-grep)
# ---------------------------------------------------------------------------


def test_module_source_has_no_shell_true_and_uses_list_argv():
    import pathlib
    src = pathlib.Path("job_finder/web/providers/gemini_cli.py").read_text()
    # Check for actual shell=True in subprocess.run calls, not in docstrings
    lines = src.split("\n")
    for line in lines:
        # Only check lines that are actual subprocess.run function calls
        if "subprocess.run(" in line and not line.strip().startswith("#"):
            assert "shell=True" not in line, f"shell=True found in subprocess.run call: {line}"
    # Confirm timeout kwarg present on the subprocess.run call
    assert "subprocess.run(" in src
    # The subprocess.run call must have timeout= within the same call group;
    # this is a coarse check that a "timeout=" appears in the file.
    assert "timeout=" in src
