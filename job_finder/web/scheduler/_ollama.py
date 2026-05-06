"""Ollama auto-start helper for the background scheduler.

Agentic backfill runs nightly at 3:30 AM and requires Ollama. This helper
probes the service at scheduler init and spawns a detached ``ollama serve``
process when the probe fails. Best-effort; never raises.
"""

import logging
import os
import subprocess
import sys
import time
from pathlib import Path

logger = logging.getLogger(__name__)


def _ensure_ollama_running(config: dict, *, poll_seconds: int = 30) -> None:
    """Ensure Ollama is reachable; auto-start 'ollama serve' if not.

    Binary location resolves in order:
      1. $OLLAMA_EXE environment variable (user override)
      2. %LOCALAPPDATA%\\Programs\\Ollama\\ollama.exe (default Windows install)
      3. 'ollama' on PATH

    Args:
        config: Full JF_CONFIG dict; passed to OllamaProvider for base_url.
        poll_seconds: Max seconds to wait for Ollama to come up after spawning.
    """
    try:
        from job_finder.web.providers.ollama_provider import OllamaProvider
    except ImportError as exc:
        logger.debug("Ollama auto-start skipped (provider import failed): %s", exc)
        return

    try:
        OllamaProvider(config=config)  # health check inside __init__
        logger.debug("Ollama: already running, skipping auto-start")
        return
    except RuntimeError:
        pass  # not running — try to start it

    # Locate the binary
    ollama_exe = os.environ.get("OLLAMA_EXE", "").strip() or None
    if not ollama_exe:
        localappdata = os.environ.get("LOCALAPPDATA", "")
        if localappdata:
            default_path = Path(localappdata) / "Programs" / "Ollama" / "ollama.exe"
            if default_path.exists():
                ollama_exe = str(default_path)
    if not ollama_exe:
        # Fall back to PATH lookup (Linux/macOS or Windows with PATH entry)
        import shutil

        ollama_exe = shutil.which("ollama")

    if not ollama_exe:
        logger.warning(
            "Ollama auto-start skipped: binary not found. Set OLLAMA_EXE env var or "
            "install Ollama (https://ollama.com/download). Agentic backfill will be disabled."
        )
        return

    try:
        if sys.platform == "win32":
            # Detach fully so Ollama outlives the Flask process
            DETACHED_PROCESS = 0x00000008
            CREATE_NEW_PROCESS_GROUP = 0x00000200
            subprocess.Popen(
                [ollama_exe, "serve"],
                creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
        else:
            subprocess.Popen(
                [ollama_exe, "serve"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                start_new_session=True,
            )
    except Exception as exc:
        logger.warning("Ollama auto-start failed to spawn '%s serve': %s", ollama_exe, exc)
        return

    # Poll for readiness
    for attempt in range(poll_seconds):
        try:
            OllamaProvider(config=config)
            logger.info("Ollama auto-started successfully after %ds", attempt + 1)
            return
        except RuntimeError:
            time.sleep(1)

    logger.warning(
        "Ollama did not become ready within %ds of auto-start. Agentic backfill may fail tonight.",
        poll_seconds,
    )
