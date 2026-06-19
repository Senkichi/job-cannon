"""Render the v3.1 ``Location facts`` user-message block (D-6, P3.3).

Design rule D-6 ("Facts beat judgment"): geography membership and remote
eligibility are deterministic facts derivable from ``locations_structured`` +
``workplace_type`` + ``primary_country_code`` against the candidate's
``target_locations`` / ``home_country``. The v3.1 prompt variant replaces the
free-text ``Location: <string>`` user-message line with a structured facts
block so the LLM reads the decided geography verdict instead of re-deriving it
from prose (the S6 failure: empty-location + 24k chars of JD → unstable
inference → spurious location_fit).

This module is a *pure* renderer: it takes already-resolved inputs (the same
ones ``compute_location_fit`` consumes) and returns one line. The
``candidate-geography-match`` token is computed by delegating to
``compute_location_fit`` so the value asserted to the LLM here is identical to
the deterministic override the orchestrator applies post-LLM
(``_apply_location_fit_override``). Mapping:

    verdict score >= 4  → "yes"   (geography is a match)
    verdict score <= 2  → "no"    (geography is ineligible)
    verdict is None      → "unknown" (facts undecided — LLM judges)

The variant is eval-gated (D-10); this renderer ships with it but only takes
effect when ``config["scoring"]["prompt_variant"] == "v3_1"``.
"""

from __future__ import annotations

from typing import Any

from job_finder.web.location_fit import compute_location_fit


def _dedup_preserve(values: list[str]) -> list[str]:
    """Order-preserving de-duplication of non-empty strings."""
    seen: set[str] = set()
    out: list[str] = []
    for v in values:
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


def render_location_facts_line(
    locations_structured: list[dict[str, Any]],
    workplace_type: str | None,
    primary_country_code: str | None,
    target_locations: list[str],
    home_country: str | None,
) -> str:
    """Render the ``Location facts: …`` line for the v3.1 user message.

    Args:
        locations_structured: Decoded ``locations_structured`` JSON (list of
            JobLocation dicts: city, region, country, country_code,
            workplace_type, unresolved). Entries with ``unresolved=True``
            contribute nothing to the displayed cities/country/workplace.
        workplace_type: Denormalized ``jobs.workplace_type`` fallback used when
            ``locations_structured`` carries no workplace signal.
        primary_country_code: Denormalized ``jobs.primary_country_code``
            fallback used when no structured country is present.
        target_locations: Candidate target locations, already adapted via
            ``location_fit.resolve_targets_and_home`` (remote sentinel synthesized).
        home_country: Candidate home country ISO code (or None).

    Returns:
        ``"Location facts: cities=[…], country=…, workplace=…, "
        "candidate-geography-match=yes|no|unknown"`` — a single line, no newline.
    """
    resolved = [loc for loc in (locations_structured or []) if not loc.get("unresolved")]

    cities = _dedup_preserve([str(loc.get("city")) for loc in resolved if loc.get("city")])
    countries = _dedup_preserve(
        [
            str(loc.get("country") or loc.get("country_code"))
            for loc in resolved
            if loc.get("country") or loc.get("country_code")
        ]
    )
    workplaces = _dedup_preserve(
        [str(loc.get("workplace_type")).upper() for loc in resolved if loc.get("workplace_type")]
    )

    # Denormalized fallbacks when the structured rows are silent on a field.
    if not countries and primary_country_code:
        countries = [str(primary_country_code)]
    if not workplaces and workplace_type:
        workplaces = [str(workplace_type).upper()]

    cities_str = ", ".join(cities) if cities else "(none)"
    country_str = ", ".join(countries) if countries else "(none)"
    workplace_str = "|".join(workplaces) if workplaces else "UNSPECIFIED"

    verdict = compute_location_fit(
        locations_structured=locations_structured,
        workplace_type=workplace_type,
        primary_country_code=primary_country_code,
        target_locations=target_locations,
        home_country=home_country,
    )
    if verdict is None:
        match = "unknown"
    else:
        score, _reason = verdict
        match = "yes" if score >= 4 else ("no" if score <= 2 else "unknown")

    return (
        f"Location facts: cities=[{cities_str}], country={country_str}, "
        f"workplace={workplace_str}, candidate-geography-match={match}"
    )


__all__ = ["render_location_facts_line"]
