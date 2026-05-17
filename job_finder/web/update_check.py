"""Update-check service — fetches latest GitHub release, caches result, exposes
banner context for the Jinja2 template layer. See Phase 43 D-01..D-04.
"""
import json
import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from importlib.metadata import version as _pkg_version, PackageNotFoundError
from pathlib import Path
from typing import Optional

import requests

from job_finder.web.user_data_dirs import (
    ensure_user_data_dir,
    update_check_path,
)

logger = logging.getLogger(__name__)

_GITHUB_RELEASES_LATEST_URL = (
    "https://api.github.com/repos/Senkichi/job-cannon/releases/latest"
)
_STALE_AFTER = timedelta(hours=24)
_FETCH_TIMEOUT_SECONDS = 5
_MAX_VERSION_LEN = 64  # defense against absurd tag_name responses


def _empty_cache() -> dict:
    return {
        "checked_at": None,
        "latest_version": None,
        "current_version": current_version(),
        "dismissed_versions": [],
    }


def _is_stale(cache: Optional[dict]) -> bool:
    if not cache or not cache.get("checked_at"):
        return True
    try:
        checked_at = datetime.fromisoformat(cache["checked_at"].replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return True
    return datetime.now(timezone.utc) - checked_at > _STALE_AFTER


def _write_cache_atomic(cache: dict, cache_path: Path) -> None:
    """Atomic write — mirror settings.py::_write_config."""
    tmp_path = cache_path.with_suffix(".json.tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2, sort_keys=False)
        os.replace(tmp_path, cache_path)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def current_version() -> Optional[str]:
    """Return the installed package version with leading 'v', or None.

    Source of truth: pyproject.toml > project.version surfaced via
    importlib.metadata. No __version__ constant — avoids drift (D-03).
    Returns 'v5.0.0' format (leading v added) for tag-comparison parity.
    """
    try:
        raw = _pkg_version("job-cannon")
        return f"v{raw}" if raw and not raw.startswith("v") else raw
    except PackageNotFoundError:
        return None


def read_cache() -> Optional[dict]:
    """Read update_check.json; return None on missing or unparseable."""
    path = update_check_path()
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return None
        return data
    except (OSError, json.JSONDecodeError) as e:
        logger.info("update_check cache unreadable (non-fatal): %s", e)
        return None


def append_dismissed_version(version_str: str) -> None:
    """Read cache, append version to dismissed_versions, write atomically.

    Idempotent — no-op if version already in list. Creates cache if missing.
    Mirrors the read→merge→write discipline of settings._write_config
    to survive concurrent threads (D-06).
    """
    if not version_str or len(version_str) > _MAX_VERSION_LEN:
        return  # reject obviously-bad input silently
    cache = read_cache() or _empty_cache()
    dismissed = list(cache.get("dismissed_versions") or [])
    if version_str not in dismissed:
        dismissed.append(version_str)
    cache["dismissed_versions"] = dismissed
    ensure_user_data_dir()
    _write_cache_atomic(cache, update_check_path())


def _fetch_and_persist() -> Optional[dict]:
    """GET GitHub releases/latest, parse tag_name, persist cache. Silent-fail (D-04)."""
    try:
        response = requests.get(
            _GITHUB_RELEASES_LATEST_URL,
            timeout=_FETCH_TIMEOUT_SECONDS,
            headers={"Accept": "application/vnd.github+json", "User-Agent": "job-cannon-update-check"},
        )
    except requests.RequestException as e:
        logger.info("Update check network error (non-fatal): %s", e)
        return None
    if response.status_code != 200:
        logger.info("Update check non-200 (non-fatal): status=%s", response.status_code)
        return None
    try:
        payload = response.json()
        latest = payload.get("tag_name")
    except (ValueError, AttributeError) as e:
        logger.info("Update check JSON parse error (non-fatal): %s", e)
        return None
    if not isinstance(latest, str) or not latest or len(latest) > _MAX_VERSION_LEN:
        logger.info("Update check tag_name shape invalid (non-fatal): %r", latest)
        return None
    # Whitelist allowed chars to defuse response-injection (alphanumeric, dot, hyphen, plus, leading v)
    if not all(c.isalnum() or c in ".-+v" for c in latest):
        logger.info("Update check tag_name has disallowed chars (non-fatal): %r", latest)
        return None
    prior = read_cache() or _empty_cache()
    cache = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "latest_version": latest,
        "current_version": current_version(),
        "dismissed_versions": list(prior.get("dismissed_versions") or []),
    }
    ensure_user_data_dir()
    try:
        _write_cache_atomic(cache, update_check_path())
    except OSError as e:
        logger.info("Update check cache write failed (non-fatal): %s", e)
        return None
    return cache


def kick_off_background_check_if_due(config: dict) -> None:
    """Start a daemon thread to refresh the cache if the 24h window has elapsed.

    No-op when config['TESTING'] is True (pytest safety) or cache is fresh.
    Mirrors startup_backfills.run_description_reformat_once daemon-thread pattern (D-01).
    """
    if config and config.get("TESTING"):
        return
    if not _is_stale(read_cache()):
        return

    def _run():
        try:
            _fetch_and_persist()
        except Exception as e:  # belt-and-suspenders silent-fail (D-04)
            logger.info("Update check failed (non-fatal): %s", e)

    threading.Thread(target=_run, daemon=True).start()
    logger.debug("Update check started in background thread")


def banner_context() -> Optional[dict]:
    """Return banner template dict, or None if no banner should render (D-08)."""
    cache = read_cache()
    if not cache:
        return None
    latest = cache.get("latest_version")
    current = cache.get("current_version") or current_version()
    if not latest or not current:
        return None
    if latest == current:
        return None  # D-08(a)
    if latest in (cache.get("dismissed_versions") or []):
        return None  # D-08(c)
    return {"latest_version": latest, "current_version": current}
