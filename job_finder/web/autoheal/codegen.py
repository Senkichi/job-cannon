"""Autoheal codegen — assemble inputs, build constrained prompt, call model.

This module is the ASSEMBLE + GENERATE stage of the heal pipeline (Phase C, C3).

Three public entry points:

- ``assemble_inputs(conn, source, surface)`` — gather failing samples, corpus
  baseline samples, and drift signal from source_health.
- ``build_prompt(surface, inputs)`` — return ``(system, messages)`` for the
  constrained recipe-generation call.  For ATS surfaces the prompt includes the
  canonical ``JOB_TITLE_FIELDS`` / ``JOB_URL_FIELDS`` so the model proposes
  *additions* rather than replacements.
- ``generate_recipe(conn, config, source, surface)`` — orchestrate the above,
  call the model, parse the response, validate via ``validate_recipe``, and
  return a typed recipe or ``None`` on any failure.  Never raises.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any

from job_finder.web._field_alias import JOB_TITLE_FIELDS, JOB_URL_FIELDS
from job_finder.web.autoheal.recipe_schema import (
    AtsAliasRecipe,
    HtmlRecipe,
    validate_recipe,
)
from job_finder.web.model_provider import call_model

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# JSON schema passed as output_schema to enforce structured output from model
# ---------------------------------------------------------------------------

_HTML_RECIPE_JSON_SCHEMA: dict = {
    "type": "object",
    "required": ["source", "container_selector", "fields"],
    "additionalProperties": False,
    "properties": {
        "source": {"type": "string"},
        "container_selector": {"type": "string"},
        "fields": {
            "type": "object",
            "required": ["title", "url"],
            "additionalProperties": False,
            "properties": {
                "title": {"$ref": "#/$defs/field_rule"},
                "url": {"$ref": "#/$defs/field_rule"},
                "company": {"$ref": "#/$defs/field_rule"},
                "location": {"$ref": "#/$defs/field_rule"},
            },
        },
    },
    "$defs": {
        "field_rule": {
            "type": "object",
            "required": ["selector", "attr"],
            "additionalProperties": False,
            "properties": {
                "selector": {"type": "string"},
                "attr": {"type": "string"},
                "regex": {"type": "string"},
                "group": {"type": "integer"},
            },
        }
    },
}

_ATS_ALIAS_RECIPE_JSON_SCHEMA: dict = {
    "type": "object",
    "required": ["source", "title_fields", "url_fields", "array_keys"],
    "additionalProperties": False,
    "properties": {
        "source": {"type": "string"},
        "title_fields": {"type": "array", "items": {"type": "string"}},
        "url_fields": {"type": "array", "items": {"type": "string"}},
        "array_keys": {"type": "array", "items": {"type": "string"}},
    },
}

_SCHEMA_BY_SURFACE: dict[str, dict] = {
    "email": _HTML_RECIPE_JSON_SCHEMA,
    "ats": _ATS_ALIAS_RECIPE_JSON_SCHEMA,
}

# How many baseline (positive-yield) samples to include in the prompt.
_MAX_BASELINE_SAMPLES = 2
# How many failing (zero-yield) samples to include in the prompt.
_MAX_FAILING_SAMPLES = 2


# ---------------------------------------------------------------------------
# assemble_inputs
# ---------------------------------------------------------------------------


def assemble_inputs(conn: sqlite3.Connection, source: str, surface: str) -> dict[str, Any]:
    """Collect failing samples, baseline samples, and drift signal from source_health.

    Args:
        conn: Open SQLite connection.
        source: Source key (e.g. ``"linkedin"`` or ``"ats:lever"``).
        surface: ``"email"`` or ``"ats"``.

    Returns:
        Dict with keys ``source``, ``surface``, ``failing_samples``,
        ``baseline_samples``, ``drift_signal``.
    """
    # Pull the most recent samples for this source
    rows = conn.execute(
        "SELECT raw_text, output_json FROM corpus_sample "
        "WHERE source = ? ORDER BY id DESC LIMIT 50",
        (source,),
    ).fetchall()

    failing_samples: list[str] = []
    baseline_samples: list[str] = []

    for row in rows:
        try:
            job_count = int(json.loads(row[1]).get("job_count", 0))
        except (ValueError, TypeError):
            job_count = 0

        if job_count == 0 and len(failing_samples) < _MAX_FAILING_SAMPLES:
            failing_samples.append(row[0])
        elif job_count > 0 and len(baseline_samples) < _MAX_BASELINE_SAMPLES:
            baseline_samples.append(row[0])

        if (
            len(failing_samples) >= _MAX_FAILING_SAMPLES
            and len(baseline_samples) >= _MAX_BASELINE_SAMPLES
        ):
            break

    # Read drift signal from source_health
    health_row = conn.execute(
        "SELECT status, consecutive_breaks, baseline_yield, last_signal, heal_attempts "
        "FROM source_health WHERE source = ?",
        (source,),
    ).fetchone()

    drift_signal: dict[str, Any] = {}
    if health_row:
        drift_signal = {
            "status": health_row[0],
            "consecutive_breaks": health_row[1],
            "baseline_yield": health_row[2],
            "last_signal": health_row[3],
            "heal_attempts": health_row[4],
        }

    return {
        "source": source,
        "surface": surface,
        "failing_samples": failing_samples,
        "baseline_samples": baseline_samples,
        "drift_signal": drift_signal,
    }


# ---------------------------------------------------------------------------
# build_prompt
# ---------------------------------------------------------------------------

_EMAIL_SYSTEM = """\
You are a CSS-selector recipe generator for an HTML email job-listing parser.

Your task: given examples of working and broken HTML email bodies, produce a \
declarative JSON recipe that extracts job listings from the broken layout.

Return ONLY valid JSON matching this exact schema (no prose, no markdown fences):
{schema}

Rules:
- "container_selector": a CSS selector matching one block per job posting
- "fields": map field name → extraction rule
  - Required fields: "title" and "url"
  - Optional fields: "company", "location"
  - Each rule: {{"selector": "<css>", "attr": "<text|href|…>", "regex": null, "group": 0}}
- "attr": use "text" to extract text content, or an HTML attribute name (e.g. "href")
- Unknown top-level keys are FORBIDDEN — use exactly the keys shown above
""".strip()

_ATS_SYSTEM = """\
You are an ATS field-alias recipe generator for a job-listing API parser.

The parser already knows these canonical field keys (first-match-wins):
  JOB_TITLE_FIELDS (priority order): {title_fields}
  JOB_URL_FIELDS (priority order):   {url_fields}

Your task: given examples of failing API responses where the canonical keys are \
missing or renamed, propose ADDITIONAL alias keys that should be checked AFTER the \
canonical list. Do NOT replace or reorder the canonical keys.

Return ONLY valid JSON matching this exact schema (no prose, no markdown fences):
{schema}

Rules:
- "title_fields": list of EXTRA title keys to try (appended after canonical list)
- "url_fields": list of EXTRA url keys to try (appended after canonical list)
- "array_keys": list of EXTRA array-envelope keys to try (appended after canonical list)
- At least one list must be non-empty
- Empty string values are FORBIDDEN
- Unknown top-level keys are FORBIDDEN
""".strip()


def build_prompt(surface: str, inputs: dict[str, Any]) -> tuple[str, list[dict]]:
    """Build the constrained system + user messages for recipe generation.

    Args:
        surface: ``"email"`` or ``"ats"``.
        inputs: Dict returned by ``assemble_inputs``.

    Returns:
        ``(system_str, messages_list)`` suitable for ``call_model``.
    """
    schema = _SCHEMA_BY_SURFACE.get(surface, {})
    schema_str = json.dumps(schema, indent=2)

    if surface == "ats":
        system = _ATS_SYSTEM.format(
            title_fields=json.dumps(JOB_TITLE_FIELDS),
            url_fields=json.dumps(JOB_URL_FIELDS),
            schema=schema_str,
        )
    else:
        system = _EMAIL_SYSTEM.format(schema=schema_str)

    # Build user message with context
    parts: list[str] = [
        f"Source: {inputs['source']}",
        f"Surface: {inputs['surface']}",
    ]

    drift = inputs.get("drift_signal", {})
    if drift:
        parts.append(
            f"Drift: {drift.get('consecutive_breaks', 0)} consecutive zero-yield runs "
            f"(baseline was ~{drift.get('baseline_yield', 0):.1f} jobs/run)"
        )

    if inputs.get("baseline_samples"):
        parts.append("\n--- WORKING SAMPLE(S) (previously extracted jobs successfully) ---")
        for i, sample in enumerate(inputs["baseline_samples"], 1):
            parts.append(f"[Working sample {i}]\n{sample[:3000]}")

    if inputs.get("failing_samples"):
        parts.append("\n--- FAILING SAMPLE(S) (currently yields 0 jobs) ---")
        for i, sample in enumerate(inputs["failing_samples"], 1):
            parts.append(f"[Failing sample {i}]\n{sample[:3000]}")

    parts.append("\nReturn ONLY the JSON recipe. No explanation, no fences.")

    user_content = "\n".join(parts)

    messages = [{"role": "user", "content": user_content}]
    return system, messages


# ---------------------------------------------------------------------------
# generate_recipe
# ---------------------------------------------------------------------------


def generate_recipe(
    conn: sqlite3.Connection,
    config: dict,
    source: str,
    surface: str,
) -> HtmlRecipe | AtsAliasRecipe | None:
    """Assemble inputs, build prompt, call model, validate, return recipe or None.

    Never raises — all failures return None and are logged.

    Args:
        conn: Open SQLite connection (passed through to call_model).
        config: Application config dict.
        source: Source key (e.g. ``"linkedin"`` or ``"ats:lever"``).
        surface: ``"email"`` or ``"ats"``.

    Returns:
        A validated ``HtmlRecipe`` or ``AtsAliasRecipe``, or ``None`` on any
        failure (malformed model output, validation error, cascade exhausted).
    """
    autoheal_cfg = config.get("autoheal", {})
    tier = autoheal_cfg.get("heal_provider", "quick")
    output_schema = _SCHEMA_BY_SURFACE.get(surface)

    try:
        inputs = assemble_inputs(conn, source, surface)
        system, messages = build_prompt(surface, inputs)

        result = call_model(
            tier,
            system,
            messages,
            conn,
            config,
            output_schema=output_schema,
            purpose=f"autoheal_generate:{source}",
        )

        data = result.data
        if not isinstance(data, dict):
            logger.warning(
                "autoheal generate_recipe: model returned non-dict for source=%s surface=%s type=%s",
                source,
                surface,
                type(data).__name__,
            )
            return None

        recipe = validate_recipe(surface, data)
        return recipe

    except ValueError as exc:
        logger.warning(
            "autoheal generate_recipe: validation failed for source=%s surface=%s: %s",
            source,
            surface,
            exc,
        )
        return None
    except Exception:
        logger.exception(
            "autoheal generate_recipe: unexpected error for source=%s surface=%s",
            source,
            surface,
        )
        return None
