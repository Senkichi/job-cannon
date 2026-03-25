"""Configuration loader and centralized defaults.

All config fallback values live here so they stay in sync across the
codebase.  Import the constant you need rather than hard-coding a number.
"""

import yaml
from pathlib import Path


DEFAULT_CONFIG_PATH = "config.yaml"

# --- Server defaults ---
DEFAULT_SERVER_HOST = "127.0.0.1"
DEFAULT_SERVER_PORT = 5000
DEFAULT_SERVER_DEBUG = True

# --- Scoring defaults ---
DEFAULT_HAIKU_THRESHOLD = 42
DEFAULT_BORDERLINE_HIGH = 54
DEFAULT_MONTHLY_BUDGET_USD = 25.0
DEFAULT_MIN_SCORE_THRESHOLD = 40
DEFAULT_MULTI_VERSION_THRESHOLD = 80

# --- Model defaults ---
DEFAULT_MODEL_HAIKU = "claude-haiku-4-5"
DEFAULT_MODEL_SONNET = "claude-sonnet-4-6"
DEFAULT_MODEL_OPUS = "claude-opus-4-6"

# --- Database ---
DEFAULT_DB_PATH = "jobs.db"

# --- Sources ---
DEFAULT_LOOKBACK_DAYS = 7

# --- Output ---
DEFAULT_MAX_RESULTS = 50

# --- Profile ---
DEFAULT_PROFILE_PATH = "experience_profile.json"

# --- Company denylist (single source of truth) ---
# Placeholder company names that should not produce company records
# and should be excluded from scoring.
COMPANY_DENYLIST: frozenset[str] = frozenset({
    "unknown",
    "medical jobs",
    "clinical jobs",
    "remotehunter",
    "jobgether",
    "mercor",
    "crossing hurdles",
})


def get_company_denylist(config: dict) -> frozenset[str]:
    """Return the company denylist, merging config.yaml entries with hardcoded defaults.

    Config entries are additive — the hardcoded defaults are always included.

    Args:
        config: Full config dict (may contain filters.company_denylist list).

    Returns:
        frozenset of lowercased, stripped company name strings to exclude.
    """
    config_entries = config.get("filters", {}).get("company_denylist", [])
    extra = frozenset(e.lower().strip() for e in config_entries if e)
    return COMPANY_DENYLIST | extra


def validate_required_sections(config: dict) -> None:
    """Validate that all required top-level sections are present in config.

    Args:
        config: Config dict loaded from config.yaml.

    Raises:
        ValueError: If any required section is missing, naming the missing section(s).
    """
    required = ["profile", "sources", "scoring", "db"]
    missing = [s for s in required if s not in config]
    if missing:
        raise ValueError(
            f"Config is missing required section(s): {', '.join(missing)}\n"
            f"See config.example.yaml for the expected structure."
        )


def load_config(config_path: str = DEFAULT_CONFIG_PATH) -> dict:
    """Load configuration from YAML file.

    Args:
        config_path: Path to config.yaml.

    Returns:
        Configuration dictionary.
    """
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(
            f"Config file not found: {config_path}\n\n"
            f"To get started:\n"
            f"  1. Copy the example:  cp config.example.yaml config.yaml\n"
            f"  2. Edit config.yaml and fill in:\n"
            f"     - profile.target_titles (job titles you're looking for)\n"
            f"     - profile.target_locations (where you want to work)\n"
            f"     - profile.skills (your key skills)\n"
            f"     - sources.gmail.enabled (set to true to use Gmail alerts)\n"
            f"  3. See docs/SETUP.md for full configuration reference\n"
        )

    with open(path, "r") as f:
        try:
            cfg = yaml.safe_load(f)
        except yaml.YAMLError as exc:
            raise ValueError(
                f"Config file contains invalid YAML: {config_path}\n{exc}\n"
                f"See config.example.yaml for the expected structure."
            ) from exc

    if cfg is None:
        raise ValueError(
            f"Config file is empty or contains only comments: {config_path}\n"
            f"See config.example.yaml for the expected structure."
        )

    validate_required_sections(cfg)
    return cfg
