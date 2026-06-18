"""Tests for resume parser module.

These tests assert the call_model invocation matches the *canonical* signature
in job_finder/web/model_provider.py:575-587. The previous test file mocked
call_model and asserted on whatever kwargs the resume_parser happened to pass,
which masked finding C-1: resume_parser called call_model with kwargs that
would have raised TypeError at runtime (tier="low", system_prompt=, user_message=,
no conn, no config).

The "valid kwargs at the integration boundary" check (test_call_llm_uses_canonical_kwargs)
is the regression test that would have caught C-1. Keep it.
"""

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from job_finder.web.model_provider import ModelResult
from job_finder.web.onboarding.resume_parser import (
    EXPERIENCE_PROFILE_SCHEMA,
    _call_llm,
    _empty_profile,
    _extract_email,
    _extract_text,
    parse_resume,
)

# --- Fixtures ---


@pytest.fixture
def in_memory_db():
    """Bare sqlite3 connection (no schema). The resume parser does not touch
    scoring_costs in tests because call_model is mocked; an empty connection
    is sufficient as the conn= positional argument."""
    conn = sqlite3.connect(":memory:")
    yield conn
    conn.close()


@pytest.fixture
def stub_config():
    """Minimal config dict for parse_resume. call_model is mocked so the
    routing keys are never read, but pass a realistic shape so any future
    config-reading code doesn't NoneType-crash."""
    return {
        "providers": {
            "quick": {
                "provider": "ollama",
                "model": "qwen2.5:14b",
                "fallback_chain": [],
            },
        },
    }


@pytest.fixture
def valid_profile_data():
    """Full profile matching EXPERIENCE_PROFILE_SCHEMA's required keys."""
    return {
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


def _make_result(data: dict) -> ModelResult:
    return ModelResult(
        data=data,
        cost_usd=0.0,
        input_tokens=0,
        output_tokens=0,
        model="qwen2.5:14b",
        provider="ollama",
        schema_valid=True,
    )


# --- Canonical-signature boundary tests (the C-1 regression set) ---


def test_call_llm_uses_canonical_kwargs(in_memory_db, stub_config, valid_profile_data):
    """C-1 regression: _call_llm must invoke call_model with the exact kwargs
    accepted by model_provider.call_model. tier="quick" (workload class),
    system=, messages=, conn=, config=. NOT tier="low", system_prompt=, user_message=."""
    with patch("job_finder.web.onboarding.resume_parser.call_model") as mock_call:
        mock_call.return_value = _make_result(valid_profile_data)

        _call_llm("Sample resume text", in_memory_db, stub_config)

        mock_call.assert_called_once()
        kwargs = mock_call.call_args.kwargs

        # Workload class (Phase 40 rename: low/mid/high → quick/score/triage).
        assert kwargs["tier"] == "quick", f"Expected tier='quick', got {kwargs.get('tier')!r}"
        # Canonical message shape.
        assert isinstance(kwargs["messages"], list)
        assert kwargs["messages"][0]["role"] == "user"
        assert "Sample resume text" in kwargs["messages"][0]["content"]
        # system, not system_prompt.
        assert "system" in kwargs
        assert "system_prompt" not in kwargs, "Stale kwarg from C-1 bug"
        assert "user_message" not in kwargs, "Stale kwarg from C-1 bug"
        # Required positional/keyword args for call_model.
        assert kwargs["conn"] is in_memory_db
        assert kwargs["config"] is stub_config
        # Cost-attribution label so scoring_costs.purpose has a recognizable tag.
        assert kwargs["purpose"] == "resume_parse"
        assert kwargs["max_tokens"] == 2048
        assert kwargs["output_schema"] is EXPERIENCE_PROFILE_SCHEMA


def test_parse_resume_threads_conn_and_config_to_call_llm(
    in_memory_db, stub_config, valid_profile_data, tmp_path
):
    """parse_resume must thread conn/config through to _call_llm. The C-1 bug
    silently dropped these because parse_resume's signature didn't accept them."""
    with (
        patch(
            "job_finder.web.onboarding.resume_parser._extract_text",
            return_value="Sample resume text",
        ),
        patch("job_finder.web.onboarding.resume_parser.call_model") as mock_call,
    ):
        mock_call.return_value = _make_result(valid_profile_data)

        # Use a real Path with a .pdf suffix so _extract_text dispatch logic
        # (which keys off suffix) doesn't reject before reaching the patched fn.
        fake_pdf = tmp_path / "resume.pdf"
        fake_pdf.write_bytes(b"%PDF-1.4\n")

        result = parse_resume(fake_pdf, conn=in_memory_db, config=stub_config)

        kwargs = mock_call.call_args.kwargs
        assert kwargs["conn"] is in_memory_db
        assert kwargs["config"] is stub_config
        assert result == valid_profile_data


# --- Behavior tests (no behavioral change from C-1 fix; just kwarg surface) ---


def test_parse_resume_docx_extracts_paragraphs(
    in_memory_db, stub_config, valid_profile_data, tmp_path
):
    """DOCX extraction reads paragraphs and feeds the result into call_model."""
    with (
        patch("job_finder.web.onboarding.resume_parser.Document") as mock_doc_class,
        patch("job_finder.web.onboarding.resume_parser.call_model") as mock_call,
    ):
        mock_doc = MagicMock()
        mock_para1 = MagicMock()
        mock_para1.text = "First paragraph"
        mock_para2 = MagicMock()
        mock_para2.text = "Second paragraph"
        mock_doc.paragraphs = [mock_para1, mock_para2]
        mock_doc_class.return_value = mock_doc

        mock_call.return_value = _make_result(valid_profile_data)

        fake_docx = tmp_path / "resume.docx"
        fake_docx.write_bytes(b"PK\x03\x04")  # docx files are zip-stamped

        parse_resume(fake_docx, conn=in_memory_db, config=stub_config)

        mock_doc_class.assert_called_once()
        assert len(mock_doc.paragraphs) == 2


def test_parse_resume_rejects_unsupported_extension(in_memory_db, stub_config, tmp_path):
    """Unsupported file extensions raise ValueError (propagates)."""
    fake_txt = tmp_path / "resume.txt"
    fake_txt.write_text("not a resume")

    with pytest.raises(ValueError) as exc_info:
        parse_resume(fake_txt, conn=in_memory_db, config=stub_config)

    assert "Unsupported resume file type" in str(exc_info.value)
    assert ".txt" in str(exc_info.value)


def test_parse_resume_blank_text_returns_empty_profile(in_memory_db, stub_config, tmp_path):
    """Blank extracted text returns empty profile without calling LLM."""
    with (
        patch(
            "job_finder.web.onboarding.resume_parser._extract_text",
            return_value="",
        ),
        patch("job_finder.web.onboarding.resume_parser.call_model") as mock_call,
    ):
        fake_pdf = tmp_path / "resume.pdf"
        fake_pdf.write_bytes(b"%PDF-1.4\n")

        result = parse_resume(fake_pdf, conn=in_memory_db, config=stub_config)

        assert result == _empty_profile()
        mock_call.assert_not_called()


def test_parse_resume_llm_failure_returns_empty_profile(in_memory_db, stub_config, tmp_path):
    """call_model exception returns empty profile (defensive, never raises)."""
    with (
        patch(
            "job_finder.web.onboarding.resume_parser._extract_text",
            return_value="Sample resume text",
        ),
        patch("job_finder.web.onboarding.resume_parser.call_model") as mock_call,
    ):
        mock_call.side_effect = Exception("LLM error")

        fake_pdf = tmp_path / "resume.pdf"
        fake_pdf.write_bytes(b"%PDF-1.4\n")

        result = parse_resume(fake_pdf, conn=in_memory_db, config=stub_config)

        assert result == _empty_profile()


# --- _extract_text (no signature change; same as before) ---


def test_extract_text_pdf():
    """PDF text extraction reads pages."""
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
    """DOCX extraction reads paragraphs."""
    with patch("job_finder.web.onboarding.resume_parser.Document") as mock_doc_class:
        mock_doc = MagicMock()
        mock_para = MagicMock()
        mock_para.text = "Paragraph text"
        mock_doc.paragraphs = [mock_para]
        mock_doc_class.return_value = mock_doc

        result = _extract_text(Path("resume.docx"))

        assert result == "Paragraph text\n"
        mock_doc_class.assert_called_once()


# --- Schema and helper integrity ---


def test_empty_profile_structure():
    """_empty_profile returns correct structure."""
    profile = _empty_profile()

    assert profile["positions"] == []
    assert profile["skills"] == []
    assert profile["education"] == []
    assert profile["target_roles_suggested"] == []
    assert profile["target_locations_suggested"] == []
    assert profile["salary_range_suggested"] == {}


def test_experience_profile_schema_contains_required_fields():
    """EXPERIENCE_PROFILE_SCHEMA contains all required fields."""
    required = EXPERIENCE_PROFILE_SCHEMA["required"]

    assert "positions" in required
    assert "skills" in required
    assert "education" in required
    assert "target_roles_suggested" in required
    assert "target_locations_suggested" in required
    assert "salary_range_suggested" in required


def test_call_llm_returns_empty_on_no_data(in_memory_db, stub_config):
    """_call_llm returns empty dict when result has no data."""
    with patch("job_finder.web.onboarding.resume_parser.call_model") as mock_call:
        mock_call.return_value = None

        result = _call_llm("Sample text", in_memory_db, stub_config)

        assert result == {}


def test_call_llm_prompt_instructs_skill_inference_when_no_skills_section(
    in_memory_db, stub_config, valid_profile_data
):
    """UAT F4 (2026-05-21): the system prompt sent to call_model must tell
    the model to infer 8-15 skills from position descriptions when the
    resume has no explicit Skills section.

    This is a contract test on the prompt itself — we don't try to assert
    LLM behaviour (model-dependent), only that the instruction is present.
    The instruction's effect on real resumes is verified by manual spot-check
    per the plan's "Evidence this plan might be wrong" guidance."""
    with patch("job_finder.web.onboarding.resume_parser.call_model") as mock_call:
        mock_call.return_value = _make_result(valid_profile_data)

        _call_llm("resume text without a skills section", in_memory_db, stub_config)

        system_prompt = mock_call.call_args.kwargs["system"]

        # Must mention the inference instruction.
        assert "infer" in system_prompt.lower(), (
            "System prompt should tell the model to infer skills when no "
            "explicit Skills section exists."
        )
        # Must reference the source material for inference (positions / bullets).
        assert (
            "position descriptions" in system_prompt.lower()
            or "position description" in system_prompt.lower()
        ), "System prompt should name position descriptions as the inference source."
        # Must specify the 8-15 range so the model returns a useful list, not 2.
        assert "8" in system_prompt and "15" in system_prompt, (
            "System prompt should bound the inference at 8-15 skills."
        )
        # The prompt should call out the "(inferred)" anti-pattern so the
        # model knows not to emit it. (The downstream consumer doesn't
        # distinguish, and the marker would leak into the profile-edit
        # textarea.) This is a soft assertion — the design just requires the
        # guidance be communicated; the exact phrasing is the implementer's
        # call.
        assert "(inferred)" in system_prompt and "no" in system_prompt.lower(), (
            "System prompt should explicitly tell the model not to add '(inferred)' markers."
        )


# --- Email extraction for the IMAP prefill (Issue #399) ---


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Jane Doe\njane.doe@gmail.com\n555-1212", "jane.doe@gmail.com"),
        ("Contact: J.DOE+jobs@Example.CO.UK", "j.doe+jobs@example.co.uk"),
        ("No address here at all", ""),
        ("", ""),
        ("incomplete@domain", ""),  # no TLD → not matched
    ],
)
def test_extract_email(text, expected):
    """_extract_email lifts the first plausible address, lowercased."""
    assert _extract_email(text) == expected


def test_parse_resume_attaches_extracted_email(
    in_memory_db, stub_config, valid_profile_data, tmp_path
):
    """Issue #399: the parsed profile carries an `email` lifted from resume text."""
    with (
        patch(
            "job_finder.web.onboarding.resume_parser._extract_text",
            return_value="Jane Doe\njane.doe@gmail.com\nSoftware Engineer",
        ),
        patch("job_finder.web.onboarding.resume_parser.call_model") as mock_call,
    ):
        mock_call.return_value = _make_result(valid_profile_data)

        fake_pdf = tmp_path / "resume.pdf"
        fake_pdf.write_bytes(b"%PDF-1.4\n")

        result = parse_resume(fake_pdf, conn=in_memory_db, config=stub_config)

        assert result["email"] == "jane.doe@gmail.com"
        # Original profile fields are preserved alongside the new email.
        assert result["skills"] == ["Python", "Flask", "SQL"]


def test_parse_resume_email_survives_empty_llm_profile(in_memory_db, stub_config, tmp_path):
    """Even when the LLM yields nothing, a resume email still reaches the profile."""
    with (
        patch(
            "job_finder.web.onboarding.resume_parser._extract_text",
            return_value="jane.doe@gmail.com",
        ),
        patch("job_finder.web.onboarding.resume_parser.call_model") as mock_call,
    ):
        mock_call.return_value = None  # empty LLM profile

        fake_pdf = tmp_path / "resume.pdf"
        fake_pdf.write_bytes(b"%PDF-1.4\n")

        result = parse_resume(fake_pdf, conn=in_memory_db, config=stub_config)

        assert result["email"] == "jane.doe@gmail.com"
        assert result["positions"] == []  # empty-profile scaffold present


def test_parse_resume_successful_flow(in_memory_db, stub_config, valid_profile_data, tmp_path):
    """End-to-end happy path with mocked call_model."""
    with (
        patch(
            "job_finder.web.onboarding.resume_parser._extract_text",
            return_value="Resume text",
        ),
        patch("job_finder.web.onboarding.resume_parser.call_model") as mock_call,
    ):
        mock_call.return_value = _make_result(valid_profile_data)

        fake_pdf = tmp_path / "resume.pdf"
        fake_pdf.write_bytes(b"%PDF-1.4\n")

        result = parse_resume(fake_pdf, conn=in_memory_db, config=stub_config)

        assert result["positions"][0]["title"] == "Software Engineer"
        assert result["skills"] == ["Python", "Flask", "SQL"]
