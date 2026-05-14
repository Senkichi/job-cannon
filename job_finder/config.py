"""Configuration loader and centralized defaults.

All config fallback values live here so they stay in sync across the
codebase.  Import the constant you need rather than hard-coding a number.
"""

import os
from pathlib import Path

import yaml

DEFAULT_CONFIG_PATH = "config.yaml"


class ConfigNotFoundError(FileNotFoundError):
    """Raised when config.yaml cannot be located via the documented lookup order."""


# --- Server defaults ---
DEFAULT_SERVER_HOST = "127.0.0.1"
DEFAULT_SERVER_PORT = 5000
DEFAULT_SERVER_DEBUG = True

# --- Scoring defaults ---
DEFAULT_CANDIDATE_SCORE_THRESHOLD = 42
DEFAULT_BORDERLINE_HIGH = 54
DEFAULT_DAILY_BUDGET_USD: float = 10.0
DEFAULT_MIN_SCORE_THRESHOLD = 40

# --- Model defaults ---
DEFAULT_MODEL_LOW = "claude-haiku-4-5"
DEFAULT_MODEL_MID = "claude-sonnet-4-6"
DEFAULT_MODEL_HIGH = "claude-opus-4-6"

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
COMPANY_DENYLIST: frozenset[str] = frozenset(
    {
        "unknown",
        "medical jobs",
        "clinical jobs",
        "remotehunter",
        "jobgether",
        "mercor",
        "crossing hurdles",
    }
)


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


def resolve_config_path() -> str:
    """Locate config.yaml via the documented lookup order.

    Lookup order:
      1. ``$JOB_CANNON_CONFIG`` environment variable.
         If set but the path doesn't exist → :class:`ConfigNotFoundError`
         (do NOT fall through — the user explicitly named where the
         config is, silently using a different one is wrong UX).
      2. ``./config.yaml`` in the current working directory.
      3. User config directory:
         - Windows: ``%APPDATA%/job-cannon/config.yaml``
         - Unix:    ``~/.config/job-cannon/config.yaml``

    Returns:
        Absolute or relative path to the resolved config file.

    Raises:
        ConfigNotFoundError: if no path resolves AND no env var was set,
            or if the env var was set but its target file does not exist.
    """
    env = os.environ.get("JOB_CANNON_CONFIG")
    if env:
        if not os.path.exists(env):
            raise ConfigNotFoundError(
                f"$JOB_CANNON_CONFIG is set to '{env}' but no file exists there. "
                f"Either fix the env var, unset it, or place a config.yaml at the path."
            )
        return env

    cwd = os.path.join(os.getcwd(), "config.yaml")
    if os.path.exists(cwd):
        return cwd

    if os.name == "nt":
        appdata = os.environ.get("APPDATA", "")
        user_config = os.path.join(appdata, "job-cannon", "config.yaml")
    else:
        user_config = os.path.join(os.path.expanduser("~"), ".config", "job-cannon", "config.yaml")
    if os.path.exists(user_config):
        return user_config

    raise ConfigNotFoundError(
        "config.yaml not found. Looked at: "
        f"./config.yaml, {user_config}. "
        "Copy config.example.yaml to ./config.yaml to get started, "
        "or set $JOB_CANNON_CONFIG to point to your config file."
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

    try:
        with open(path, encoding="utf-8") as f:
            try:
                cfg = yaml.safe_load(f)
            except yaml.YAMLError as exc:
                raise ValueError(
                    f"Config file contains invalid YAML: {config_path}\n{exc}\n"
                    f"See config.example.yaml for the expected structure."
                ) from exc
    except UnicodeDecodeError as exc:
        raise ValueError(
            f"Config file is not valid UTF-8: {config_path}\n"
            f"Ensure the file is saved with UTF-8 encoding.\n{exc}"
        ) from exc

    if cfg is None:
        raise ValueError(
            f"Config file is empty or contains only comments: {config_path}\n"
            f"See config.example.yaml for the expected structure."
        )

    validate_required_sections(cfg)
    return cfg
