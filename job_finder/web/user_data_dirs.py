r"""Platformdirs-backed single source of truth for Job Cannon user-data paths.

All default paths delegate to platformdirs with app name "JobCannon" and
appauthor=False (skips the author segment on Windows, avoiding duplicated
path components like %APPDATA%\JobCannon\JobCannon\).

Tests override the root by setting JOB_CANNON_USER_DATA_DIR environment variable.
"""

import os
from pathlib import Path

import platformdirs

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


def logs_path() -> Path:
    """Return the path to the app.log file.

    Returns:
        Path to logs/app.log under the user data root.
    """
    return user_data_root() / "logs" / "app.log"


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
