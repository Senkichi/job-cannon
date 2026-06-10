"""Config-surface guard — prevents config ↔ code ↔ UI drift.

Three invariants:

1. **Every config.example.yaml leaf key has a reader.**
   Walk parsed example YAML to leaf key-paths; assert each leaf key name
   appears (as a word) in at least one job_finder/ Python file outside of
   the settings form-to-config writer (``blueprints/settings.py``).  An
   allowlist covers legitimately dead or aspirational keys.

2. **Settings form field ↔ config key, bidirectional.**
   a) Every ``name="X"`` field rendered in ``templates/settings/index.html``
      has a ``_has("X")`` branch in ``_parse_form_to_config`` — catches
      fields whose saves are silently dropped (the "budget" class of bug).
   b) Every ``_has("X")`` branch has a rendered field — catches orphaned
      writers that store config the UI no longer exposes.

3. **Every sources.<name> has an implementation.**
   For each ``sources.*`` block in config.example.yaml, assert a
   ``*_source.py`` module exists in ``job_finder/sources/``.

Known pre-fix drift is marked ``xfail`` (or placed in an allowlist for
longer-lived intentional gaps) with upstream issue references.  Remove
xfail markers once the blocking issues land.

No network / DB access.  Runs under the default parallel suite.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

# ---------------------------------------------------------------------------
# Repo layout
# ---------------------------------------------------------------------------
REPO = Path(__file__).parent.parent
EXAMPLE_YAML = REPO / "config.example.yaml"
JOB_FINDER = REPO / "job_finder"
SETTINGS_HTML = JOB_FINDER / "web" / "templates" / "settings" / "index.html"
# The settings BLUEPRINT is the writer (form → config dict).  We exclude only
# this file from reader checks, not job_finder/settings.py (the Settings
# dataclass, which IS a reader).
SETTINGS_BLUEPRINT = JOB_FINDER / "web" / "blueprints" / "settings.py"
SOURCES_DIR = JOB_FINDER / "sources"

# ---------------------------------------------------------------------------
# Invariant 1 — allowlist
#
# Keys present in config.example.yaml that currently have no reader outside
# blueprints/settings.py.  Each entry must include a comment explaining WHY
# it is here.  Remove an entry the moment working code starts reading that key.
# ---------------------------------------------------------------------------
_INV1_UNREAD_ALLOWLIST: frozenset[str] = frozenset(
    {
        # --- Gmail sender-alias keys ---
        # sources.gmail.senders.{linkedin_alerts,linkedin_jobs} are config-
        # documented aliases but gmail_source.py uses a hardcoded
        # email-address→parser map and never reads these key names.  The
        # config values are user documentation for Gmail filter setup.
        "linkedin_alerts",
        "linkedin_jobs",
        # 'glassdoor', 'indeed', 'ziprecruiter' appear as string literals in
        # gmail_source.py (parser names) and therefore pass without allowlisting.
        # --- Stale-detection thresholds ---
        # stale_detector.py uses module-level constants (_STALE_THRESHOLD_DAYS,
        # _ARCHIVE_THRESHOLD_DAYS) rather than reading from the config dict.
        # The config promises configurable thresholds but the code hardcodes them.
        "stale_threshold_days",
        "archive_threshold_days",
        # --- Staleness-orchestrator cascade timeout ---
        # expiry_checker.py uses a hardcoded _TIMEOUT constant instead of
        # reading staleness.cascade_request_timeout_seconds from config.
        "cascade_request_timeout_seconds",
        # --- ATS scan schedule ---
        # ats.scan_days / ats.scan_hour are written by settings.py but the
        # scheduler factories use a hardcoded schedule and never read these
        # config values.
        "scan_days",
        "scan_hour",
        # --- Output format settings ---
        # output.default_format and output.markdown_path are written by the
        # settings blueprint but are not consumed by any current code path in
        # job_finder/.
        "default_format",
        "markdown_path",
        # --- Aspirational / future-feature keys ---
        # profile.job_archetypes.*.weight_overrides — planned scoring override;
        # no consumer exists yet.  Documented for future use.
        "weight_overrides",
        # --- Parser auto-heal Phase C keys (C1: dormant infrastructure) ---
        # These keys are introduced in config.example.yaml by Phase C / C1 so
        # users can see the full autoheal: block when they copy the template.
        # Readers (defensive config.get('autoheal', {}).get(...)) land in
        # heal_pipeline.py (C3), validator.py (C4), and pipeline_runner.py (C5).
        # Remove from this allowlist when those consumers are merged.
        "heal_enabled",
        "heal_provider",
        "heal_max_attempts",
        "heal_backoff_hours",
        "validate_timeout_s",
    }
)

# ---------------------------------------------------------------------------
# Invariant 2 — form-field allowlists
# ---------------------------------------------------------------------------

# Fields rendered as name="X" that are internal form helpers, NOT config keys.
# These are expected to have no matching _has() branch.
_FORM_ONLY_ALLOWED: frozenset[str] = frozenset(
    {
        "_config_mtime",  # hidden CSRF-like mtime guard, not a config key
        "_serpapi_queries_present",  # sentinel telling parser queries were submitted
        "_thordata_queries_present",
        "_dataforseo_queries_present",
    }
)

# _has("X") branches in _parse_form_to_config that have no rendered form field.
# Each entry is a known case of config being written by the settings parser
# but not exposed in the UI.  Remove once the corresponding cleanup lands.
_PARSER_ONLY_XFAIL: frozenset[str] = frozenset(
    {
        # Google Drive integration fields — the settings parser still writes
        # drive_folder_id / drive_convert_to_gdoc after the Drive UI was
        # removed.  Pending #dead-config cleanup issue.
        "drive_folder_id",
        "drive_convert_to_gdoc",
    }
)

# ---------------------------------------------------------------------------
# Invariant 3 — sources without implementations
# ---------------------------------------------------------------------------

# Sources in config.example.yaml that have no *_source.py and no wiring in
# ingestion_runner.py.  Remove each entry once the implementation lands.
_SOURCES_UNIMPLEMENTED_XFAIL: frozenset[str] = frozenset()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_example_yaml() -> dict:
    return yaml.safe_load(EXAMPLE_YAML.read_text(encoding="utf-8"))


def _walk_leaves(obj: object, path: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
    """Return every leaf key-path in a nested YAML structure.

    A "leaf" is any node that is a scalar, an empty container, or a
    non-empty list (lists are not recursed — they are treated as atomic
    values because YAML sequence items do not have key names).
    """
    if isinstance(obj, dict):
        if not obj:
            # Empty dict counts as a leaf (e.g. weight_overrides: {})
            return [path] if path else []
        paths: list[tuple[str, ...]] = []
        for k, v in obj.items():
            paths.extend(_walk_leaves(v, path + (str(k),)))
        return paths
    else:
        # scalar or list — both are leaves
        return [path] if path else []


def _get_example_yaml_leaf_keys() -> set[str]:
    """Return the terminal key name for every leaf path in config.example.yaml."""
    cfg = _load_example_yaml()
    paths = _walk_leaves(cfg)
    return {p[-1] for p in paths if p}


def _source_names_from_example_yaml() -> set[str]:
    """Return the source names declared under sources: in config.example.yaml."""
    cfg = _load_example_yaml()
    return set((cfg.get("sources") or {}).keys())


def _py_files_in_job_finder(exclude_paths: set[Path] | None = None) -> list[Path]:
    """All .py files under job_finder/, optionally excluding specific paths."""
    exclude_paths = exclude_paths or set()
    return [p for p in JOB_FINDER.rglob("*.py") if p not in exclude_paths]


def _all_source_text(files: list[Path]) -> str:
    """Concatenate source text of the given files."""
    parts: list[str] = []
    for f in files:
        try:
            parts.append(f.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            pass
    return "\n".join(parts)


def _get_form_field_names() -> set[str]:
    """Extract static name="..." values from templates/settings/index.html.

    Dynamic Jinja2 template fields (containing ``{{``) are excluded — they
    are validated indirectly via the parser's loop logic, not by name.
    """
    html = SETTINGS_HTML.read_text(encoding="utf-8", errors="replace")
    raw = re.findall(r'\bname=["\']([^"\']+)["\']', html)
    return {n for n in raw if "{{" not in n and "{%" not in n}


def _get_parser_has_calls() -> set[str]:
    """Extract keys from _has("key") calls inside _parse_form_to_config."""
    src = SETTINGS_BLUEPRINT.read_text(encoding="utf-8", errors="replace")
    # Find the function body by slicing from definition to next top-level def
    func_match = re.search(
        r"def _parse_form_to_config\b.*?(?=\ndef |\Z)",
        src,
        re.DOTALL,
    )
    if not func_match:
        return set()
    func_body = func_match.group(0)
    return set(re.findall(r'_has\(\s*["\']([^"\']+)["\']\s*\)', func_body))


def _get_source_module_names() -> set[str]:
    """Return source names inferred from *_source.py files in job_finder/sources/."""
    names: set[str] = set()
    for p in SOURCES_DIR.glob("*_source.py"):
        # e.g. gmail_source.py → "gmail"
        name = p.stem.removesuffix("_source")
        names.add(name)
    return names


# ---------------------------------------------------------------------------
# Invariant 1 — every config.example.yaml leaf key has a reader
# ---------------------------------------------------------------------------


def test_inv1_example_yaml_keys_have_readers():
    """Every config.example.yaml leaf key must appear in job_finder/ code.

    We search for the key as a whole word (``\\b`` boundary) in any ``.py``
    file under ``job_finder/``, excluding ``blueprints/settings.py`` (the
    form-to-config writer).  This catches keys that appear as dict keys in
    ``.get("key")`` calls, as path-string segments, or as variable names.

    The allowlist ``_INV1_UNREAD_ALLOWLIST`` covers keys that are
    intentionally absent from the read path (dead config, aspirational
    features, hardcoded-equivalent keys).
    """
    leaf_keys = _get_example_yaml_leaf_keys()
    skip = _INV1_UNREAD_ALLOWLIST

    # Exclude only the settings blueprint (the writer), NOT job_finder/settings.py
    # (the Settings dataclass, which IS a reader).
    files = _py_files_in_job_finder(exclude_paths={SETTINGS_BLUEPRINT})
    source_text = _all_source_text(files)

    missing: list[str] = []
    for key in sorted(leaf_keys - skip):
        # Word-boundary search: matches "key" inside 'key', "key", key=..., etc.
        if not re.search(rf"\b{re.escape(key)}\b", source_text):
            missing.append(key)

    assert not missing, (
        "config.example.yaml leaf keys with no reference in job_finder/ "
        "(excluding blueprints/settings.py):\n"
        + "\n".join(f"  - {k}" for k in missing)
        + "\n\nFor each: add a reader, or add to _INV1_UNREAD_ALLOWLIST "
        "with a comment explaining why the key is intentionally unread."
    )


# ---------------------------------------------------------------------------
# Invariant 2a — every form field has a parser branch
# ---------------------------------------------------------------------------


def test_inv2a_form_fields_have_parser_branches():
    """Every rendered settings form field must have a _has('...') branch.

    Catches fields that the UI shows but the settings parser silently ignores
    (the "budget daily_budget_usd" class of bug, fixed in #151).
    """
    form_fields = _get_form_field_names() - _FORM_ONLY_ALLOWED
    parser_keys = _get_parser_has_calls()

    # Dynamic field patterns handled via loops in the parser — the parser
    # iterates form keys by prefix rather than individual _has() calls.
    _DYNAMIC_PREFIXES = (
        "gmail_sender_",  # iterated over configured sender keys
        "serpapi_query_",  # indexed query rows
        "serpapi_location_",
        "thordata_query_",
        "thordata_location_",
        "dataforseo_query_",
        "dataforseo_location_",
        "weight_",  # scored over all weight axis keys
    )

    def _is_dynamic(name: str) -> bool:
        return any(name.startswith(p) for p in _DYNAMIC_PREFIXES)

    missing_parser = {f for f in form_fields if f not in parser_keys and not _is_dynamic(f)}

    assert not missing_parser, (
        "Form fields with no _has('...') branch in _parse_form_to_config:\n"
        + "\n".join(f"  - {f}" for f in sorted(missing_parser))
        + "\n\nEach rendered field must have a matching parser branch, or its "
        "saves are silently dropped."
    )


# ---------------------------------------------------------------------------
# Invariant 2b — every parser branch has a form field
# ---------------------------------------------------------------------------


def test_inv2b_parser_branches_have_form_fields():
    """Every _has('...') branch in _parse_form_to_config must have a form field.

    Catches orphaned writers: config sections being written by the parser
    whose UI controls have been removed (the dead drive config class of bug).

    Known orphaned writers are in _PARSER_ONLY_XFAIL and tracked with xfail
    until the corresponding cleanup issue lands.
    """
    form_fields = _get_form_field_names()
    parser_keys = _get_parser_has_calls()

    # Dynamic field patterns: a parser key like 'drive_folder_id' is flagged
    # if it starts with none of these prefixes AND is absent from form_fields.
    _DYNAMIC_PREFIXES = (
        "gmail_sender_",
        "serpapi_query_",
        "serpapi_location_",
        "thordata_query_",
        "thordata_location_",
        "dataforseo_query_",
        "dataforseo_location_",
        "weight_",
    )

    def _covered_by_form(key: str) -> bool:
        if key in form_fields:
            return True
        if key in _FORM_ONLY_ALLOWED:
            return True
        # Keys handled via dynamic loops — the parser iterates prefixed form
        # keys without individual _has() calls so there are no corresponding
        # static form entries to match against.
        return any(key.startswith(p) for p in _DYNAMIC_PREFIXES)

    missing_from_form = {k for k in parser_keys if not _covered_by_form(k)}

    # Partition into expected (xfail) vs unexpected (hard failure)
    expected_orphans = missing_from_form & _PARSER_ONLY_XFAIL
    unexpected_orphans = missing_from_form - _PARSER_ONLY_XFAIL

    assert not unexpected_orphans, (
        "Unexpected: _has('...') branches in settings parser with no form field:\n"
        + "\n".join(f"  - {k}" for k in sorted(unexpected_orphans))
        + "\n\nRemove the parser branch, add a form field, or add to "
        "_PARSER_ONLY_XFAIL with a comment if this is a known tracked case."
    )

    if expected_orphans:
        pytest.xfail(
            "Known orphaned parser writers pending cleanup:\n"
            + "\n".join(f"  - {k}" for k in sorted(expected_orphans))
            + "\n\nRemove from _PARSER_ONLY_XFAIL once the cleanup issue lands."
        )


# ---------------------------------------------------------------------------
# Invariant 3 — every sources.<name> has an implementation
# ---------------------------------------------------------------------------


def test_inv3_sources_have_implementations():
    """Every source declared in config.example.yaml must have a *_source.py.

    Catches sources that are documented/configurable but silently never run
    because no implementation exists (surface-without-implementation bug).

    Known unimplemented sources are in _SOURCES_UNIMPLEMENTED_XFAIL and
    tracked with xfail until the implementation lands.
    """
    source_names = _source_names_from_example_yaml()
    implemented = _get_source_module_names()

    missing = source_names - implemented

    # Partition into expected (xfail) vs unexpected (hard failure)
    expected_missing = missing & _SOURCES_UNIMPLEMENTED_XFAIL
    unexpected_missing = missing - _SOURCES_UNIMPLEMENTED_XFAIL

    assert not unexpected_missing, (
        "Sources in config.example.yaml with no *_source.py implementation:\n"
        + "\n".join(f"  - {s}" for s in sorted(unexpected_missing))
        + "\n\nAdd job_finder/sources/{name}_source.py, wire it in "
        "ingestion_runner.py, or add to _SOURCES_UNIMPLEMENTED_XFAIL with a "
        "comment if tracking is desired."
    )

    if expected_missing:
        pytest.xfail(
            "Known unimplemented sources pending fix:\n"
            + "\n".join(f"  - {s}" for s in sorted(expected_missing))
            + "\n\nRemove from _SOURCES_UNIMPLEMENTED_XFAIL once the source "
            "module and ingestion_runner.py wiring land."
        )


# ---------------------------------------------------------------------------
# Regression guard — sanity-check that the helpers return non-empty sets
# ---------------------------------------------------------------------------


def test_inv3_sanity_helpers_return_nonempty_sets():
    """Meta-guard: confirm source extraction machinery works.

    Prevents a misconfigured REPO path from silently skipping all checks by
    verifying both helpers return non-empty sets.
    """
    source_names = _source_names_from_example_yaml()
    assert source_names, (
        "No sources found in config.example.yaml — "
        "check that EXAMPLE_YAML points to the correct file."
    )
    implemented = _get_source_module_names()
    assert implemented, (
        "No *_source.py files found in job_finder/sources/ — "
        "check that SOURCES_DIR points to the correct directory."
    )
