"""Autoheal codegen — ASSEMBLE heal inputs and GENERATE a candidate recipe.

Builds a constrained prompt from corpus samples + drift signal and dispatches
through the shared ``call_model`` cascade with a strict ``output_schema`` so
the model can only return recipe-shaped JSON. The parsed response is then
re-validated by ``validate_recipe`` (strict, unknown keys raise) — codegen
returns a frozen recipe dataclass or ``None``; it never returns raw model
output.

``ProviderCascadeExhaustedError`` propagates to the caller (``run_heal``
audits it as ``no_provider``); every other malformed-output failure maps to
``None``.
"""

from __future__ import annotations

import logging
import sqlite3

from job_finder.web._field_alias import JOB_ARRAY_KEYS, JOB_TITLE_FIELDS, JOB_URL_FIELDS
from job_finder.web.autoheal.recipe_schema import (
    AtsAliasRecipe,
    HtmlRecipe,
    validate_recipe,
)
from job_finder.web.model_provider import call_model

logger = logging.getLogger(__name__)

# How many corpus samples feed the prompt, and the per-sample size cap.
# Email bodies can run to hundreds of KB; the model only needs enough of the
# structure to write selectors, and oversized prompts blow local-model context.
MAX_FAILING_SAMPLES = 3
MAX_BASELINE_SAMPLES = 2
MAX_SAMPLE_CHARS = 8_000

# ---------------------------------------------------------------------------
# Output schemas (passed to call_model so the cascade enforces structure)
# ---------------------------------------------------------------------------

_FIELD_RULE_SCHEMA = {
    "type": "object",
    "properties": {
        "selector": {"type": "string"},
        "attr": {"type": "string"},
        "regex": {"type": ["string", "null"]},
        "group": {"type": "integer"},
    },
    "required": ["selector", "attr"],
    "additionalProperties": False,
}

EMAIL_RECIPE_SCHEMA = {
    "type": "object",
    "properties": {
        "source": {"type": "string"},
        "container_selector": {"type": "string"},
        "fields": {
            "type": "object",
            "properties": {
                "title": _FIELD_RULE_SCHEMA,
                "url": _FIELD_RULE_SCHEMA,
                "company": _FIELD_RULE_SCHEMA,
                "location": _FIELD_RULE_SCHEMA,
            },
            "required": ["title", "url"],
            "additionalProperties": False,
        },
    },
    "required": ["source", "container_selector", "fields"],
    "additionalProperties": False,
}

ATS_RECIPE_SCHEMA = {
    "type": "object",
    "properties": {
        "source": {"type": "string"},
        "title_fields": {"type": "array", "items": {"type": "string"}},
        "url_fields": {"type": "array", "items": {"type": "string"}},
        "array_keys": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["source"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------------------
# ASSEMBLE
# ---------------------------------------------------------------------------


def assemble_inputs(conn: sqlite3.Connection, source: str, surface: str) -> dict:
    """Collect failing samples, prior-working baseline samples, and drift signal.

    Returns a dict with keys ``failing_samples`` (recent zero-yield raw
    texts, newest first), ``baseline_samples`` (recent positive-yield raw
    texts), and ``drift`` (source_health row excerpt). Sample texts are
    returned in full — truncation for the prompt happens in build_prompt;
    the validator needs the untruncated text.
    """
    import json as _json

    rows = conn.execute(
        "SELECT raw_text, output_json FROM corpus_sample WHERE source = ? ORDER BY id DESC",
        (source,),
    ).fetchall()

    failing: list[str] = []
    baseline: list[str] = []
    for raw_text, output_json in rows:
        try:
            snapshot = _json.loads(output_json)
            job_count = int(snapshot.get("job_count", 0))
            extractor = snapshot.get("extractor", "legacy")
        except (ValueError, TypeError, AttributeError):
            job_count = 0
            extractor = "legacy"
        if job_count == 0 and len(failing) < MAX_FAILING_SAMPLES:
            # Zero-yields are valid break evidence regardless of extractor.
            failing.append(raw_text)
        elif job_count > 0 and extractor != "override" and len(baseline) < MAX_BASELINE_SAMPLES:
            # I3 corpus provenance: override-produced positives are excluded —
            # the artifact being regression-gated must not write its own
            # ground truth. Pre-D2 samples have no extractor key → legacy.
            baseline.append(raw_text)
        if len(failing) >= MAX_FAILING_SAMPLES and len(baseline) >= MAX_BASELINE_SAMPLES:
            break

    drift: dict = {}
    health = conn.execute(
        "SELECT consecutive_breaks, baseline_yield, last_signal "
        "FROM source_health WHERE source = ?",
        (source,),
    ).fetchone()
    if health is not None:
        drift = {
            "consecutive_breaks": health[0],
            "baseline_yield": health[1],
            "last_signal": health[2],
        }

    return {"failing_samples": failing, "baseline_samples": baseline, "drift": drift}


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------


def _clip(text: str) -> str:
    if len(text) <= MAX_SAMPLE_CHARS:
        return text
    return text[:MAX_SAMPLE_CHARS] + "\n…[truncated]"


def build_prompt(surface: str, inputs: dict, source: str) -> tuple[str, list[dict]]:
    """Build (system, messages) for the recipe-generation call.

    The prompt is constrained: the model must return ONLY JSON matching the
    recipe schema for *surface*. For ATS, the canonical alias lists are
    included so the model proposes *additions*, never replacements.
    """
    drift = inputs.get("drift", {})
    failing = [_clip(s) for s in inputs.get("failing_samples", [])]
    baseline = [_clip(s) for s in inputs.get("baseline_samples", [])]

    drift_line = (
        f"Drift signal: {drift.get('consecutive_breaks', '?')} consecutive zero-yield "
        f"extractions (prior baseline {drift.get('baseline_yield', '?')} jobs/sample). "
        f"{drift.get('last_signal') or ''}"
    )

    failing_block = "\n\n".join(
        f"--- FAILING SAMPLE {i + 1} (current parser extracts 0 jobs) ---\n{s}"
        for i, s in enumerate(failing)
    )
    baseline_block = "\n\n".join(
        f"--- PRIOR-WORKING SAMPLE {i + 1} (old format, parser worked) ---\n{s}"
        for i, s in enumerate(baseline)
    )

    if surface == "email":
        system = (
            "You repair broken HTML job-alert email parsers by writing a declarative "
            "extraction recipe. Respond with ONLY JSON matching the given schema — no "
            "prose, no markdown fences. The recipe must extract jobs from the FAILING "
            "samples AND still extract jobs from the PRIOR-WORKING samples (use CSS "
            "or-selectors like '.old, .new' where the markup diverged)."
        )
        contract = (
            "Recipe JSON contract:\n"
            '{"source": "<label>", "container_selector": "<CSS selector matching one '
            'block per job>", "fields": {"title": {"selector": "...", "attr": "text"}, '
            '"url": {"selector": "a", "attr": "href"}, "company": {...}, "location": '
            "{...}}}\n"
            "fields.title and fields.url are required; map company too when present "
            "(jobs without a company are dropped). attr is 'text' or an HTML attribute "
            "name; optional 'regex' + 'group' post-process the extracted string."
        )
    elif surface == "careers":
        system = (
            "You repair broken careers-page job extractors by writing a declarative "
            "extraction recipe. The samples are rendered careers-page HTML — job "
            "links or job tiles in a listing, not alert markup. Respond with ONLY "
            "JSON matching the given schema — no prose, no markdown fences. The "
            "recipe must extract job postings from the FAILING samples AND still "
            "extract from the PRIOR-WORKING samples (use CSS or-selectors like "
            "'.old, .new' where the markup diverged)."
        )
        contract = (
            "Recipe JSON contract:\n"
            f'{{"source": "{source}", "container_selector": "<CSS selector matching '
            'one block per job tile/link>", "fields": {"title": {"selector": "...", '
            '"attr": "text"}, "url": {"selector": "a", "attr": "href"}}}\n'
            "fields.title and fields.url are required. attr is 'text' or an HTML "
            "attribute name; optional 'regex' + 'group' post-process the extracted "
            "string. Relative hrefs are resolved against the page URL automatically."
        )
    else:
        system = (
            "You repair broken ATS API field mappings by proposing ADDITIONAL field "
            "aliases. Respond with ONLY JSON matching the given schema — no prose, no "
            "markdown fences. Propose only the NEW keys seen in the failing samples; "
            "the canonical keys keep working and must not be repeated."
        )
        contract = (
            "Recipe JSON contract:\n"
            f'{{"source": "{source}", "title_fields": [...], "url_fields": [...], '
            '"array_keys": [...]}\n'
            f"Canonical title keys (already tried, in order): {JOB_TITLE_FIELDS}\n"
            f"Canonical url keys (already tried, in order): {JOB_URL_FIELDS}\n"
            f"Canonical array keys (already tried, in order): {JOB_ARRAY_KEYS}\n"
            "Return only keys NOT in the canonical lists. Empty lists are allowed for "
            "axes that did not change."
        )

    user_content = (
        f"Source: {source} (surface: {surface})\n{drift_line}\n\n"
        f"{contract}\n\n{failing_block}\n\n{baseline_block}"
    )
    return system, [{"role": "user", "content": user_content}]


# ---------------------------------------------------------------------------
# GENERATE
# ---------------------------------------------------------------------------


def generate_recipe(
    conn: sqlite3.Connection,
    config: dict,
    source: str,
    surface: str,
    inputs: dict | None = None,
) -> HtmlRecipe | AtsAliasRecipe | None:
    """Generate and strictly validate a candidate recipe for a degraded source.

    Returns the frozen recipe, or ``None`` when the model output is malformed,
    wrong-surface, or contains unknown keys. ``ProviderCascadeExhaustedError``
    from the cascade propagates (the heal pipeline audits it as no_provider).
    """
    if inputs is None:
        inputs = assemble_inputs(conn, source, surface)
    system, messages = build_prompt(surface, inputs, source)
    tier = config.get("autoheal", {}).get("heal_provider", "quick")
    schema = EMAIL_RECIPE_SCHEMA if surface in ("email", "careers") else ATS_RECIPE_SCHEMA

    result = call_model(
        tier,
        system,
        messages,
        conn,
        config,
        output_schema=schema,
        purpose="autoheal_codegen",
        max_tokens=2048,
    )

    data = result.data
    if not isinstance(data, dict) or not data:
        logger.warning("autoheal codegen: model returned non-dict output for %s", source)
        return None
    try:
        return validate_recipe(surface, data)
    except ValueError as exc:
        logger.warning("autoheal codegen: candidate recipe rejected for %s: %s", source, exc)
        return None
