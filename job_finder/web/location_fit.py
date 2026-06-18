"""Deterministic location_fit override from structured location facts (P3.1).

Design rule D-6: "Facts beat judgment." Geography membership, remote
eligibility, and country exclusion are *deterministic* facts derivable from
``locations_structured`` + ``primary_country_code`` + ``workplace_type`` +
the candidate's ``target_locations`` and ``home_country``. This module
computes them in Python and returns an override score (int 1–5) + rationale
string that replaces the LLM-emitted ``location_fit`` sub-score when the
facts decide the outcome unambiguously.

The override runs **post-LLM, pre-persist** in the scoring orchestrator
(``score_and_persist_job``): schema unchanged, ``derive_classification``
unchanged, no prompt change → **no eval gate needed**. The eval harness
measures the model; this override is downstream policy that applies
deterministically on top.

Rule table — first matching row wins; returns ``None`` when no row fires
(the LLM judgment is authoritative for the undecided cases):

    Row 1: any REMOTE location, unrestricted, 'Remote' ∈ targets
           → (5, "fully remote, remote targeted")
    Row 2: any REMOTE location restricted to home_country     [†home_country]
           → (5, "fully remote, remote targeted")
    Row 3: all REMOTE locations restricted to countries ≠ home_country [†]
           → (1, "remote but ineligible geography")
    Row 4: all locations onsite/hybrid/UNSPECIFIED, countries ≠ home_country,
           no target_location matches any city/region             [†]
           → (1, "on-site outside candidate geography")
    Row 5: any location's city/region/country matches a non-Remote target
           → (5, "on-site/hybrid in target geography")
    → None: LLM judges (e.g. onsite home-country city not in targets)

Multi-location rule: best location wins — a job offerable in NYC *or* Toronto
is as good as its best option for the candidate.
Unresolved entries (``unresolved=True``) contribute nothing.

†-rows fire only when ``home_country`` is present (non-None, non-empty).

Reference: issue #390, §P3.1.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _norm(value: str | None) -> str:
    """Normalize a string for case-insensitive comparison."""
    return (value or "").strip().lower()


def _remote_unrestricted(loc: dict[str, Any]) -> bool:
    """True iff a location dict represents an unrestricted remote posting.

    "Unrestricted" means the location carries no country constraint — the
    country_code is None/empty. A REMOTE posting with an explicit country
    (e.g. ``{workplace_type: "REMOTE", country_code: "US"}``) is
    *country-restricted* and falls through to row 2 / row 3.
    """
    return _norm(loc.get("workplace_type")) == "remote" and not loc.get("country_code")


def _remote_in_country(loc: dict[str, Any], country_code: str) -> bool:
    """True iff the location is REMOTE and restricted to ``country_code``."""
    return _norm(loc.get("workplace_type")) == "remote" and _norm(
        loc.get("country_code")
    ) == _norm(country_code)


def _remote_outside_country(loc: dict[str, Any], country_code: str) -> bool:
    """True iff the location is REMOTE and restricted to a country ≠ home."""
    cc = loc.get("country_code")
    return (
        _norm(loc.get("workplace_type")) == "remote"
        and bool(cc)  # must have a country restriction to be "outside"
        and _norm(cc) != _norm(country_code)
    )


def _is_remote(loc: dict[str, Any]) -> bool:
    """True iff location has any REMOTE workplace_type."""
    return _norm(loc.get("workplace_type")) == "remote"


def _onsite_or_hybrid_or_unspecified(loc: dict[str, Any]) -> bool:
    """True iff location is onsite/hybrid/UNSPECIFIED (non-remote)."""
    wt = _norm(loc.get("workplace_type"))
    return wt in ("onsite", "hybrid", "unspecified", "")


def _country_outside_home(loc: dict[str, Any], home_country_code: str) -> bool:
    """True iff location has a country that differs from home_country."""
    cc = loc.get("country_code")
    if not cc:
        # No country info — cannot confirm it is "outside"; treat as ambiguous.
        return False
    return _norm(cc) != _norm(home_country_code)


def _target_loc_matches(loc: dict[str, Any], target_locations: list[str]) -> bool:
    """True iff any non-Remote target_location matches the city, region, or country.

    Matching is case-insensitive substring/equality. The "Remote" token is
    explicitly excluded — it is a modality signal, not a geography membership
    test.  Row 5 fires on a geographic match; row 1 already covers the pure
    remote case.
    """
    geo_targets = [t for t in target_locations if _norm(t) != "remote"]
    city = _norm(loc.get("city"))
    region = _norm(loc.get("region"))
    country = _norm(loc.get("country"))

    for target in geo_targets:
        t = _norm(target)
        if not t:
            continue
        # Substring match: target "San Francisco" matches city "San Francisco",
        # and "California" matches region "California". Also handles cases
        # where the target is a country ("United States") matching country.
        if city and t in city:
            return True
        if region and t in region:
            return True
        if country and t in country:
            return True
        # Reverse check: city/region/country substring in target (e.g. target
        # "New York, NY" and city "New York").
        if city and city in t:
            return True
        if region and region in t:
            return True
    return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def compute_location_fit(
    locations_structured: list[dict[str, Any]],
    workplace_type: str | None,
    primary_country_code: str | None,
    target_locations: list[str],
    home_country: str | None,
) -> tuple[int, str] | None:
    """Deterministic location_fit when structured facts decide it; None → LLM judges.

    Args:
        locations_structured: Parsed JobLocation objects serialized to dicts
            (the ``locations_structured`` DB column, decoded from JSON).
            Each dict has: city, region, region_code, country, country_code,
            workplace_type, raw, unresolved. Entries with ``unresolved=True``
            contribute nothing.
        workplace_type: Denormalized ``jobs.workplace_type`` column value
            (e.g. "REMOTE", "ONSITE", "HYBRID", "UNSPECIFIED"). Used as a
            fallback when ``locations_structured`` is empty.
        primary_country_code: Denormalized ``jobs.primary_country_code`` column
            value. Used as a fallback for country when ``locations_structured``
            is empty.
        target_locations: Candidate's ``profile.target_locations`` list from
            config. Typically contains items like "Remote", "San Francisco",
            "New York".
        home_country: Candidate's ``profile.home_country`` ISO country code
            (e.g. "US"). Optional; rows marked † in the rule table require
            this to fire. When None/empty, those rows are silently skipped.

    Returns:
        ``(score: int, reason: str)`` when facts are decisive, ``None`` when
        the LLM should judge (ambiguous / insufficient data).

    Rule table (first match wins; D-6 — D-10 cite):
        Row 1: any REMOTE, unrestricted, 'Remote' ∈ targets
               → (5, "fully remote, remote targeted")
        Row 2: any REMOTE restricted to home_country        [† needs home_country]
               → (5, "fully remote, remote targeted")
        Row 3: all REMOTE restricted to countries ≠ home_country [†]
               → (1, "remote but ineligible geography")
        Row 4: all onsite/hybrid/UNSPECIFIED in countries ≠ home_country,
               no target_loc city/region match              [†]
               → (1, "on-site outside candidate geography")
        Row 5: any city/region/country matches a non-Remote target
               → (5, "on-site/hybrid in target geography")
        otherwise → None
    """
    target_locations = target_locations or []
    home = (home_country or "").strip().upper() or None

    # Build the resolved (non-unresolved) location list.
    resolved: list[dict[str, Any]] = [
        loc for loc in (locations_structured or []) if not loc.get("unresolved")
    ]

    # When locations_structured is empty or all unresolved, synthesize a
    # single pseudo-entry from the denormalized columns so the rules can
    # still fire on the available data (e.g. the data_enricher LLM extract
    # path that writes workplace_type/primary_country_code before the full
    # structured parse runs).
    if not resolved and (workplace_type or primary_country_code):
        resolved = [
            {
                "workplace_type": (workplace_type or "UNSPECIFIED").upper(),
                "country_code": primary_country_code or None,
                "city": None,
                "region": None,
                "country": None,
                "unresolved": False,
            }
        ]

    if not resolved:
        # No structured facts at all — LLM judges.
        return None

    remote_in_targets = any(_norm(t) == "remote" for t in target_locations)

    # ------------------------------------------------------------------
    # Row 1: any REMOTE, unrestricted (no country), 'Remote' ∈ targets
    # ------------------------------------------------------------------
    if remote_in_targets and any(_remote_unrestricted(loc) for loc in resolved):
        return (5, "fully remote, remote targeted")

    # ------------------------------------------------------------------
    # Row 2: any REMOTE restricted to home_country       [†home_country]
    # "Restricted to home_country" means country_code == home — the job is
    # remote but only for residents of the candidate's country.
    # ------------------------------------------------------------------
    if home and remote_in_targets:
        if any(_remote_in_country(loc, home) for loc in resolved):
            return (5, "fully remote, remote targeted")

    # ------------------------------------------------------------------
    # Row 3: ALL remote locations are restricted to countries ≠ home_country
    # Fires only when every location is REMOTE AND every REMOTE has an
    # explicit country ≠ home. A mix of REMOTE+onsite, or any unrestricted
    # REMOTE, falls through.
    # ------------------------------------------------------------------
    if home:
        remote_locs = [loc for loc in resolved if _is_remote(loc)]
        if remote_locs and all(_remote_outside_country(loc, home) for loc in remote_locs):
            # Only fires when ALL resolved are remote-and-outside — no onsite
            # fallback location exists that could override.
            non_remote = [loc for loc in resolved if not _is_remote(loc)]
            if not non_remote:
                return (1, "remote but ineligible geography")

    # ------------------------------------------------------------------
    # Row 4: all locations onsite/hybrid/UNSPECIFIED, every one has a country
    # ≠ home_country, AND no target_location city/region matches   [†]
    # ------------------------------------------------------------------
    if home:
        non_remote_locs = [loc for loc in resolved if _onsite_or_hybrid_or_unspecified(loc)]
        if non_remote_locs and len(non_remote_locs) == len(resolved):
            # Every resolved location is non-remote.
            all_outside = all(_country_outside_home(loc, home) for loc in non_remote_locs)
            if all_outside:
                no_geo_match = not any(
                    _target_loc_matches(loc, target_locations) for loc in non_remote_locs
                )
                if no_geo_match:
                    return (1, "on-site outside candidate geography")

    # ------------------------------------------------------------------
    # Row 5: any location's city/region/country matches a non-Remote target
    # ------------------------------------------------------------------
    if any(_target_loc_matches(loc, target_locations) for loc in resolved):
        return (5, "on-site/hybrid in target geography")

    # No rule fired — LLM judges (e.g. onsite home-country city not in targets:
    # desirability requires judgment).
    return None
