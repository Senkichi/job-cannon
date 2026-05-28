"""Company homepage and careers page discovery via Claude Code CLI.

Supplements the domain-guess homepage discoverer for companies where
mechanical name→domain mapping fails (multi-word names, abbreviations,
parent companies). Uses `claude -p` with structured JSON output.

Runs from a temp directory to minimize system prompt overhead.

Usage:
    from job_finder.web.claude_enricher import enrich_companies_via_claude
    results = enrich_companies_via_claude([
        {"name": "Ford Motor Company"},
        {"name": "GE HealthCare"},
    ])
"""

import json
import logging
import shutil
import subprocess
import tempfile

logger = logging.getLogger(__name__)

BATCH_SIZE = 10

_SYSTEM_PROMPT = (
    "You are a company URL lookup tool. For each company, return its official "
    'homepage URL and careers/jobs page URL. Return JSON with a "companies" array. '
    'Each element: {"name":"original name","homepage_url":"https://...",'
    '"careers_url":"https://...","company_size":"startup|small|mid-size|large",'
    '"industry":"one word or phrase"}. '
    "Only include URLs you are confident about. Omit any field you are unsure of. "
    "Do NOT guess or fabricate URLs."
)

_JSON_SCHEMA = json.dumps(
    {
        "type": "object",
        "properties": {
            "companies": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "homepage_url": {"type": "string"},
                        "careers_url": {"type": "string"},
                        "company_size": {
                            "type": "string",
                            "enum": ["startup", "small", "mid-size", "large"],
                        },
                        "industry": {"type": "string"},
                    },
                    "required": ["name"],
                },
            }
        },
        "required": ["companies"],
    }
)

# Canonical industry mapping
_INDUSTRY_CANONICAL = {
    "ai_ml": "ai & ml",
    "food_beverage": "food & beverage",
    "telecom": "telecommunications",
    "real_estate": "real estate",
    "technology": "software",
    "tech": "software",
    "financial services": "finance",
    "pharma": "healthcare",
    "biotech": "biotech",
    "pharmaceuticals": "healthcare",
}


def _normalize_industry(industry: str) -> str:
    lower = industry.lower().strip()
    return _INDUSTRY_CANONICAL.get(lower, lower)


def enrich_companies_via_claude(
    companies: list[dict],
) -> list[dict]:
    """Discover homepage/careers URLs for companies using Claude Code CLI.

    Args:
        companies: List of dicts with at least a "name" key.

    Returns:
        List of dicts with name, homepage_url, careers_url, company_size,
        industry (all optional except name).
    """
    all_results: list[dict] = []

    for batch_start in range(0, len(companies), BATCH_SIZE):
        batch = companies[batch_start : batch_start + BATCH_SIZE]
        batch_results = _classify_batch(batch)
        all_results.extend(batch_results)

    return all_results


def _classify_batch(companies: list[dict]) -> list[dict]:
    """Classify a single batch via claude CLI."""
    parts = []
    for c in companies:
        label = c["name"]
        if c.get("homepage_url"):
            label += f" (homepage: {c['homepage_url']})"
        parts.append(label)
    names_str = ", ".join(parts)
    prompt = f"Look up these companies and find their URLs: {names_str}"

    # Windows: npm exposes `claude` as `claude.CMD`. Bare "claude" passed
    # to subprocess.run (shell=False) cannot be resolved by CreateProcessW,
    # which does not honor PATHEXT. shutil.which DOES honor PATHEXT and
    # returns the full .CMD path. POSIX no-op (returns "claude" → bare).
    claude_bin = shutil.which("claude") or "claude"

    cmd = [
        claude_bin,
        "-p",
        prompt,
        "--model",
        "haiku",
        "--output-format",
        "json",
        "--no-session-persistence",
        "--system-prompt",
        _SYSTEM_PROMPT,
        "--json-schema",
        _JSON_SCHEMA,
        "--allowedTools",
        "WebSearch",
        "WebFetch",
    ]

    try:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=120,
                cwd=tmpdir,
                encoding="utf-8",
                errors="replace",
            )
    except subprocess.TimeoutExpired:
        logger.warning("Claude CLI timed out for batch: %s", names_str[:100])
        return []
    except FileNotFoundError:
        logger.error("Claude CLI not found on PATH")
        return []

    if result.returncode != 0:
        logger.warning(
            "Claude CLI failed (rc=%d): %s",
            result.returncode,
            result.stderr[:200] if result.stderr else result.stdout[:200],
        )
        return []

    try:
        response = json.loads(result.stdout)
    except json.JSONDecodeError:
        logger.warning("Failed to parse Claude CLI JSON output")
        return []

    if response.get("is_error"):
        logger.warning("Claude CLI error: %s", response.get("result", "")[:200])
        return []

    structured = response.get("structured_output", {})
    companies = structured.get("companies", [])

    for entry in companies:
        if "industry" in entry:
            entry["industry"] = _normalize_industry(entry["industry"])

    return companies
