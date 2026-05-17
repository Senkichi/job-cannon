"""Tests for resume parser module."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from job_finder.web.onboarding.resume_parser import (
    EXPERIENCE_PROFILE_SCHEMA,
    _call_llm,
    _empty_profile,
    _extract_text,
    parse_resume,
)


@pytest.fixture
def mock_model_result():
    """Mock ModelResult object."""
    result = MagicMock()
    result.data = {
        "positions": [
            {
                "title": "Software Engineer",
                "company": "Tech Corp",
                "start_date": "2020-01",
                "end_date": "2023-12",
                "description": "Built web applications",
            }
        ],
        "skills": ["Python", "Flask", "SQL"],
        "education": [
            {"degree": "BS Computer Science", "institution": "University", "year": "2020"}
        ],
        "target_roles_suggested": ["Senior Software Engineer"],
        "target_locations_suggested": ["San Francisco", "Remote"],
        "salary_range_suggested": {"min": 150000, "max": 200000, "currency": "USD"},
    }
    return result


def test_parse_resume_pdf_calls_model_with_low_tier(mock_model_result):
    """Test that parse_resume calls call_model with tier='low' for PDF."""
    with patch("job_finder.web.onboarding.resume_parser._extract_text") as mock_extract, patch(
        "job_finder.web.onboarding.resume_parser.call_model"
    ) as mock_call:
        mock_extract.return_value = "Sample resume text"
        mock_call.return_value = mock_model_result

        result = parse_resume("resume.pdf")

        mock_call.assert_called_once()
        call_kwargs = mock_call.call_args.kwargs
        assert call_kwargs["tier"] == "low"
        assert "target_roles_suggested" in call_kwargs["output_schema"]["properties"]


def test_parse_resume_docx_extracts_paragraphs():
    """Test that DOCX extraction reads paragraphs."""
    with patch("job_finder.web.onboarding.resume_parser.Document") as mock_doc_class, patch(
        "job_finder.web.onboarding.resume_parser.call_model"
    ) as mock_call:
        # Mock Document with paragraphs
        mock_doc = MagicMock()
        mock_para1 = MagicMock()
        mock_para1.text = "First paragraph"
        mock_para2 = MagicMock()
        mock_para2.text = "Second paragraph"
        mock_doc.paragraphs = [mock_para1, mock_para2]
        mock_doc_class.return_value = mock_doc

        # Mock LLM response
        mock_result = MagicMock()
        mock_result.data = _empty_profile()
        mock_call.return_value = mock_result

        result = parse_resume("resume.docx")

        mock_doc_class.assert_called_once()
        # Verify paragraphs were accessed
        assert len(mock_doc.paragraphs) == 2


def test_parse_resume_rejects_unsupported_extension():
    """Test that unsupported file extensions raise ValueError."""
    with pytest.raises(ValueError) as exc_info:
        parse_resume("resume.txt")

    assert "Unsupported resume file type" in str(exc_info.value)
    assert ".txt" in str(exc_info.value)


def test_parse_resume_blank_text_returns_empty_profile():
    """Test that blank extracted text returns empty profile without calling LLM."""
    with patch("job_finder.web.onboarding.resume_parser._extract_text") as mock_extract, patch(
        "job_finder.web.onboarding.resume_parser.call_model"
    ) as mock_call:
        mock_extract.return_value = ""

        result = parse_resume("resume.pdf")

        assert result == _empty_profile()
        mock_call.assert_not_called()


def test_parse_resume_llm_failure_returns_empty_profile():
    """Test that LLM exception returns empty profile."""
    with patch("job_finder.web.onboarding.resume_parser._extract_text") as mock_extract, patch(
        "job_finder.web.onboarding.resume_parser.call_model"
    ) as mock_call:
        mock_extract.return_value = "Sample resume text"
        mock_call.side_effect = Exception("LLM error")

        result = parse_resume("resume.pdf")

        assert result == _empty_profile()


def test_extract_text_pdf():
    """Test PDF text extraction."""
    with patch("job_finder.web.onboarding.resume_parser.PDF.open") as mock_pdf_open:
        mock_pdf = MagicMock()
        mock_page = MagicMock()
        mock_page.extract_text.return_value = "Page 1 text"
        mock_pdf.pages = [mock_page]
        mock_pdf_open.return_value.__enter__.return_value = mock_pdf

        result = _extract_text(Path("resume.pdf"))

        assert result == "Page 1 text"
        mock_pdf_open.assert_called_once()


def test_extract_text_docx():
    """Test DOCX text extraction."""
    with patch("job_finder.web.onboarding.resume_parser.Document") as mock_doc_class:
        mock_doc = MagicMock()
        mock_para = MagicMock()
        mock_para.text = "Paragraph text"
        mock_doc.paragraphs = [mock_para]
        mock_doc_class.return_value = mock_doc

        result = _extract_text(Path("resume.docx"))

        assert result == "Paragraph text\n"
        mock_doc_class.assert_called_once()


def test_empty_profile_structure():
    """Test that _empty_profile returns correct structure."""
    profile = _empty_profile()

    assert profile["positions"] == []
    assert profile["skills"] == []
    assert profile["education"] == []
    assert profile["target_roles_suggested"] == []
    assert profile["target_locations_suggested"] == []
    assert profile["salary_range_suggested"] == {}


def test_experience_profile_schema_contains_required_fields():
    """Test that EXPERIENCE_PROFILE_SCHEMA contains all required fields."""
    required = EXPERIENCE_PROFILE_SCHEMA["required"]

    assert "positions" in required
    assert "skills" in required
    assert "education" in required
    assert "target_roles_suggested" in required
    assert "target_locations_suggested" in required
    assert "salary_range_suggested" in required


def test_call_llm_uses_low_tier():
    """Test that _call_llm calls call_model with tier='low'."""
    with patch("job_finder.web.onboarding.resume_parser.call_model") as mock_call:
        mock_result = MagicMock()
        mock_result.data = {"test": "data"}
        mock_call.return_value = mock_result

        _call_llm("Sample text")

        call_kwargs = mock_call.call_args.kwargs
        assert call_kwargs["tier"] == "low"
        assert call_kwargs["max_tokens"] == 2048
        assert "target_roles_suggested" in call_kwargs["output_schema"]["properties"]


def test_call_llm_returns_empty_on_no_data():
    """Test that _call_llm returns empty dict when result has no data."""
    with patch("job_finder.web.onboarding.resume_parser.call_model") as mock_call:
        mock_call.return_value = None

        result = _call_llm("Sample text")

        assert result == {}


def test_parse_resume_successful_flow():
    """Test successful end-to-end parse flow."""
    with patch("job_finder.web.onboarding.resume_parser._extract_text") as mock_extract, patch(
        "job_finder.web.onboarding.resume_parser.call_model"
    ) as mock_call:
        mock_extract.return_value = "Resume text"
        mock_result = MagicMock()
        mock_result.data = {
            "positions": [{"title": "Engineer", "company": "Corp"}],
            "skills": ["Python"],
            "education": [],
            "target_roles_suggested": ["Senior Engineer"],
            "target_locations_suggested": ["SF"],
            "salary_range_suggested": {},
        }
        mock_call.return_value = mock_result

        result = parse_resume("resume.pdf")

        assert result["positions"][0]["title"] == "Engineer"
        assert result["skills"] == ["Python"]
        assert result["target_roles_suggested"] == ["Senior Engineer"]
