"""Configuration loader and centralized defaults.

All config fallback values live here so they stay in sync across the
codebase.  Import the constant you need rather than hard-coding a number.
"""

import os
import tempfile
from pathlib import Path

import yaml

DEFAULT_CONFIG_PATH = "config.yaml"


class ConfigNotFoundError(FileNotFoundError):
    """Raised when config.yaml cannot be located via the documented lookup order."""


class ConfigError(ValueError):
    """Raised when config.yaml has invalid schema or structure."""


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
# Legacy tier constants removed in Phase 40 (replaced by _PROVIDER_DEFAULTS)

def resolve_triage_enabled(config: dict) -> bool:
    """Resolve 'auto' string to bool based on primary provider.

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

    # Reject old providers.scoring schema (Phase 40 migration)
    if "providers" in config and "scoring" in config["providers"]:
        raise ConfigError(
            "Old config schema detected: providers.scoring key found. "
            "Phase 40 migrated to flat providers: schema. "
            "See .planning/phases/40-workload-tiers-cascade-rewire-canary/40-CONTEXT.md for migration instructions."
        )


def write_config(data: dict) -> Path:
    """Write config dict to user-data config.yaml atomically.

    Creates the user-data directory if needed, writes to a temp file in the
    same directory, then swaps with os.replace() for atomicity.

    Args:
        data: Configuration dictionary to write.

    Returns:
        Path to the written config file.
    """
    from job_finder.web import user_data_dirs

    user_data_dirs.ensure_user_data_dir()
    config_path = user_data_dirs.config_path()

    # Write to a temp file in the same directory for atomic swap
    fd, temp_path = tempfile.mkstemp(dir=config_path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, default_flow_style=False)
        # Atomic swap
        os.replace(temp_path, config_path)
    except Exception:
        # Clean up temp file on error
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise

    return config_path


def load_config(config_path: str | os.PathLike[str] | None = None, *, allow_missing: bool = False) -> dict:
    """Load configuration from YAML file.

    Args:
        config_path: Path to config.yaml. If None, uses platformdirs default.
        allow_missing: If True, returns {} when config doesn't exist.

    Returns:
        Configuration dictionary, or {} if allow_missing=True and file doesn't exist.

    Raises:
        ConfigNotFoundError: If config file not found and allow_missing=False.
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
        path = Path(config_path)

    # File existence check
    if not path.exists():
        if allow_missing:
            return {}
        raise ConfigNotFoundError(
            f"Config file not found: {path}\n\n"
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
                    f"Config file contains invalid YAML: {path}\n{exc}\n"
                    f"See config.example.yaml for the expected structure."
                ) from exc
    except UnicodeDecodeError as exc:
        raise ValueError(
            f"Config file is not valid UTF-8: {path}\n"
            f"Ensure the file is saved with UTF-8 encoding.\n{exc}"
        ) from exc

    if cfg is None:
        raise ValueError(
            f"Config file is empty or contains only comments: {path}\n"
            f"See config.example.yaml for the expected structure."
        )

    validate_required_sections(cfg)
    return cfg
