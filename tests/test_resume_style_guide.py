"""Tests for resume_style_guide.py — load/save I/O helpers, directives, and extraction.

Covers:
- load_style_guide: returns {} when file missing.
- save_style_guide + load_style_guide round-trip: data is preserved.
- _build_style_guide_directives: converts guide dict to directive list.
- extract_style_guide: calls Sonnet and returns structured style guide dict.
"""

import json
import os
import sqlite3
from unittest.mock import patch

import pytest

from job_finder.web.resume_style_guide import load_style_guide, save_style_guide


class TestLoadStyleGuide:
    def test_load_style_guide_returns_empty_dict_when_file_missing(self, tmp_path):
        """load_style_guide on a non-existent path returns {}."""
        missing = str(tmp_path / "nonexistent_style_guide.json")
        result = load_style_guide(missing)
        assert result == {}

    def test_load_style_guide_returns_dict(self, tmp_path):
        """load_style_guide on a valid JSON file returns a dict."""
        path = str(tmp_path / "style_guide.json")
        data = {"tone": "professional", "sections": ["summary", "experience"]}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        result = load_style_guide(path)
        assert isinstance(result, dict)


class TestSaveLoadRoundtrip:
    def test_save_load_roundtrip(self, tmp_path):
        """save_style_guide + load_style_guide preserves the original dict."""
        path = str(tmp_path / "style_guide.json")
        guide = {
            "tone": "professional",
            "bullet_style": "action verbs",
            "sections": ["summary", "experience", "skills"],
            "max_pages": 2,
        }
        save_style_guide(guide, path)
        loaded = load_style_guide(path)
        assert loaded == guide

    def test_save_creates_file(self, tmp_path):
        """save_style_guide creates the file on disk."""
        path = str(tmp_path / "style_guide.json")
        assert not os.path.exists(path)
        save_style_guide({"key": "value"}, path)
        assert os.path.exists(path)

    def test_save_uses_indent_2(self, tmp_path):
        """save_style_guide writes with indent=2 (human-readable)."""
        path = str(tmp_path / "style_guide.json")
        save_style_guide({"key": "value"}, path)
        with open(path, encoding="utf-8") as f:
            raw = f.read()
        # indent=2 means the file contains newlines and spaces
        assert "\n" in raw
        assert "  " in raw

    def test_save_handles_unicode(self, tmp_path):
        """save_style_guide preserves non-ASCII characters (ensure_ascii=False)."""
        path = str(tmp_path / "style_guide.json")
        guide = {"greeting": "Bonjour — caf\u00e9"}
        save_style_guide(guide, path)
        loaded = load_style_guide(path)
        assert loaded["greeting"] == "Bonjour — caf\u00e9"
        # Verify the file contains the actual unicode character, not escaped
        with open(path, encoding="utf-8") as f:
            raw = f.read()
        assert "\u00e9" in raw


# ---------------------------------------------------------------------------
# _build_style_guide_directives tests
# ---------------------------------------------------------------------------


class TestBuildStyleGuideDirectives:
    def test_build_style_guide_directives_returns_list(self):
        """_build_style_guide_directives with a full guide returns a non-empty list."""
        from job_finder.web.resume_style_guide import _build_style_guide_directives

        guide = {
            "bullet_style": "dashes",
            "verb_tense": "past",
            "section_order": ["Summary", "Experience", "Skills"],
            "tone": "direct",
            "date_format": "MMM YYYY",
        }
        result = _build_style_guide_directives(guide)
        assert isinstance(result, list)
        assert len(result) > 0

    def test_build_style_guide_directives_empty_guide(self):
        """_build_style_guide_directives({}) returns empty list."""
        from job_finder.web.resume_style_guide import _build_style_guide_directives

        result = _build_style_guide_directives({})
        assert result == []

    def test_build_style_guide_directives_formats_bullet_style(self):
        """Bullet style appears in directives list."""
        from job_finder.web.resume_style_guide import _build_style_guide_directives

        guide = {"bullet_style": "dashes", "tone": "direct"}
        result = _build_style_guide_directives(guide)
        assert any("Bullet style" in d and "dashes" in d for d in result)

    def test_build_style_guide_directives_joins_section_order(self):
        """section_order list is joined with comma-space."""
        from job_finder.web.resume_style_guide import _build_style_guide_directives

        guide = {"section_order": ["Summary", "Experience", "Skills"]}
        result = _build_style_guide_directives(guide)
        assert any("Section order" in d and "Summary, Experience, Skills" in d for d in result)

    def test_build_style_guide_directives_skips_empty_fields(self):
        """Empty string fields are excluded from directives."""
        from job_finder.web.resume_style_guide import _build_style_guide_directives

        guide = {"bullet_style": "dashes", "verb_tense": "", "tone": "direct"}
        result = _build_style_guide_directives(guide)
        assert any("Bullet style" in d for d in result)
        assert any("Tone" in d for d in result)
        assert not any("Verb tense:" in d for d in result)


# ---------------------------------------------------------------------------
# extract_style_guide tests
# ---------------------------------------------------------------------------


class TestSchemaExpansion:
    def test_schema_has_all_new_fields(self):
        from job_finder.web.resume_style_guide import STYLE_GUIDE_SCHEMA

        new_fields = [
            "summary_formula",
            "skills_format",
            "bullet_formula",
            "bullet_counts",
            "confidentiality_rules",
            "typography_rules",
            "jd_mirroring_rules",
            "anti_patterns",
            "role_archetype",
        ]
        for field in new_fields:
            assert field in STYLE_GUIDE_SCHEMA["properties"], f"Missing: {field}"

    def test_consistency_notes_removed_from_schema(self):
        from job_finder.web.resume_style_guide import STYLE_GUIDE_SCHEMA

        assert "consistency_notes" not in STYLE_GUIDE_SCHEMA["properties"]

    def test_consistency_notes_removed_from_field_labels(self):
        from job_finder.web.resume_style_guide import FIELD_LABELS

        assert "consistency_notes" not in FIELD_LABELS

    def test_required_fields_unchanged(self):
        from job_finder.web.resume_style_guide import STYLE_GUIDE_SCHEMA

        assert STYLE_GUIDE_SCHEMA["required"] == [
            "bullet_style",
            "verb_tense",
            "section_order",
            "tone",
            "date_format",
        ]

    def test_directives_bullet_counts_dict(self):
        from job_finder.web.resume_style_guide import _build_style_guide_directives

        guide = {
            "bullet_counts": {"current": "4-6", "previous": "2-3", "prior": "1-2", "early": "1"}
        }
        result = _build_style_guide_directives(guide)
        assert len(result) == 1
        assert "Bullet counts:" in result[0]
        assert "current 4-6" in result[0]
        assert "previous 2-3" in result[0]

    def test_directives_anti_patterns_list(self):
        from job_finder.web.resume_style_guide import _build_style_guide_directives

        guide = {"anti_patterns": ["Starting with 'Led'", "Vague metrics"]}
        result = _build_style_guide_directives(guide)
        assert any("Anti-patterns:" in d for d in result)
        assert any("Starting with 'Led'" in d for d in result)

    def test_directives_new_string_fields(self):
        from job_finder.web.resume_style_guide import _build_style_guide_directives

        guide = {
            "summary_formula": "Title + years + specialization",
            "skills_format": "Grouped by category",
            "bullet_formula": "Action verb + context + impact + metric",
            "confidentiality_rules": "Replace client names with descriptors",
            "typography_rules": "Em dash between clauses",
            "jd_mirroring_rules": "Mirror JD terminology in bullets",
            "role_archetype": "IC technical leader",
        }
        result = _build_style_guide_directives(guide)
        for label in [
            "Summary formula",
            "Skills format",
            "Bullet formula",
            "Confidentiality rules",
            "Typography rules",
            "JD mirroring rules",
            "Role archetype",
        ]:
            assert any(label in d for d in result), f"Missing directive for: {label}"


class TestExtractStyleGuide:
    @pytest.fixture
    def migrated_conn(self, tmp_db_path):
        """Return an open migrated connection (with scoring_costs table)."""
        from job_finder.web.db_migrate import run_migrations

        run_migrations(tmp_db_path)
        conn = sqlite3.connect(tmp_db_path)
        conn.row_factory = sqlite3.Row
        yield conn
        conn.close()

    @pytest.fixture
    def sample_config(self):
        """Minimal config dict with scoring model names."""
        return {
            "scoring": {
                "models": {"sonnet": "claude-sonnet-4-6"},
                "daily_budget_usd": 50.0,
            }
        }

    def test_extract_style_guide_returns_valid_dict(self, migrated_conn, sample_config):
        """extract_style_guide() returns dict with required keys when call_claude succeeds."""
        from job_finder.web.resume_style_guide import extract_style_guide

        canned = {
            "bullet_style": "dashes",
            "verb_tense": "past",
            "section_order": ["Summary", "Experience", "Skills"],
            "tone": "direct",
            "date_format": "MMM YYYY",
        }
        with patch("job_finder.web.resume_style_guide.call_claude") as mock_call:
            mock_call.return_value = (canned, 0.01)
            result = extract_style_guide(
                raw_text="Sample resume text with plenty of content...",
                existing_guide={},
                conn=migrated_conn,
                config=sample_config,
            )

        assert isinstance(result, dict)
        for key in ("bullet_style", "verb_tense", "section_order", "tone", "date_format"):
            assert key in result, f"Missing key: {key}"

    def test_extract_style_guide_merges_with_existing(self, migrated_conn, sample_config):
        """When existing_guide is non-empty, call_claude prompt includes existing guide."""
        from job_finder.web.resume_style_guide import extract_style_guide

        existing = {
            "bullet_style": "bullets",
            "verb_tense": "past",
            "section_order": ["Summary", "Experience"],
            "tone": "professional",
            "date_format": "YYYY",
        }

        captured_messages = []

        def capture_call(*args, **kwargs):
            captured_messages.extend(kwargs.get("messages", []))
            return (existing, 0.01)

        with patch("job_finder.web.resume_style_guide.call_claude", side_effect=capture_call):
            extract_style_guide(
                raw_text="Resume content...",
                existing_guide=existing,
                conn=migrated_conn,
                config=sample_config,
            )

        all_content = " ".join(str(m) for m in captured_messages)
        assert "bullet_style" in all_content, (
            "Existing guide JSON not included in prompt when existing_guide is non-empty"
        )

    def test_extract_style_guide_returns_none_on_error(self, migrated_conn, sample_config):
        """extract_style_guide() returns None on call_claude error."""
        from job_finder.web.resume_style_guide import extract_style_guide

        existing = {
            "bullet_style": "dashes",
            "verb_tense": "past",
            "section_order": [],
            "tone": "direct",
            "date_format": "MMM YYYY",
        }
        with patch(
            "job_finder.web.resume_style_guide.call_claude", side_effect=Exception("API error")
        ):
            result = extract_style_guide(
                raw_text="Resume content...",
                existing_guide=existing,
                conn=migrated_conn,
                config=sample_config,
            )
        assert result is None


class TestMigrateStyleGuide:
    @pytest.fixture
    def migrated_conn(self, tmp_db_path):
        from job_finder.web.db_migrate import run_migrations

        run_migrations(tmp_db_path)
        conn = sqlite3.connect(tmp_db_path)
        conn.row_factory = sqlite3.Row
        yield conn
        conn.close()

    @pytest.fixture
    def sample_config(self):
        return {"scoring": {"models": {"sonnet": "claude-sonnet-4-6"}, "daily_budget_usd": 50.0}}

    def test_migrate_calls_call_claude_with_correct_purpose(
        self, migrated_conn, sample_config, tmp_path
    ):
        from job_finder.web.resume_style_guide import migrate_style_guide

        guide_path = str(tmp_path / "style_guide.json")
        existing = {
            "bullet_style": "dashes",
            "verb_tense": "past",
            "section_order": ["Summary"],
            "tone": "direct",
            "date_format": "MMM YYYY",
        }
        with open(guide_path, "w") as f:
            json.dump(existing, f)

        captured = {}

        def capture_call_kw(*args, **kwargs):
            captured["purpose"] = kwargs.get("purpose", "")
            captured["output_schema"] = kwargs.get("output_schema")
            merged = dict(existing)
            merged["summary_formula"] = "Title + years"
            return (merged, 0.05)

        with patch("job_finder.web.resume_style_guide.call_claude", side_effect=capture_call_kw):
            result = migrate_style_guide(sample_config, migrated_conn, style_guide_path=guide_path)

        assert captured["purpose"] == "style_guide_migration"

    def test_migrate_preserves_existing_fields(self, migrated_conn, sample_config, tmp_path):
        from job_finder.web.resume_style_guide import migrate_style_guide

        guide_path = str(tmp_path / "style_guide.json")
        existing = {
            "bullet_style": "dashes",
            "verb_tense": "past",
            "section_order": ["Summary"],
            "tone": "direct",
            "date_format": "MMM YYYY",
        }
        with open(guide_path, "w") as f:
            json.dump(existing, f)

        merged = dict(existing)
        merged["summary_formula"] = "Title + years"
        merged["role_archetype"] = "IC leader"

        with patch("job_finder.web.resume_style_guide.call_claude", return_value=(merged, 0.05)):
            result = migrate_style_guide(sample_config, migrated_conn, style_guide_path=guide_path)

        assert result is not None
        assert result["bullet_style"] == "dashes"
        assert result["tone"] == "direct"
        assert result["summary_formula"] == "Title + years"

    def test_migrate_returns_none_on_error(self, migrated_conn, sample_config, tmp_path):
        from job_finder.web.resume_style_guide import migrate_style_guide

        guide_path = str(tmp_path / "style_guide.json")
        with open(guide_path, "w") as f:
            json.dump({}, f)

        with patch(
            "job_finder.web.resume_style_guide.call_claude", side_effect=Exception("API error")
        ):
            result = migrate_style_guide(sample_config, migrated_conn, style_guide_path=guide_path)
        assert result is None
