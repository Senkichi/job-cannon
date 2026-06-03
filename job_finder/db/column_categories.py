"""Single source of truth for the responsibility category of every `jobs` column.

The `jobs` table mixes responsibilities — parser-supplied data, system-managed
bookkeeping, scoring output, user actions, eval gold labels, and dead columns.
The schema-correspondence test (`tests/test_schema_correspondence.py`) uses this
mapping to enforce two invariants and so catch the "Pattern A" drift class
(set-on-dataclass, lost-in-persistence — e.g. `posted_date`):

  1. Every column in the live schema (`PRAGMA table_xinfo(jobs)`) is categorized
     here. Adding a column without categorizing it fails CI.
  2. Every column categorized ``"parser"`` has a matching ``ParsedJob`` field
     (and vice versa, modulo the documented non-parser ParsedJob fields).
     Adding a parser column without extending ``ParsedJob`` fails CI.

Categories:
  - ``parser``  — parser-supplied; MUST have a matching ``ParsedJob`` field.
  - ``system``  — managed by the DB / scheduler / detectors (derived keys,
                  timestamps, staleness, FK assignment, triage reason codes).
  - ``scoring`` — written by the scoring pipeline (LLM or heuristic).
  - ``user``    — set via UI actions.
  - ``eval``    — gold labels set by the eval workflow.
  - ``dead``    — vestigial; slated for removal (Phase 49 m082).

Some entries below correspond to columns that do not exist yet — they are added
in Phase 49 (``source_urls_raw``, ``salary_currency``, ``salary_period`` in m080;
``computed_status`` in m081). Categorizing them ahead of time is harmless: the
test only requires live-columns ⊆ categorized, not the reverse.

Reference: .planning/specs/2026-05-29-ingestion-contract-enforcement.md §8.2.1.
"""

from __future__ import annotations

COLUMN_CATEGORIES: dict[str, str] = {
    # ── parser-owned (must have a matching ParsedJob field) ───────────────
    "title": "parser",
    "company": "parser",
    "location": "parser",  # flat; also locations_raw / locations_structured
    "locations_raw": "parser",
    "locations_structured": "parser",
    "workplace_type": "parser",  # denormalized from locations_structured[0]
    "primary_country_code": "parser",  # denormalized from locations_structured[0]
    "sources": "parser",
    "source_urls": "parser",  # canonical (post Phase 49)
    "source_urls_raw": "parser",  # NEW in Phase 49 m080 — forensic original
    "source_id": "parser",
    "salary_min": "parser",
    "salary_max": "parser",
    "salary_currency": "parser",  # NEW in Phase 49 m080
    "salary_period": "parser",  # NEW in Phase 49 m080
    "description": "parser",
    "jd_full": "parser",
    "description_reformatted": "parser",  # arguably system (reformatter)
    "posted_date": "parser",
    # ── system-owned (managed by DB / scheduler / detector) ───────────────
    "dedup_key": "system",  # derived from (company, title)
    "first_seen": "system",
    "last_seen": "system",
    "is_stale": "system",  # stale_detector
    "expiry_status": "system",  # expiry_checker
    "expiry_checked_at": "system",
    "computed_status": "system",  # NEW in Phase 49 m081 — VIRTUAL generated column
    "company_id": "system",  # FK; assigned at upsert by company_resolver
    "enrichment_tier": "system",
    "comp_data_json": "system",  # company-research output
    "unresolved_reasons": "system",  # NEW in Phase 47 m078 — JSON reason codes
    # ── scoring-owned ─────────────────────────────────────────────────────
    "score": "scoring",
    "score_breakdown": "scoring",
    "scoring_provider": "scoring",
    "scoring_model": "scoring",
    "sub_scores_json": "scoring",
    "classification": "scoring",  # Python-derived from sub_scores
    "fit_analysis": "scoring",
    "legitimacy_note": "scoring",  # Phase 49 wires this; legitimacy_scanner writes
    # ── user-owned (set via UI actions) ───────────────────────────────────
    "user_interest": "user",
    "pipeline_status": "user",
    "notes": "user",
    # ── gold / eval (set by eval workflow) ────────────────────────────────
    "gold_classification": "eval",
    "gold_sub_scores_json": "eval",
    "gold_notes": "eval",
    "gold_labeled_at": "eval",
    "gold_no_signal_axes": "eval",
    # ── dead (Phase 49 drops these via m082) ──────────────────────────────
    "opus_score": "dead",
    "eval_blocks": "dead",
    "job_archetype": "dead",
}

# ParsedJob fields that intentionally do NOT map to a parser-owned column.
# They live on ParsedJob for transport but are categorized elsewhere:
#   - dedup_key:          derived key (system), carried so callers don't recompute it
#   - scoring_provider:   None at ingest (scoring), populated later by the scorer
#   - unresolved_reasons: triage reason codes (system), persisted to jobs.unresolved_reasons
# The schema-correspondence test exempts these from the "every ParsedJob field
# is a parser column" check.
NON_PARSER_PARSEDJOB_FIELDS: frozenset[str] = frozenset(
    {"dedup_key", "scoring_provider", "unresolved_reasons"}
)
