"""Unit tests for safe_json_load utility (now in job_finder/utils.py).

The function is re-exported from job_finder.web.db_helpers for backward
compatibility, so both import paths are valid.
"""

import pytest

from job_finder.web.db_helpers import safe_json_load


class TestSafeJsonLoad:
    """Tests for safe_json_load pure function.

    safe_json_load wraps json.loads with guards for None, empty string,
    non-string input, and malformed JSON. Returns a caller-supplied default
    on any failure.
    """

    def test_none_returns_default_list(self):
        """safe_json_load(None, default=[]) returns []."""
        assert safe_json_load(None, default=[]) == []

    def test_empty_string_returns_default_list(self):
        """safe_json_load('', default=[]) returns []."""
        assert safe_json_load("", default=[]) == []

    def test_malformed_json_returns_default_list(self):
        """safe_json_load('[broken', default=[]) returns []."""
        assert safe_json_load("[broken", default=[]) == []

    def test_valid_json_list_returned(self):
        """safe_json_load('["a","b"]', default=[]) returns ['a', 'b']."""
        assert safe_json_load('["a","b"]', default=[]) == ["a", "b"]

    def test_valid_json_dict_returned(self):
        """safe_json_load('{"key": "val"}', default={}) returns {'key': 'val'}."""
        assert safe_json_load('{"key": "val"}', default={}) == {"key": "val"}

    def test_none_with_dict_default_returns_empty_dict(self):
        """safe_json_load(None, default={}) returns {}."""
        assert safe_json_load(None, default={}) == {}

    def test_no_default_returns_none(self):
        """safe_json_load(None) with no default argument returns None."""
        assert safe_json_load(None) is None

    def test_non_string_int_returns_default(self):
        """safe_json_load(123, default=[]) returns [] (non-string input)."""
        assert safe_json_load(123, default=[]) == []

    def test_non_string_list_returns_default(self):
        """safe_json_load(['already', 'a', 'list'], default=[]) returns []."""
        assert safe_json_load(["already", "a", "list"], default=[]) == []

    def test_valid_json_preserves_types(self):
        """safe_json_load('{"a": 1}', default={}) returns {'a': 1} with int value."""
        result = safe_json_load('{"a": 1}', default={})
        assert result == {"a": 1}
        assert isinstance(result["a"], int)

    def test_debug_log_on_malformed(self, caplog):
        """safe_json_load logs at DEBUG level when JSONDecodeError occurs."""
        import logging

        with caplog.at_level(logging.DEBUG, logger="job_finder.utils"):
            safe_json_load("[broken", default=[])

        assert len(caplog.records) >= 1
        assert caplog.records[0].levelno == logging.DEBUG
