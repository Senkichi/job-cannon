"""Tests for ATS text normalization in docx_formatter."""

import pytest

from job_finder.web.docx_formatter import (
    _ATS_NORMALIZE_MAP,
    _normalize_for_ats,
    _normalize_resume_data,
)


class TestNormalizeForAts:
    """Unit tests for _normalize_for_ats()."""

    def test_empty_string(self):
        assert _normalize_for_ats("") == ""

    def test_none_passthrough(self):
        assert _normalize_for_ats(None) is None

    def test_plain_ascii_unchanged(self):
        text = "Senior Engineer at Acme Corp, 2020-2024"
        assert _normalize_for_ats(text) == text

    def test_smart_single_quotes(self):
        assert _normalize_for_ats("\u2018hello\u2019") == "'hello'"

    def test_smart_double_quotes(self):
        assert _normalize_for_ats("\u201Chello\u201D") == '"hello"'

    def test_em_dash(self):
        assert _normalize_for_ats("Python \u2014 Expert") == "Python  -  Expert"

    def test_en_dash(self):
        assert _normalize_for_ats("2020\u20132024") == "2020-2024"

    def test_ellipsis(self):
        assert _normalize_for_ats("etc\u2026") == "etc..."

    def test_non_breaking_space(self):
        assert _normalize_for_ats("100\u00A0employees") == "100 employees"

    def test_zero_width_chars_removed(self):
        assert _normalize_for_ats("hello\u200Bworld") == "helloworld"
        assert _normalize_for_ats("test\u200C\u200D\uFEFF") == "test"

    def test_bullet_chars(self):
        assert _normalize_for_ats("\u2022 Item 1") == "- Item 1"
        assert _normalize_for_ats("\u25CF Item 2") == "- Item 2"

    def test_guillemets(self):
        assert _normalize_for_ats("\u00ABhello\u00BB") == '"hello"'

    def test_every_char_in_map(self):
        """Every character in the map produces a different output."""
        for code_point in _ATS_NORMALIZE_MAP:
            char = chr(code_point)
            result = _normalize_for_ats(char)
            # Either replaced with something different or removed
            assert result != char or result == ""

    def test_accented_chars_preserved(self):
        """Non-Latin characters NOT in the map must survive."""
        assert _normalize_for_ats("Jos\u00e9 Garc\u00eda") == "Jos\u00e9 Garc\u00eda"
        assert _normalize_for_ats("\u00fc\u00f6\u00e4") == "\u00fc\u00f6\u00e4"


class TestNormalizeResumeData:
    """Unit tests for _normalize_resume_data()."""

    def test_string(self):
        assert _normalize_resume_data("hello\u2019s") == "hello's"

    def test_list(self):
        result = _normalize_resume_data(["\u201Cfoo\u201D", "plain"])
        assert result == ['"foo"', "plain"]

    def test_dict(self):
        result = _normalize_resume_data({"key": "\u2014value"})
        assert result == {"key": " - value"}

    def test_nested(self):
        data = {
            "name": "Jos\u00e9",
            "positions": [
                {"title": "Staff Engineer \u2014 Platform", "achievements": ["\u2022 Built X"]}
            ],
        }
        result = _normalize_resume_data(data)
        assert result["name"] == "Jos\u00e9"
        assert result["positions"][0]["title"] == "Staff Engineer  -  Platform"
        assert result["positions"][0]["achievements"][0] == "- Built X"

    def test_non_string_passthrough(self):
        assert _normalize_resume_data(42) == 42
        assert _normalize_resume_data(None) is None
        assert _normalize_resume_data(True) is True

    def test_immutability(self):
        """Original data must not be mutated."""
        original = {"name": "\u201Ctest\u201D"}
        _normalize_resume_data(original)
        assert original["name"] == "\u201Ctest\u201D"


class TestBuildResumeDocxNormalization:
    """Integration test: build_resume_docx applies ATS normalization."""

    def test_docx_output_normalized(self):
        from job_finder.web.docx_formatter import build_resume_docx

        resume_data = {
            "name": "Test\u2019s User",
            "contact_line": "email\u00A0|\u00A0phone",
            "summary": "\u201CSmart quotes\u201D in summary",
            "skills": ["Python", "SQL\u2014Expert"],
            "positions": [
                {
                    "title": "Engineer",
                    "company": "Corp\u2026",
                    "dates": "2020\u20132024",
                    "achievements": ["\u2022 Built systems"],
                }
            ],
            "education": [
                {"degree": "BS", "institution": "MIT", "year": "2020"}
            ],
        }
        buf = build_resume_docx(resume_data)
        assert buf is not None
        assert buf.tell() == 0
        # Verify it's valid DOCX (starts with PK zip header)
        header = buf.read(4)
        assert header == b"PK\x03\x04"
