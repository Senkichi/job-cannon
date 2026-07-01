"""Configuration loader and centralized defaults.

All config fallback values live here so they stay in sync across the
codebase.  Import the constant you need rather than hard-coding a number.
"""

import os
from pathlib import Path

import yaml


class ConfigNotFoundError(FileNotFoundError):
    """Raised when config.yaml cannot be located via the documented lookup order."""


class ConfigError(ValueError):
    """Raised when config.yaml has invalid schema or structure."""


# --- Server defaults ---
DEFAULT_SERVER_HOST = "127.0.0.1"
DEFAULT_SERVER_PORT = 5000
DEFAULT_SERVER_DEBUG = False

# --- Scoring defaults ---
DEFAULT_CANDIDATE_SCORE_THRESHOLD = 42
DEFAULT_DAILY_BUDGET_USD: float = 10.0
DEFAULT_MIN_SCORE_THRESHOLD = 40

# --- Metrics defaults ---
DEFAULT_FIT_FLOOR: float = 3.5

# --- Model defaults ---
# Legacy tier constants removed in Phase 40 (replaced by _PROVIDER_DEFAULTS)


def resolve_triage_enabled(config: dict) -> bool:
    """Resolve 'auto' string to bool based on primary provider.

    NOTE: Reserved/unwired — the pre-scoring triage gate has no production callers.
    ``providers.triage`` is absent from config.example.yaml; only called from tests.
    Do not wire without a product decision.

    Returns:
        True when primary is claude_code_cli, gemini, gemini_cli, or anthropic.
        False when primary is ollama or local_bundled.
        Preserves explicit True/False from config.
    """
    triage_cfg = config.get("providers", {}).get("triage", {})
    enabled = triage_cfg.get("enabled", "auto")

    if enabled == "auto":
        primary = config.get("providers", {}).get("primary", "anthropic")
        _LOCAL_PRIMARIES = {"ollama", "local_bundled"}
        return primary not in _LOCAL_PRIMARIES

    return bool(enabled)


# --- Database ---
DEFAULT_DB_PATH = "jobs.db"

# --- Sources ---
DEFAULT_LOOKBACK_DAYS = 7

# --- Output ---
DEFAULT_MAX_RESULTS = 50

# --- Agentic backfill ---
# Max jobs the nightly agentic enricher (Ollama + DDG + Playwright) processes
# per run. This is a RUNTIME-BUDGET bound: each job is ~30-60s of network +
# render + LLM validation, so the cap keeps the 4:15 AM run clear of the 5:00 AM
# careers_crawl. Overridable via config: agentic.batch_limit.
DEFAULT_AGENTIC_BATCH_LIMIT = 50

# --- Job-description storage cap ---
# Maximum characters of job-description text persisted to jobs.jd_full /
# jobs.description. This is a STORAGE bound (DB size + a guard against
# pathological multi-hundred-KB postings), NOT a scoring bound — the scorer
# applies its own independent prompt cap (job_scorer._MAX_JD_CHARS) when it
# builds the model input. It must therefore stay comfortably ABOVE that prompt
# cap so the stored JD is never the limiting factor for either scoring or the
# full-JD display on job-row expand. Was 8000 (which truncated readable JDs
# mid-token and sat below the 10k the scorer would have accepted).
JD_STORAGE_MAX_CHARS = 50_000

# --- Company denylist (single source of truth) ---
# Company names that should not produce company records and should be excluded
# from scoring. Two classes live here:
#   1. Placeholder / scam names that are never a real employer.
#   2. Aggregators / re-posters that re-list OTHER employers' jobs under their
#      own brand (#213). Their postings legitimately score apply/consider — the
#      underlying job is real — but the row is mis-attributed to the aggregator
#      and routes the user to a paywalled re-listing instead of the employer's
#      own ATS. Demote them before scoring spend.
#
# IMPORTANT: entries here MUST be in NORMALIZED form (i.e. the output of
# job_finder.normalizers.normalize_company), because get_company_denylist()
# re-normalizes both this set and config entries, and the matching sites
# (should_exclude, cleanup_denylist_companies) compare against normalize_company
# of the stored brand. Normalization strips legal-entity suffixes, so write the
# bare name ("virtual vocations", not "virtual vocations inc") — the suffixed
# variant the SERP sources actually store will normalize down to it.
#
# This is a conservative seed of unambiguous aggregators/re-posters. It is
# EXTENSIBLE per-user via config.yaml `filters.company_denylist` (taste-dependent
# staffing mills belong there, not here). Note SynergisticIT appears in the wild
# both spaced and unspaced ("SynergisticIT" -> "synergisticit",
# "Synergistic it" -> "synergistic it"); normalize_company does not collapse the
# space, so both variants are listed.
_RAW_COMPANY_DENYLIST: frozenset[str] = frozenset(
    {
        # Placeholder / scam names
        "unknown",
        "medical jobs",
        "clinical jobs",
        "remotehunter",
        "jobgether",
        "mercor",
        "crossing hurdles",
        # Aggregators / re-posters (#213)
        "virtual vocations",
        "prosidian consulting",
        "synergisticit",
        "synergistic it",
    }
)


def _normalize_denylist(entries) -> frozenset[str]:
    """Normalize denylist entries via normalize_company for suffix-variant parity.

    Imported lazily to avoid a config<->normalizers import cycle at module load.
    """
    from job_finder.normalizers import normalize_company

    return frozenset(normalize_company(e) for e in entries if e and normalize_company(e))


# Normalized form used by all matching sites. Built once at import.
COMPANY_DENYLIST: frozenset[str] = _normalize_denylist(_RAW_COMPANY_DENYLIST)


def get_company_allowlist(config: dict) -> frozenset[str]:
    """Return the company allowlist from config, merged with hardcoded defaults.

    The allowlist lets users rescue false-positive rejections without code
    changes. An allowed name bypasses overlong and suspicious-value rejection
    (but not the empty/no-alpha hard rejects).

    Args:
        config: Full config dict (may contain filters.company_allowlist list).

    Returns:
        frozenset of lowercased, stripped company name strings to always accept.
    """
    config_entries = config.get("filters", {}).get("company_allowlist", [])
    return frozenset(e.lower().strip() for e in config_entries if e)


def get_company_denylist(config: dict) -> frozenset[str]:
    """Return the company denylist, merging config.yaml entries with hardcoded defaults.

    Config entries are additive — the hardcoded defaults are always included.

    Both the hardcoded seed and config entries are normalized via
    normalize_company so the returned set matches the same canonical form the
    matching sites (should_exclude) compute from the stored brand. This closes
    the #213 legal-entity-suffix gap: a config or seed entry of
    "Virtual Vocations" now matches stored rows of "Virtual Vocations Inc"
    (both normalize to "virtual vocations").

    Args:
        config: Full config dict (may contain filters.company_denylist list).

    Returns:
        frozenset of normalized (normalize_company) company name strings to exclude.
    """
    config_entries = config.get("filters", {}).get("company_denylist", [])
    extra = _normalize_denylist(config_entries)
    return COMPANY_DENYLIST | extra


def get_fit_floor(config: dict) -> float:
    """Return the fit-floor threshold for target-set membership.

    Args:
        config: Full config dict (may contain metrics.fit_floor).

    Returns:
        Fit-floor value (default 3.5).
    """
    return float(config.get("metrics", {}).get("fit_floor", DEFAULT_FIT_FLOOR))


def validate_target_titles(config: dict) -> None:
    """Reject configs where profile.target_titles is empty without an explicit override.

    An empty target_titles list disables the substring filter inside
    ats_platforms._title_matches (the ``if target_titles:`` branch is
    skipped) and causes every ATS-API scanner -- Greenhouse, Lever, Ashby,
    Workday, SmartRecruiters -- plus the careers_crawler tiers to ingest
    every open posting on every scanned company's board. On 2026-05-18 a
    single off-cadence run with an accidentally-cleared target_titles
    inserted 45,623 rows in one pass.

    Override: set ``profile.allow_unfiltered_scan: true`` when you
    intentionally want full-board ingestion (e.g. ATS coverage testing,
    relying on the LLM cascade for downstream filtering).

    Raises:
        ConfigError: If profile.target_titles is missing or empty and
            profile.allow_unfiltered_scan is not True.
    """
    profile = config.get("profile", {})
    if profile.get("allow_unfiltered_scan") is True:
        return

    titles = profile.get("target_titles")
    # Treat missing, None, [] all as "empty". Explicit non-list values are
    # also rejected -- the call sites assume an iterable of strings.
    if not titles or not isinstance(titles, list):
        raise ConfigError(
            "profile.target_titles is empty or missing.\n\n"
            "An empty list disables the ATS-scan title filter and causes "
            "whole-board ingestion from every scanned company (Greenhouse, "
            "Lever, Ashby, Workday, SmartRecruiters). This has previously "
            "inserted 45,000+ rows in a single off-cadence scan.\n\n"
            "Either populate the list with the title keywords you care about, "
            "or set profile.allow_unfiltered_scan: true to acknowledge that "
            "you want full-board ingestion."
        )


def validate_required_sections(config: dict) -> None:
    """Validate that all required top-level sections are present in config.

    Args:
        config: Config dict loaded from config.yaml.

    Raises:
        ConfigError: If any required section is missing, naming the missing section(s).
        ConfigError: If profile.target_titles is empty without an explicit override
            (see validate_target_titles).
    """
    required = ["profile", "sources", "scoring", "db"]
    missing = [s for s in required if s not in config]
    if missing:
        raise ConfigError(
            f"Config is missing required section(s): {', '.join(missing)}\n"
            f"Run `job-cannon --print-example-config` to see the full expected structure."
        )

    # Reject old providers.scoring schema (Phase 40 migration)
    if "providers" in config and "scoring" in config["providers"]:
        raise ConfigError(
            "Old config schema detected: providers.scoring key found. "
            "Phase 40 migrated to flat providers: schema. "
            "Run: uv run python -m job_finder.migrate_config\n"
            "See .planning/phases/40-workload-tiers-cascade-rewire-canary/40-CONTEXT.md for migration instructions."
        )

    validate_target_titles(config)


def normalize_profile_work_arrangement(cfg: dict) -> dict:
    """Heal legacy configs where work arrangement was a 'Remote' sentinel in target_locations.

    When ``profile.target_locations`` contains "Remote" (case-insensitive), derives
    ``profile.work_arrangement = "remote"`` (unless already set) and strips the sentinel
    from the geography list, returning a NEW config dict. Configs that already have
    ``work_arrangement`` set, or whose ``target_locations`` contains no "Remote" sentinel,
    are returned unchanged (as new dicts). Idempotent.

    Args:
        cfg: Full config dict (may be empty). Never mutated.

    Returns:
        New config dict; only modified when the legacy sentinel is present.
    """
    profile = cfg.get("profile")
    if not profile:
        return dict(cfg)

    target_locations: list = list(profile.get("target_locations") or [])
    remote_sentinels = [t for t in target_locations if (t or "").strip().lower() == "remote"]

    if not remote_sentinels:
        # No sentinel present — nothing to heal; return a shallow copy (immutability).
        return {**cfg, "profile": dict(profile)}

    # Strip sentinel from the geography list.
    clean_locations = [t for t in target_locations if (t or "").strip().lower() != "remote"]
    # Preserve an existing explicit work_arrangement; derive only when absent.
    derived_arrangement = profile.get("work_arrangement") or "remote"

    new_profile = {
        **profile,
        "work_arrangement": derived_arrangement,
        "target_locations": clean_locations,
    }
    return {**cfg, "profile": new_profile}


def load_config(
    config_path: str | os.PathLike[str] | None = None,
    allow_missing: bool = False,
) -> dict:
    """Load config.yaml from the user-data directory or a custom path.

    Path resolution order:
    1. If ``config_path`` is provided, use it.
    2. If ``$JOB_CANNON_CONFIG`` env var is set, use it.
    3. Default to user_data_dirs.config_path().

    Args:
        config_path: Optional custom path to config.yaml. Accepts str or PathLike;
            internally coerced to Path so callers can pass either.
        allow_missing: If True, return ``{}`` when the file is missing instead of
            raising ConfigNotFoundError. Schema validation still applies to
            populated configs — the onboarding wizard handles ConfigError by
            routing to the migration UI.

    Returns:
        Configuration dictionary, or {} if allow_missing=True and file doesn't exist.

    Raises:
        ConfigNotFoundError: If config file not found and allow_missing=False.
        ValueError: If config file contains invalid YAML or is empty.
        ConfigError: If a populated config fails schema validation, regardless
            of allow_missing.
    """
    from job_finder.web import user_data_dirs

    # Path selection rules
    if config_path is None:
        env = os.environ.get("JOB_CANNON_CONFIG")
        if env:
            if not os.path.exists(env):
                raise ConfigNotFoundError(
                    f"$JOB_CANNON_CONFIG is set to '{env}' but no file exists there. "
                    f"Either fix the env var, unset it, or place a config.yaml at the path."
                )
            path = Path(env)
        else:
            path = user_data_dirs.config_path()
    else:
        # Coerce to Path so str/PathLike callers all reach the .exists() branch safely.
        # Commit 9869675 narrowed the param to Optional[Path] but did not update the
        # body; tests and callers still pass str (e.g. test_config_resolution).
        path = Path(config_path)

    # File existence check
    if not path.exists():
        if allow_missing:
            return {}
        raise ConfigNotFoundError(
            f"Config file not found: {path}\n\n"
            f"To get started:\n"
            f"  1. Print the example:  job-cannon --print-example-config > config.yaml\n"
            f"  2. Edit config.yaml and fill in:\n"
            f"     - profile.target_titles (job titles you're looking for)\n"
            f"     - profile.target_locations (where you want to work)\n"
            f"     - profile.skills (your key skills)\n"
            f"     - sources.gmail.enabled (set to true to use Gmail alerts)\n"
            f"  3. See docs/SETUP.md for full configuration reference\n"
        )

    try:
        with open(path, encoding="utf-8") as f:
            try:
                cfg = yaml.safe_load(f)
            except yaml.YAMLError as exc:
                raise ValueError(
                    f"Config file contains invalid YAML: {path}\n{exc}\n"
                    f"Run `job-cannon --print-example-config` to see the expected structure."
                ) from exc
    except UnicodeDecodeError as exc:
        raise ValueError(
            f"Config file is not valid UTF-8: {path}\n"
            f"Ensure the file is saved with UTF-8 encoding.\n{exc}"
        ) from exc

    if cfg is None:
        raise ValueError(
            f"Config file is empty or contains only comments: {path}\n"
            f"Run `job-cannon --print-example-config` to see the expected structure."
        )

    # An absent file already returned {} above. If we reached here with a
    # populated dict, the user has a config — validate it, even when
    # allow_missing=True (the onboarding wizard handles ConfigError by
    # routing to the migration UI). Phase 40 hotfix (2026-05-17): conflating
    # "file missing" with "skip every schema check" let an old-shape config
    # load silently and broke the LLM cascade for ~24h.
    validate_required_sections(cfg)
    # Heal legacy work-arrangement sentinel on every load (idempotent).
    return normalize_profile_work_arrangement(cfg)
