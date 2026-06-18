"""Presence assertions for the Pass B clean-VM stranger-run log template.

Markdown-only checks (no app boot). The key invariant: the per-run log can
never silently drift from the wizard, so we import ``_WIZARD_STEPS`` directly
and assert every label appears in the template text.
"""

from __future__ import annotations

from pathlib import Path

from job_finder.web.onboarding.blueprint import _WIZARD_STEPS

_TEMPLATE = (
    Path(__file__).resolve().parents[1] / "packaging" / "windows" / "PASS-B-RUNLOG.template.md"
)


def _read() -> str:
    return _TEMPLATE.read_text(encoding="utf-8")


def test_template_file_exists() -> None:
    assert _TEMPLATE.is_file(), f"missing template: {_TEMPLATE}"


def test_all_wizard_step_labels_present() -> None:
    text = _read()
    missing = [label for _key, label in _WIZARD_STEPS if label not in text]
    assert not missing, f"wizard labels absent from template: {missing}"


def test_packaging_gate_markers_present() -> None:
    text = _read()
    assert "SmartScreen" in text
    assert "Run anyway" in text or "Run-anyway" in text
    assert "defaults to keep" in text or r"%LOCALAPPDATA%\JobCannon" in text


def test_friction_note_column_present() -> None:
    assert "Friction note" in _read()


def test_required_sections_present() -> None:
    text = _read()
    for marker in ("Section A", "Section B", "Section C", "Section D"):
        assert marker in text, f"missing {marker}"
