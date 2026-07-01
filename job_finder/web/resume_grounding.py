"""Never-fabricate validator: subset grounding + title-alignment allowlist + JD keyword-coverage.

Pure, deterministic validation that tailored resumes contain ONLY facts grounded in
experience_profile.json, respect prohibited-item hard-stops, and honor the owner's
title-variant allowlist for the most-recent position.

Public API:
    validate_resume_grounding(tailored, profile, job) -> GroundingReport
    adjudicate_resume_prose(tailored, profile, config, conn) -> tuple[FabricationViolation, ...]

Dataclasses:
    FabricationViolation(kind, value, section)
    KeywordCoverage(jd_keywords, present, missing, ratio)
    GroundingReport(violations, coverage)
"""

import logging
import re
import sqlite3
from dataclasses import dataclass

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FabricationViolation:
    """A single grounding violation found in a tailored resume.

    kind: The violation type:
        - "company": fabricated employer
        - "title": fabricated job title
        - "dates": invented year
        - "degree": fabricated degree
        - "institution": fabricated institution
        - "skill": fabricated skill
        - "prohibited_item": hard-stop prohibited token/phrase
        - "title_unlisted": most-recent title not in allowlist
        - "prose_fabrication": LLM-detected fabrication in prose (bullets/summary)
        - "adjudicator_unavailable": prose adjudicator failed (fail-closed)
    value: The offending token/phrase as it appeared in the tailored resume.
    section: Human locator, e.g. "sections[2]", "education", or "summary".
    """

    kind: str
    value: str
    section: str


@dataclass(frozen=True)
class KeywordCoverage:
    """Deterministic JD keyword coverage metric.

    jd_keywords: Extracted JD keywords (from tailored["jd_keywords"]).
    present: Keywords found (truthfully) in the tailored text.
    missing: Keywords with no truthful home (honest gaps).
    ratio: len(present) / len(jd_keywords), 0.0 if no keywords.
    """

    jd_keywords: tuple[str, ...]
    present: tuple[str, ...]
    missing: tuple[str, ...]
    ratio: float


@dataclass(frozen=True)
class GroundingReport:
    """Complete grounding validation result.

    violations: Tuple of all FabricationViolation objects from layers A/B/C.
    coverage: KeywordCoverage metric from layer D (reported, never a refusal reason).
    """

    violations: tuple[FabricationViolation, ...]
    coverage: KeywordCoverage


# ---------------------------------------------------------------------------
# Normalization helpers
# ---------------------------------------------------------------------------


def _normalize_company(company: str) -> str:
    """Normalize company name for comparison (delegates to dedup_normalizer)."""
    from job_finder.web.dedup_normalizer import normalize_company

    return normalize_company(company)


def _normalize_company_for_grounding(company: str) -> str:
    """Normalize company name for grounding: casefold + whitespace collapse only.

    Unlike the dedup normalizer, this does NOT strip legal/suffix words (inc, llc, corp, etc.)
    because grounding requires exact string equivalence, not dedup-style fuzzy matching.
    """
    return " ".join(company.casefold().split())


def _normalize_text(text: str) -> str:
    """Light normalization for titles/degrees/institutions/keywords."""
    return " ".join(text.lower().split())


def _extract_years(dates_str: str) -> set[str]:
    """Extract 4-digit years from a dates string."""
    return set(re.findall(r"\b(?:19|20)\d{2}\b", dates_str))


# ---------------------------------------------------------------------------
# Layer A: Structural subset check
# ---------------------------------------------------------------------------


def _build_profile_fact_sets(profile: dict) -> dict[str, set]:
    """Build normalized fact sets from profile for membership testing.

    Returns dict with keys:
        - companies: set of normalized company names (casefold + whitespace only)
        - titles: set of normalized titles (all positions + title_variants)
        - title_by_company: {normalized_company: set of normalized titles}
        - years_by_company: {normalized_company: set of year strings}
        - degrees: set of normalized degree strings
        - institutions: set of normalized institution strings
        - skills: set of normalized skills (profile + position skills)
    """
    companies = set()
    titles = set()
    title_by_company: dict[str, set] = {}
    years_by_company: dict[str, set] = {}
    skills = set()

    for position in profile.get("positions", []):
        company = position.get("company", "")
        title = position.get("title", "")
        start_date = position.get("start_date", "")
        end_date = position.get("end_date") or ""
        title_variants = position.get("title_variants", [])
        position_skills = position.get("skills", [])

        norm_company = _normalize_company_for_grounding(company)
        norm_title = _normalize_text(title)

        companies.add(norm_company)
        titles.add(norm_title)
        title_by_company.setdefault(norm_company, set()).add(norm_title)

        # Add title variants to the titles set
        if isinstance(title_variants, list):
            for variant in title_variants:
                if isinstance(variant, str):
                    norm_variant = _normalize_text(variant)
                    titles.add(norm_variant)
                    title_by_company.setdefault(norm_company, set()).add(norm_variant)

        # Extract years from date strings
        date_years = set(re.findall(r"\b(?:19|20)\d{2}\b", f"{start_date} {end_date}"))
        years_by_company.setdefault(norm_company, set()).update(date_years)

        # Add position skills
        if isinstance(position_skills, list):
            for skill in position_skills:
                if isinstance(skill, str):
                    skills.add(_normalize_text(skill))

    # Add profile-level skills
    profile_skills = profile.get("skills", [])
    if isinstance(profile_skills, list):
        for skill in profile_skills:
            if isinstance(skill, str):
                skills.add(_normalize_text(skill))

    # Education facts
    degrees = set()
    institutions = set()
    for edu in profile.get("education", []):
        degree = edu.get("degree", "")
        institution = edu.get("institution", "")
        if degree:
            degrees.add(_normalize_text(degree))
        if institution:
            institutions.add(_normalize_text(institution))

    return {
        "companies": companies,
        "titles": titles,
        "title_by_company": title_by_company,
        "years_by_company": years_by_company,
        "degrees": degrees,
        "institutions": institutions,
        "skills": skills,
    }


def _check_structural_subset(
    tailored: dict, profile_facts: dict[str, set]
) -> list[FabricationViolation]:
    """Layer A: Check that every structural fact in tailored is grounded in profile.

    Omission is allowed (tailoring drops irrelevant roles). Addition is fabrication.
    """
    violations = []

    # Build union of all profile years for blank/ungrounded company check
    all_profile_years = set()
    for company_years in profile_facts["years_by_company"].values():
        all_profile_years.update(company_years)

    for idx, section in enumerate(tailored.get("sections", [])):
        section_locator = f"sections[{idx}]"

        # Check company (use grounding normalizer: casefold + whitespace only)
        company = section.get("company", "")
        norm_company = _normalize_company_for_grounding(company) if company else ""
        company_is_grounded = company and norm_company in profile_facts["companies"]

        if company and not company_is_grounded:
            violations.append(
                FabricationViolation(kind="company", value=company, section=section_locator)
            )

        # Check title (per-company validation, not global)
        title = section.get("title", "")
        if title:
            norm_title = _normalize_text(title)
            # Title must belong to THIS section's company's allowlist
            if company_is_grounded:
                company_titles = profile_facts["title_by_company"].get(norm_company, set())
                if norm_title not in company_titles:
                    violations.append(
                        FabricationViolation(kind="title", value=title, section=section_locator)
                    )
            else:
                # If company is ungrounded, title must be in global titles set
                if norm_title not in profile_facts["titles"]:
                    violations.append(
                        FabricationViolation(kind="title", value=title, section=section_locator)
                    )

        # Check dates (no invented years)
        dates = section.get("dates", "")
        if dates:
            tailored_years = _extract_years(dates)

            if company_is_grounded:
                # Check against this company's years
                profile_years = profile_facts["years_by_company"].get(norm_company, set())
                allowed_years = profile_years | {"present", "current"}
                for year in tailored_years:
                    if year not in allowed_years:
                        violations.append(
                            FabricationViolation(kind="dates", value=year, section=section_locator)
                        )
            else:
                # Blank or ungrounded company: check against union of all profile years
                allowed_years = all_profile_years | {"present", "current"}
                for year in tailored_years:
                    if year not in allowed_years:
                        violations.append(
                            FabricationViolation(kind="dates", value=year, section=section_locator)
                        )

    # Check skills (every tailored skill must be grounded)
    for skill in tailored.get("skills", []):
        if isinstance(skill, str):
            norm_skill = _normalize_text(skill)
            if norm_skill not in profile_facts["skills"]:
                violations.append(
                    FabricationViolation(kind="skill", value=skill, section="skills")
                )

    # Check education
    for edu in tailored.get("education", []):
        degree = edu.get("degree", "")
        institution = edu.get("institution", "")

        if degree:
            norm_degree = _normalize_text(degree)
            if norm_degree not in profile_facts["degrees"]:
                violations.append(
                    FabricationViolation(kind="degree", value=degree, section="education")
                )

        if institution:
            norm_inst = _normalize_text(institution)
            if norm_inst not in profile_facts["institutions"]:
                violations.append(
                    FabricationViolation(
                        kind="institution", value=institution, section="education"
                    )
                )

    return violations


# ---------------------------------------------------------------------------
# Layer B: Prohibited items scan
# ---------------------------------------------------------------------------


def _check_prohibited_items(
    tailored: dict, profile: dict, job: dict, style_guide: dict | None = None
) -> list[FabricationViolation]:
    """Layer B: Deterministic scan for mechanically-checkable hard-stops.

    Configuration is read from style_guide['prohibited_items'] if provided,
    otherwise uses hardcoded defaults (for backward compatibility).
    """
    violations = []

    # Build concatenated text for scanning
    summary = tailored.get("summary", "")
    skills_text = " ".join(tailored.get("skills", []))
    bullets = []
    for section in tailored.get("sections", []):
        bullets.extend(section.get("bullets", []))
    bullets_text = " ".join(bullets)
    full_text = f"{summary} {skills_text} {bullets_text}".lower()

    # Read configuration from style_guide or use defaults
    if style_guide and isinstance(style_guide, dict):
        prohibited_config = style_guide.get("prohibited_items", {})
    else:
        # Hardcoded defaults for backward compatibility
        prohibited_config = {
            "banned_tokens": ["dbt", "spark", "apache spark"],
            "roi_forbidden_pct": 454,
            "roi_allowed_pct": 350,
            "min_years_anchor": 8,
            "ban_company_name_in_summary": True,
            "ban_sample_sizes": True,
            "ban_em_dash_family": True,
            "ban_third_person": True,
        }

    # 1. Banned tokens (case-insensitive word boundary)
    banned_tokens = prohibited_config.get("banned_tokens", [])
    if isinstance(banned_tokens, list):
        for token in banned_tokens:
            if isinstance(token, str) and token:
                # Check for word boundary match
                if re.search(rf"\b{re.escape(token.lower())}\b", full_text):
                    violations.append(
                        FabricationViolation(
                            kind="prohibited_item", value=token, section="summary/skills/bullets"
                        )
                    )

    # 2. Company name in Professional Summary
    if prohibited_config.get("ban_company_name_in_summary", True):
        target_company = job.get("company", "")
        if target_company:
            norm_target = _normalize_company_for_grounding(target_company)
            norm_summary = _normalize_company_for_grounding(summary)
            if norm_target in norm_summary:
                violations.append(
                    FabricationViolation(
                        kind="prohibited_item", value=target_company, section="summary"
                    )
                )

    # 3. Sample sizes (N=X)
    if prohibited_config.get("ban_sample_sizes", True):
        if re.search(r"\bN\s*=\s*\d", full_text, re.IGNORECASE):
            violations.append(
                FabricationViolation(kind="prohibited_item", value="N=", section="bullets")
            )

    # 4. Em dashes or en dashes in full text (U+2012..U+2015)
    if prohibited_config.get("ban_em_dash_family", True):
        if re.search(r"[‒–—―]", full_text):
            violations.append(
                FabricationViolation(
                    kind="prohibited_item", value="dash", section="summary/skills/bullets"
                )
            )

    # 5. ROI figure (forbidden vs allowed percentage)
    roi_forbidden = prohibited_config.get("roi_forbidden_pct")
    if roi_forbidden is not None:
        if re.search(rf"{roi_forbidden}\s*%", full_text):
            violations.append(
                FabricationViolation(
                    kind="prohibited_item", value=f"{roi_forbidden}%", section="bullets"
                )
            )

    # 6. Years-of-experience figure smaller than min_years_anchor
    min_years = prohibited_config.get("min_years_anchor")
    if min_years is not None:
        for year_match in re.finditer(r"\b(\d+)\+?\s*(?:years|yrs)\b", full_text, re.IGNORECASE):
            years = int(year_match.group(1))
            if years < min_years:
                violations.append(
                    FabricationViolation(
                        kind="prohibited_item", value=f"{years} years", section="summary/bullets"
                    )
                )

    # 7. Third-person self-reference
    if prohibited_config.get("ban_third_person", True):
        owner_first_name = _derive_owner_first_name(profile)
        if owner_first_name:
            # Word-boundary match on first name as standalone token
            if re.search(rf"\b{re.escape(owner_first_name.lower())}\b", full_text):
                violations.append(
                    FabricationViolation(
                        kind="prohibited_item", value=owner_first_name, section="summary/bullets"
                    )
                )

        # Check for third-person pronouns (without literal space requirement)
        if re.search(r"\b(?:he|his|him|she|her|hers|they|their)\b", full_text):
            violations.append(
                FabricationViolation(
                    kind="prohibited_item", value="third-person", section="summary/bullets"
                )
            )

    return violations


def _derive_owner_first_name(profile: dict) -> str:
    """Derive owner's first name from profile (best-effort, fallback empty)."""
    # Try to derive from contact.full_name first
    contact = profile.get("contact", {})
    full_name = contact.get("full_name", "")
    if full_name and isinstance(full_name, str):
        # First token is the first name
        first_name = full_name.split()[0]
        return first_name

    # Fallback: try to derive from most recent position's context
    # This is a best-effort heuristic; the real name should be in config if needed
    return ""


# ---------------------------------------------------------------------------
# Layer C: Title-alignment allowlist enforcement
# ---------------------------------------------------------------------------


def _identify_most_recent_position(profile: dict) -> dict | None:
    """Identify the most-recent position from profile.positions.

    Rules:
        - end_date is null/"present"/"current" → most-recent
        - Else latest start_date
        - Among concurrent current roles, break ties by latest start_date (month-aware, not document order)
    """
    positions = profile.get("positions", [])
    if not positions:
        return None

    most_recent = None
    most_recent_score = -1

    for idx, position in enumerate(positions):
        end_date = position.get("end_date") or ""
        start_date = position.get("start_date", "")

        # Score: current positions first, then by start_date
        if not end_date or end_date.lower() in ("present", "current"):
            # Extract year and month from start_date for month-aware tiebreaking
            # Parse month if present (e.g., "Nov 2023" vs "Jan 2023")
            start_year_match = re.search(r"\b(19|20)\d{2}\b", start_date)
            start_year = int(start_year_match.group()) if start_year_match else 0

            # Try to extract month (3-letter abbreviation or numeric)
            month_score = 0
            month_match = re.search(
                r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\b",
                start_date,
                re.IGNORECASE,
            )
            if month_match:
                month_str = month_match.group().capitalize()
                month_map = {
                    "Jan": 1,
                    "Feb": 2,
                    "Mar": 3,
                    "Apr": 4,
                    "May": 5,
                    "Jun": 6,
                    "Jul": 7,
                    "Aug": 8,
                    "Sep": 9,
                    "Oct": 10,
                    "Nov": 11,
                    "Dec": 12,
                }
                month_score = month_map.get(month_str, 0)

            # Base 1M + year*12 + month for month-aware tiebreak
            score = 1000000 + (start_year * 12) + month_score
        else:
            # Extract year from start_date for ordering
            start_year_match = re.search(r"\b(19|20)\d{2}\b", start_date)
            start_year = int(start_year_match.group()) if start_year_match else 0
            score = start_year

        # Document order as final tiebreaker (higher index = lower priority)
        score -= idx * 0.001

        if score > most_recent_score:
            most_recent_score = score
            most_recent = position

    return most_recent


def _check_title_allowlist(tailored: dict, profile: dict) -> list[FabricationViolation]:
    """Layer C: Enforce title-alignment allowlist for most-recent position."""
    violations = []

    most_recent = _identify_most_recent_position(profile)
    if not most_recent:
        return violations  # No positions to validate against

    sections = tailored.get("sections", [])
    if not sections:
        return violations

    # Identify the most-recent tailored section by matching company/dates to profile's most-recent
    most_recent_norm_company = _normalize_company_for_grounding(most_recent.get("company", ""))

    most_recent_section_idx = None
    for idx, section in enumerate(sections):
        section_company = section.get("company", "")
        norm_section_company = _normalize_company_for_grounding(section_company)

        # Match by company (dates matching is harder since sections use dates string)
        if norm_section_company == most_recent_norm_company:
            most_recent_section_idx = idx
            break

    # If most-recent company is NOT present in tailored sections, do NOT fall back to index 0.
    # Instead, validate each section's title against its own company's allowlist (the older-position path).
    if most_recent_section_idx is None:
        # No most-recent company match → validate all sections as older positions
        for idx, section in enumerate(sections):
            section_title = section.get("title", "")
            if not section_title:
                continue

            norm_section_title = _normalize_text(section_title)
            section_company = section.get("company", "")
            norm_section_company = _normalize_company_for_grounding(section_company)

            # Find matching position in profile
            matching_position = None
            for position in profile.get("positions", []):
                if (
                    _normalize_company_for_grounding(position.get("company", ""))
                    == norm_section_company
                ):
                    matching_position = position
                    break

            if matching_position:
                pos_canonical = _normalize_text(matching_position.get("title", ""))
                pos_variants = matching_position.get("title_variants", [])
                if isinstance(pos_variants, list):
                    pos_norm_variants = {
                        _normalize_text(v) for v in pos_variants if isinstance(v, str)
                    }
                else:
                    pos_norm_variants = set()

                pos_admissible = {pos_canonical} | pos_norm_variants
                if norm_section_title not in pos_admissible:
                    violations.append(
                        FabricationViolation(
                            kind="title", value=section_title, section=f"sections[{idx}]"
                        )
                    )

        return violations

    # Validate most-recent section against its allowlist
    most_recent_section = sections[most_recent_section_idx]
    tailored_title = most_recent_section.get("title", "")
    if tailored_title:
        norm_tailored_title = _normalize_text(tailored_title)

        # Build admissible set for most-recent position
        canonical_title = most_recent.get("title", "")
        norm_canonical = _normalize_text(canonical_title)

        title_variants = most_recent.get("title_variants", [])
        if isinstance(title_variants, list):
            norm_variants = {_normalize_text(v) for v in title_variants if isinstance(v, str)}
        else:
            # Malformed variants → collapse to canonical only
            norm_variants = set()

        admissible_set = {norm_canonical} | norm_variants

        # Check if most-recent tailored title is in admissible set
        if norm_tailored_title not in admissible_set:
            violations.append(
                FabricationViolation(
                    kind="title_unlisted",
                    value=tailored_title,
                    section=f"sections[{most_recent_section_idx}]",
                )
            )

    # Older positions: title must equal canonical or be in that position's variants
    for idx, section in enumerate(sections):
        if idx == most_recent_section_idx:
            continue  # Skip most-recent, already validated

        section_title = section.get("title", "")
        if not section_title:
            continue

        norm_section_title = _normalize_text(section_title)
        section_company = section.get("company", "")
        norm_section_company = _normalize_company_for_grounding(section_company)

        # Find matching position in profile
        matching_position = None
        for position in profile.get("positions", []):
            if (
                _normalize_company_for_grounding(position.get("company", ""))
                == norm_section_company
            ):
                matching_position = position
                break

        if matching_position:
            pos_canonical = _normalize_text(matching_position.get("title", ""))
            pos_variants = matching_position.get("title_variants", [])
            if isinstance(pos_variants, list):
                pos_norm_variants = {
                    _normalize_text(v) for v in pos_variants if isinstance(v, str)
                }
            else:
                pos_norm_variants = set()

            pos_admissible = {pos_canonical} | pos_norm_variants
            if norm_section_title not in pos_admissible:
                violations.append(
                    FabricationViolation(
                        kind="title", value=section_title, section=f"sections[{idx}]"
                    )
                )

    return violations


# ---------------------------------------------------------------------------
# Layer D: JD keyword coverage metric
# ---------------------------------------------------------------------------


def _compute_keyword_coverage(tailored: dict) -> KeywordCoverage:
    """Layer D: Compute deterministic JD keyword coverage metric."""
    jd_keywords = tailored.get("jd_keywords", [])
    if not jd_keywords:
        return KeywordCoverage(jd_keywords=(), present=(), missing=(), ratio=0.0)

    # Build concatenated tailored text
    summary = tailored.get("summary", "")
    skills_text = " ".join(tailored.get("skills", []))
    bullets = []
    for section in tailored.get("sections", []):
        bullets.extend(section.get("bullets", []))
    bullets_text = " ".join(bullets)
    full_text = _normalize_text(f"{summary} {skills_text} {bullets_text}")

    present = []
    missing = []

    for keyword in jd_keywords:
        norm_keyword = _normalize_text(keyword)
        # Check if keyword appears as token or substring
        if norm_keyword in full_text or norm_keyword.replace(" ", "") in full_text.replace(
            " ", ""
        ):
            present.append(keyword)
        else:
            missing.append(keyword)

    ratio = len(present) / len(jd_keywords) if jd_keywords else 0.0

    return KeywordCoverage(
        jd_keywords=tuple(jd_keywords),
        present=tuple(present),
        missing=tuple(missing),
        ratio=ratio,
    )


# ---------------------------------------------------------------------------
# Prose adjudicator (fail-closed LLM check for bullets/summary)
# ---------------------------------------------------------------------------


_PROSE_ADJUDICATION_SCHEMA = {
    "type": "object",
    "properties": {
        "fabrications": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "claim": {"type": "string", "description": "The fabricated claim text"},
                    "type": {
                        "type": "string",
                        "description": "Type of fabrication: employer, job_title, certification, metric, tool_skill, tenure, or other",
                    },
                    "section": {
                        "type": "string",
                        "description": "Locator: 'summary' or 'sections[N]'",
                    },
                },
                "required": ["claim", "type", "section"],
            },
        }
    },
    "required": ["fabrications"],
    "additionalProperties": True,
}

_PROSE_ADJUDICATION_SYSTEM = (
    "You are a strict fact-checker validating that a resume's prose (summary and bullet points) "
    "contains ONLY facts grounded in the candidate's TRUE profile. "
    "Identify EVERY concrete claim in the prose that asserts a fact NOT supported by the ground truth. "
    "Specifically flag fabricated: EMPLOYERS, JOB TITLES, CERTIFICATIONS/credentials, quantified METRICS, "
    "named TOOLS/SKILLS, or TENURES (dates/durations). "
    "Claims that are rephrasings or re-emphasis of ground-truth achievements are ALLOWED. "
    "Respond with JSON only."
)


def adjudicate_resume_prose(
    tailored: dict, profile: dict, config: dict, conn: sqlite3.Connection
) -> tuple[FabricationViolation, ...]:
    """Fail-closed LLM adjudicator for resume prose (summary + bullets).

    This is an I/O function (dispatches call_model) and must be called AFTER
    the pure validate_resume_grounding check. It grounds the free-text prose
    (summary and bullets) against the structured profile facts, catching
    fabrications that the deterministic validator cannot detect.

    Ground truth corpus = the STRUCTURED profile only:
        - Every position's company/title/start_date/end_date/achievements[]
        - Top-level skills[] ∪ each position's skills[]
        - education[] (degree/institution)

    Prose under test = tailored['summary'] + every section's bullets.

    FAIL CLOSED: if call_model raises, returns empty, or returns unparseable output,
    treat it as a BLOCKING violation (kind="adjudicator_unavailable") — the resume
    must NOT be emitted. This is the opposite of jd_adjudicator, which advances the
    cascade on failure. Here, "can't verify" = "refuse".

    Args:
        tailored: Dict from resume_tailor.transform (matches TAILORED_RESUME_SCHEMA).
        profile: Experience profile dict (from load_scoring_profile).
        config: App config dict (passed straight to call_model).
        conn: Open sqlite3 connection (call_model needs it for cost recording).

    Returns:
        Tuple of FabricationViolation objects. Empty if prose is clean.
        Returns a single adjudicator_unavailable violation on any LLM failure.
    """
    from job_finder.web.model_provider import call_model

    # Build ground truth corpus from profile
    ground_truth_parts = ["## GROUND TRUTH FACTS (ONLY these are allowed)", ""]

    # Positions with achievements (the truthful bullet source)
    for position in profile.get("positions", []):
        company = position.get("company", "")
        title = position.get("title", "")
        start_date = position.get("start_date", "")
        end_date = position.get("end_date") or "present"
        achievements = position.get("achievements", [])
        position_skills = position.get("skills", [])

        ground_truth_parts.append(f"**{title}** @ {company} ({start_date} - {end_date})")
        if achievements:
            ground_truth_parts.append("Achievements:")
            for ach in achievements:
                ground_truth_parts.append(f"  - {ach}")
        if position_skills:
            ground_truth_parts.append(f"Skills: {', '.join(position_skills)}")
        ground_truth_parts.append("")

    # Profile-level skills
    profile_skills = profile.get("skills", [])
    if profile_skills:
        ground_truth_parts.append("### Profile-Level Skills")
        ground_truth_parts.append(f"{', '.join(profile_skills)}")
        ground_truth_parts.append("")

    # Education
    for edu in profile.get("education", []):
        degree = edu.get("degree", "")
        institution = edu.get("institution", "")
        if degree or institution:
            ground_truth_parts.append(f"**{degree}** — {institution}")

    ground_truth = "\n".join(ground_truth_parts)

    # Build prose under test from tailored
    prose_parts = ["## RESUME PROSE TO CHECK", ""]
    summary = tailored.get("summary", "")
    if summary:
        prose_parts.append("### Summary")
        prose_parts.append(summary)
        prose_parts.append("")

    for idx, section in enumerate(tailored.get("sections", [])):
        bullets = section.get("bullets", [])
        if bullets:
            prose_parts.append(f"### Section {idx} Bullets")
            for bullet in bullets:
                prose_parts.append(f"  - {bullet}")
            prose_parts.append("")

    prose_to_check = "\n".join(prose_parts)

    # Dispatch LLM adjudication
    user_msg = f"{ground_truth}\n\n{prose_to_check}"
    try:
        result = call_model(
            tier="quick",
            system=_PROSE_ADJUDICATION_SYSTEM,
            messages=[{"role": "user", "content": user_msg}],
            conn=conn,
            config=config,
            output_schema=_PROSE_ADJUDICATION_SCHEMA,
            purpose="resume_prose_adjudication",
            max_tokens=512,
        )
        data = result.data
    except Exception as exc:
        logger.warning("adjudicate_resume_prose: call_model failed: %s", exc)
        return (
            FabricationViolation(
                kind="adjudicator_unavailable",
                value="LLM adjudication failed",
                section="summary/bullets",
            ),
        )

    # Parse and validate response
    if not isinstance(data, dict) or "fabrications" not in data:
        logger.warning("adjudicate_resume_prose: unparseable response: %s", data)
        return (
            FabricationViolation(
                kind="adjudicator_unavailable",
                value="Unparseable LLM response",
                section="summary/bullets",
            ),
        )

    fabrications = data.get("fabrications", [])
    if not isinstance(fabrications, list):
        logger.warning("adjudicate_resume_prose: fabrications not a list: %s", data)
        return (
            FabricationViolation(
                kind="adjudicator_unavailable",
                value="Malformed fabrications list",
                section="summary/bullets",
            ),
        )

    # Convert to FabricationViolation objects
    violations = []
    for fab in fabrications:
        if not isinstance(fab, dict):
            continue
        claim = fab.get("claim", "")
        fab_type = fab.get("type", "other")
        section = fab.get("section", "summary")
        if claim:
            violations.append(
                FabricationViolation(
                    kind="prose_fabrication",
                    value=claim,
                    section=f"{section} ({fab_type})",
                )
            )

    return tuple(violations)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate_resume_grounding(
    tailored: dict, profile: dict, job: dict, style_guide: dict | None = None
) -> GroundingReport:
    """Return every hard fact in `tailored` NOT grounded in `profile`, every
    prohibited-item hard-stop violated, every out-of-allowlist most-recent title,
    PLUS a deterministic JD-keyword-coverage metric.

    Four deterministic layers, all pure/no-I/O/no-LLM:

    (A) SUBSET semantics for STRUCTURAL FACTS: the tailored resume may OMIT true
        facts (tailoring drops irrelevant roles) — that is fine. It may NOT ADD a
        fact absent from the profile — that is fabrication. Checks:
          - each section's company   ∈ profile positions' companies (casefold+whitespace only)
          - each section's title      ∈ that section's company's title allowlist
                                        (per-company, not global)
          - each section's date span ⊆ the profile's known date tokens for that
                                        company (start/end years) — no invented years
                                        (blank/ungrounded company checks against union of all years)
          - each skill                ∈ profile skills (profile-level + position-level)
          - each education degree/institution ∈ profile education (normalized)

    (B) PROHIBITED-ITEM hard-stops (pure string/regex over the tailored output):
        - Configurable via style_guide['prohibited_items']; defaults to:
          dbt, Apache Spark, company name in summary, N= sample sizes, em-dashes,
          454% ROI, sub-8 year-count, third-person self-reference.

    (C) TITLE-ALIGNMENT ALLOWLIST: the MOST-RECENT position's tailored title must be
        a member of ({canonical} ∪ that position's title_variants). Out-of-set →
        kind="title_unlisted". Older positions: tailored title == canonical, or ∈
        that position's own declared title_variants; else the layer-A title check
        fires. This bounds "nothing too crazy" to the owner's affirmed set.

    (D) JD-KEYWORD COVERAGE (metric, NOT a violation): normalized present/absent of
        tailored["jd_keywords"] against the tailored text → KeywordCoverage. Honest
        absence is fine; this is REPORTED, never a refusal reason.

    Bullets/summary prose are re-emphasis of true achievements and are NOT
    string-fact-checked in layer (A) (see optional adjudicator). Returns a
    GroundingReport whose `violations` is empty when the tailored resume is fully
    grounded, clean, and title-legal. Pure; does not mutate.

    Args:
        tailored: Dict from resume_tailor.transform (matches TAILORED_RESUME_SCHEMA).
        profile: Experience profile dict (from load_scoring_profile).
        job: Job row dict (used for company name in summary check).
        style_guide: Optional style guide dict (for prohibited_items config).

    Returns:
        GroundingReport with violations tuple and KeywordCoverage metric.
    """
    # Build profile fact sets once
    profile_facts = _build_profile_fact_sets(profile)

    # Layer A: Structural subset check
    violations = _check_structural_subset(tailored, profile_facts)

    # Layer B: Prohibited items scan
    violations.extend(_check_prohibited_items(tailored, profile, job, style_guide))

    # Layer C: Title-alignment allowlist
    violations.extend(_check_title_allowlist(tailored, profile))

    # Layer D: Keyword coverage (metric, not a violation)
    coverage = _compute_keyword_coverage(tailored)

    return GroundingReport(violations=tuple(violations), coverage=coverage)
