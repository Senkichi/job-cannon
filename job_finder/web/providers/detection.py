"""Provider auto-detection — liveness probes for subscription-leveraged CLIs.

Probes claude, gemini, and ollama in priority order and returns a ranked
list of ProviderHandle instances. Results are cached for the process
lifetime; pass refresh=True to re-probe (e.g., after the wizard installs
a CLI mid-session).

Detection ordering (CONTEXT.md D-03, memory project_public_release_provider_priority):
  1. claude_code_cli (priority=1) — `claude -p "ping"` with 10s timeout
  2. gemini_cli     (priority=2) — `gemini -p "ping" --output-format json` (10s; quota-tolerant)
  3. ollama         (priority=3) — `ollama list` (>=2 lines)

`local_bundled` is intentionally NOT auto-detected — it requires an explicit
GGUF model_path that the wizard provides (Phase 42).

Security invariants (CONTEXT.md D-09):
    - subprocess.run list-form argv, never shell=True
    - binary path from shutil.which() (validates PATH membership)
    - explicit timeout=10 on every subprocess.run call
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass

from job_finder.web.claude_client import _resolve_cli_binary

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ProviderHandle:
    """Description of an available provider, surfaced to the wizard / Settings UI."""

    name: str          # "claude_code_cli" | "gemini_cli" | "ollama"
    binary_path: str   # absolute path from shutil.which()
    cost_label: str    # human-readable for wizard UI
    priority: int      # lower = preferred (1=claude_code_cli, 2=gemini_cli, 3=ollama)


# Module-level cache — process-lifetime; no TTL eviction (CONTEXT.md D-03).
# Pass detect_available_providers(refresh=True) to re-probe.
_detection_cache: dict[str, ProviderHandle | None] = {}

_QUOTA_HINTS: tuple[str, ...] = ("quota", "rate limit", "capacity", "429")


def _probe_cli(
    binary_name: str,
    argv_template: list[str],
    *,
    quota_tolerant: bool = False,
    extra_ok: Callable[[subprocess.CompletedProcess], bool] = lambda _: True,
) -> tuple[str, subprocess.CompletedProcess] | None:
    """Run a 10-second liveness probe for a CLI binary.

    Returns (resolved_binary_path, completed_process) on success, None on
    timeout/OS-error/non-zero exit (with quota_tolerant carve-out for stderr
    hints in _QUOTA_HINTS) or when extra_ok rejects the result.
    """
    if not shutil.which(binary_name):
        return None
    p = _resolve_cli_binary(binary_name)
    try:
        result = subprocess.run(
            [p, *argv_template],
            capture_output=True,
            text=True,
            timeout=10,
            encoding="utf-8",
            errors="replace",
        )
    except (subprocess.TimeoutExpired, OSError):
        logger.debug("%s liveness probe timed out / OS error", binary_name)
        return None
    if result.returncode != 0:
        stderr_lower = (result.stderr or "").lower()
        if not quota_tolerant or not any(h in stderr_lower for h in _QUOTA_HINTS):
            logger.debug(
                "%s liveness probe non-zero rc=%s", binary_name, result.returncode
            )
            return None
    if not extra_ok(result):
        return None
    return p, result


def _check_claude_code() -> ProviderHandle | None:
    out = _probe_cli(
        "claude",
        ["-p", "ping", "--output-format", "json",
         "--no-session-persistence", "--tools", ""],
    )
    if out is None:
        return None
    p, _ = out
    return ProviderHandle(
        name="claude_code_cli",
        binary_path=p,
        cost_label="$0 (uses your Claude.ai subscription)",
        priority=1,
    )


def _check_gemini_cli() -> ProviderHandle | None:
    out = _probe_cli(
        "gemini",
        ["-p", "ping", "--output-format", "json"],
        quota_tolerant=True,
    )
    if out is None:
        return None
    p, _ = out
    return ProviderHandle(
        name="gemini_cli",
        binary_path=p,
        cost_label="$0 (uses your Google AI Studio free tier)",
        priority=2,
    )


def _check_ollama() -> ProviderHandle | None:
    out = _probe_cli(
        "ollama",
        ["list"],
        extra_ok=lambda r: len(
            [ln for ln in (r.stdout or "").splitlines() if ln.strip()]
        ) >= 2,
    )
    if out is None:
        return None
    p, _ = out
    return ProviderHandle(
        name="ollama",
        binary_path=p,
        cost_label="$0 (local inference, no API quota)",
        priority=3,
    )


# Probe registry — the iteration order matches priority order.
_PROBES: list[tuple[str, Callable[[], ProviderHandle | None]]] = [
    ("claude_code_cli", _check_claude_code),
    ("gemini_cli", _check_gemini_cli),
    ("ollama", _check_ollama),
]


def detect_available_providers(*, refresh: bool = False) -> list[ProviderHandle]:
    """Return available providers in priority order.

    Results are cached for the process lifetime. Pass `refresh=True` to
    re-probe (e.g., after the user installs a CLI during the wizard).

    Args:
        refresh: If True, clear the cache and re-run all probes.

    Returns:
        Sorted list of ProviderHandle (lowest priority value first).
    """
    if not refresh and _detection_cache:
        return sorted(
            [h for h in _detection_cache.values() if h is not None],
            key=lambda h: h.priority,
        )

    _detection_cache.clear()
    for key, check_fn in _PROBES:
        _detection_cache[key] = check_fn()

    return sorted(
        [h for h in _detection_cache.values() if h is not None],
        key=lambda h: h.priority,
    )
