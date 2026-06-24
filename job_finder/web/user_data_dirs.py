r"""Platformdirs-backed single source of truth for Job Cannon user-data paths.

All default paths delegate to platformdirs with app name "JobCannon" and
appauthor=False (skips the author segment on Windows, avoiding duplicated
path components like %APPDATA%\JobCannon\JobCannon\).

Tests override the root by setting JOB_CANNON_USER_DATA_DIR environment variable.
"""

import logging
import os
from pathlib import Path

import platformdirs

logger = logging.getLogger(__name__)

_APP_NAME = "JobCannon"
_APP_AUTHOR = False


def user_data_root() -> Path:
    """Return the user data root directory.

    If JOB_CANNON_USER_DATA_DIR is set, return that path (used for test isolation
    and developer local setup). Otherwise, delegate to platformdirs.

    Returns:
        Path to the user data root directory.
    """
    override = os.environ.get("JOB_CANNON_USER_DATA_DIR")
    if override:
        return Path(override)
    return Path(platformdirs.user_data_dir(_APP_NAME, appauthor=_APP_AUTHOR))


def ensure_user_data_dir() -> Path:
    """Create the user data root directory if it doesn't exist.

    Returns:
        Path to the user data root directory (created if needed).
    """
    root = user_data_root()
    root.mkdir(parents=True, exist_ok=True)
    return root


def config_path() -> Path:
    """Return the path to the config.yaml file.

    Returns:
        Path to config.yaml under the user data root.
    """
    return user_data_root() / "config.yaml"


def db_path() -> Path:
    """Return the path to the jobs.db file.

    Returns:
        Path to jobs.db under the user data root.
    """
    return user_data_root() / "jobs.db"


def profile_path() -> Path:
    """Return the path to the experience_profile.json file.

    Single source of truth for the candidate experience profile, mirroring
    config_path()/db_path(). The onboarding wizard writes here, and both the
    Profile editor and the scorer resolve the same location, so a packaged /
    pipx install (where launch-CWD != user-data root) no longer splits the
    profile across two files. Respects JOB_CANNON_USER_DATA_DIR.

    Returns:
        Path to experience_profile.json under the user data root.
    """
    return user_data_root() / "experience_profile.json"


def logs_path() -> Path:
    """Return the path to the app.log file.

    Returns:
        Path to logs/app.log under the user data root.
    """
    return user_data_root() / "logs" / "app.log"


def last_alive_path() -> Path:
    """Return the path to the ``last_alive`` liveness-heartbeat marker.

    Touched on a short recurring interval by the serve-path heartbeat job
    (``scheduler/_heartbeat.py``) so an out-of-process healthcheck can judge
    app liveness by the file's *freshness* — even when the in-process
    scheduler's daily health heartbeat hasn't fired and the HTTP listener is
    unreachable. Respects ``JOB_CANNON_USER_DATA_DIR``.

    Returns:
        Path to ``last_alive`` under the user data root.
    """
    return user_data_root() / "last_alive"


def cache_path() -> Path:
    """Return the path to the cache directory.

    Returns:
        Path to cache directory under the user data root.
    """
    return user_data_root() / "cache"


def update_check_path() -> Path:
    """Return the path to the update_check.json cache file.

    Returns:
        Path to update_check.json under the user data root.
    """
    return user_data_root() / "update_check.json"


def token_path() -> Path:
    """Return the path to the Gmail OAuth token file.

    Returns:
        Path to token.json under the user data root.
    """
    return user_data_root() / "token.json"


def credentials_path() -> Path:
    """Return the path to the Gmail OAuth credentials file.

    Returns:
        Path to credentials.json under the user data root.
    """
    return user_data_root() / "credentials.json"


def parse_failures_dir() -> Path:
    """Return the path to the Gmail parse-failures archive directory.

    Returns:
        Path to gmail_parse_failures/ under the user data root.
    """
    return user_data_root() / "gmail_parse_failures"


def warn_if_data_split(cwd: Path | None = None) -> bool:
    """Warn at startup when an unset env var is silently shadowing real data.

    Failure mode this catches: the developer's persisted ``JOB_CANNON_USER_DATA_DIR``
    is missing from a new shell, so ``user_data_root()`` falls back to platformdirs,
    the app starts a fresh onboarding flow at that empty location, and the real
    ``jobs.db`` at the repo checkout becomes invisible.

    Detection is intentionally narrow — warns iff *all three* hold:
        1. ``JOB_CANNON_USER_DATA_DIR`` is unset.
        2. The resolved data root differs from ``cwd``.
        3. ``cwd / "jobs.db"`` exists.

    Args:
        cwd: Override for the current working directory (for test isolation).
            Defaults to ``Path.cwd()``.

    Returns:
        True if a warning was emitted, False otherwise. The return value is the
        test seam; production callers can ignore it.
    """
    if os.environ.get("JOB_CANNON_USER_DATA_DIR"):
        return False

    here = (cwd if cwd is not None else Path.cwd()).resolve()
    resolved = user_data_root().resolve()
    if here == resolved:
        return False

    here_db = here / "jobs.db"
    if not here_db.exists():
        return False

    logger.warning(
        "JOB_CANNON_USER_DATA_DIR is unset. The app is reading user data from %s "
        "(platformdirs default), but a jobs.db exists at the current working "
        "directory: %s. If you intended to use the cwd database, set "
        "JOB_CANNON_USER_DATA_DIR=%s and restart. This warning is only emitted at "
        "startup.",
        resolved,
        here_db,
        here,
    )
    return True
