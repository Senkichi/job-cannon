"""Smart Ollama probe and conditional auto-start.

Three-stage probe for Ollama availability:

  Stage 1a — HTTP liveness (GET /api/tags, timeout=1.0 s)
  Stage 1b — One 500 ms backoff retry on connection failure
  Stage 2  — Schema check (``{"models": [...]}`` shape)
  Stage 3  — Installability check (binary on PATH / known default path)

Returns one of:

  AlreadyRunning(spawned_by_us=False, model_present=<bool>)
      A reachable /api/tags endpoint was found — attach rather than spawn.

  Installable(path=<str>)
      /api/tags is not reachable but ``ollama`` binary is available.
      Caller should spawn; probe sets NO detach flags so the child
      inherits our process-group and dies with Job Cannon.

  Unavailable()
      Neither reachable nor installable. Cascade should fall through.

URL resolution precedence (applied before any probe):
  1. ``JOB_CANNON_OLLAMA_URL`` environment variable
  2. ``config["providers"]["ollama"]["base_url"]``
  3. Default ``http://localhost:11434``
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

_DEFAULT_OLLAMA_URL = "http://localhost:11434"
_PROBE_TIMEOUT = 1.0  # seconds — single attempt
_RETRY_BACKOFF = 0.5  # seconds — one retry after first failure


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class AlreadyRunning:
    """Ollama is already reachable at the resolved URL."""

    spawned_by_us: bool = False
    model_present: bool = False


@dataclass
class Installable:
    """Ollama binary found but service is not running."""

    path: str = ""


@dataclass
class Unavailable:
    """Ollama is neither running nor installable."""


OllamaState = AlreadyRunning | Installable | Unavailable


# ---------------------------------------------------------------------------
# URL resolution
# ---------------------------------------------------------------------------


def resolve_ollama_url(config: dict) -> str:
    """Resolve the Ollama base URL from env > config > default.

    Args:
        config: Full JF_CONFIG dict (or a sub-section — reads
                ``config["providers"]["ollama"]["base_url"]``).

    Returns:
        Resolved URL string (trailing slash stripped).
    """
    # 1. Env var override
    env_url = os.environ.get("JOB_CANNON_OLLAMA_URL", "").strip()
    if env_url:
        return env_url.rstrip("/")

    # 2. Config key
    provider_cfg = config.get("providers", {}).get("ollama", {})
    cfg_url = provider_cfg.get("base_url", "").strip()
    if cfg_url:
        return cfg_url.rstrip("/")

    # 3. Default
    return _DEFAULT_OLLAMA_URL


# ---------------------------------------------------------------------------
# Liveness probe (with one retry)
# ---------------------------------------------------------------------------


def _probe_liveness(resolved_url: str) -> dict | None:
    """Attempt GET /api/tags. Returns parsed JSON dict on success, None on failure.

    Tries once, then waits ``_RETRY_BACKOFF`` seconds and tries once more
    (stage 1b). Returns None on any error — the caller decides what that means.
    """
    url = f"{resolved_url}/api/tags"

    for attempt in range(2):
        try:
            resp = requests.get(url, timeout=_PROBE_TIMEOUT)
            resp.raise_for_status()
            return resp.json()
        except Exception:
            if attempt == 0:
                time.sleep(_RETRY_BACKOFF)
            # second attempt failing → fall through to return None

    return None


# ---------------------------------------------------------------------------
# Binary location
# ---------------------------------------------------------------------------


def _find_ollama_binary() -> str | None:
    """Locate the ``ollama`` binary; return its path or None."""
    # User override
    exe = os.environ.get("OLLAMA_EXE", "").strip() or None
    if exe and Path(exe).exists():
        return exe

    # Windows default install path
    if sys.platform == "win32":
        localappdata = os.environ.get("LOCALAPPDATA", "")
        if localappdata:
            candidate = Path(localappdata) / "Programs" / "Ollama" / "ollama.exe"
            if candidate.exists():
                return str(candidate)

    # PATH lookup (Linux / macOS / Windows with PATH entry)
    return shutil.which("ollama")


# ---------------------------------------------------------------------------
# Main probe entry point
# ---------------------------------------------------------------------------


def probe_ollama(target_model: str, resolved_url: str) -> OllamaState:
    """Three-stage Ollama probe.

    Args:
        target_model: Model tag the caller intends to use (e.g. "qwen2.5:14b").
                      Used only to populate ``AlreadyRunning.model_present``.
        resolved_url: Base URL resolved by ``resolve_ollama_url()`` — passed in
                      so the caller can store it and mutate live config once.

    Returns:
        ``AlreadyRunning``, ``Installable``, or ``Unavailable``.
    """
    data = _probe_liveness(resolved_url)

    if data is not None:
        # Stage 2 — schema check
        if not (isinstance(data, dict) and "models" in data and isinstance(data["models"], list)):
            logger.warning(
                "Port responded but did not look like Ollama (`/api/tags` schema mismatch); "
                "skipping. Set `JOB_CANNON_OLLAMA_URL=http://otherhost:port` to override."
            )
            return Unavailable()

        # Healthy Ollama — check if the target model is already pulled
        model_present = any(
            m.get("name", "") == target_model or m.get("model", "") == target_model
            for m in data["models"]
        )
        logger.info("Ollama already running, attaching (model_present=%s)", model_present)
        return AlreadyRunning(spawned_by_us=False, model_present=model_present)

    # Stage 3 — is Ollama installed?
    binary = _find_ollama_binary()
    if binary is None:
        logger.info("Ollama not installed; cascade will fall through")
        return Unavailable()

    return Installable(path=binary)


# ---------------------------------------------------------------------------
# Spawn helper (called by scheduler/__init__.py after probe returns Installable)
# ---------------------------------------------------------------------------


def spawn_ollama(binary_path: str) -> subprocess.Popen:
    """Spawn ``ollama serve`` WITHOUT detach flags.

    Deliberately omits:
      - Windows: DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
      - POSIX:   start_new_session=True

    This ensures the child process inherits our process group and is
    terminated when the parent exits (terminal-close path, §14.2 case 6).
    POSIX additionally passes ``preexec_fn`` from the lifecycle façade
    (returns None in this commit; harmless).

    The returned Popen handle is passed to ``register_owned_process()``.
    """
    from job_finder.web._process_lifecycle import make_pdeathsig_preexec_fn, register_owned_process

    kwargs: dict = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if sys.platform != "win32":
        preexec = make_pdeathsig_preexec_fn()
        if preexec is not None:
            kwargs["preexec_fn"] = preexec

    proc = subprocess.Popen([binary_path, "serve"], **kwargs)
    register_owned_process(proc)
    logger.info("Ollama spawned (pid=%s)", proc.pid)
    return proc
