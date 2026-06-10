"""Provider auto-detection — liveness probes for subscription-leveraged CLIs.

Probes claude, gemini, and ollama in priority order and returns a ranked
list of ProviderHandle instances. Results are cached for the process
lifetime; pass refresh=True to re-probe (e.g., after the wizard installs
a CLI mid-session).

Detection ordering (CONTEXT.md D-03, memory project_public_release_provider_priority):
  1. claude_code_cli (priority=1) — `claude -p "ping"` with 30s timeout
  2. gemini_cli     (priority=2) — `gemini -p "ping" --output-format json` (30s; quota-tolerant)
  3. ollama         (priority=3) — `ollama list` (>=2 lines)
  4. local_bundled  (priority=4) — file existence check for [local-ai] extra

`local_bundled` is offered when the [local-ai] extra module file is present
(without importing llama_cpp, which is lazy).

Timeout note (Issue #288): The probe timeout was raised from 10s to 30s. The
headline $0 CLIs (claude, gemini) do a network round-trip on first call and
commonly exceed 10s on cold start, authenticated, in-daily-use machines.
Detection is still synchronous — shutil.which pre-filters so only binaries
that exist on PATH get deep-probed, keeping wall time low on typical setups.

Ollama "installed but no models" is surfaced as a distinct DetectionExtra
(ollama_no_model=True) so the wizard can render inline pull guidance rather
than treating the install as absent.

Security invariants (CONTEXT.md D-09):
    - subprocess.run list-form argv, never shell=True
    - binary path from shutil.which() (validates PATH membership)
    - explicit timeout=30 on every subprocess.run call
"""

from __future__ import annotations

import importlib.util
import logging
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from job_finder.web.claude_client import _resolve_cli_binary

logger = logging.getLogger(__name__)

# Probe timeout raised from 10s → 30s (Issue #288): the $0 CLIs do a
# network handshake on first use and regularly exceed 10s on cold start.
_PROBE_TIMEOUT: int = 30


@dataclass(frozen=True, slots=True)
class ProviderHandle:
    """Description of an available provider, surfaced to the wizard / Settings UI."""

    name: str  # "claude_code_cli" | "gemini_cli" | "ollama" | "local_bundled"
    binary_path: str  # absolute path from shutil.which() (or module path for local_bundled)
    cost_label: str  # human-readable for wizard UI
    priority: int  # lower = preferred (1=claude_code_cli, 2=gemini_cli, 3=ollama, 4=local_bundled)


@dataclass(frozen=True, slots=True)
class DetectionExtras:
    """Side-channel state produced by detection that is NOT a ProviderHandle.

    Used to surface actionable states (e.g. "Ollama installed but no model
    pulled yet") to the wizard UI without polluting the ProviderHandle list
    that the scoring cascade consumes.
    """

    ollama_no_model: bool = field(default=False)
    """True when `ollama` binary is on PATH and `ollama list` exits 0 but
    returns only a header row (no models pulled yet).  The wizard renders
    inline ``ollama pull qwen2.5:14b`` guidance + a Re-check button."""


# Module-level cache — process-lifetime; no TTL eviction (CONTEXT.md D-03).
# Pass detect_available_providers(refresh=True) to re-probe.
_detection_cache: dict[str, ProviderHandle | None] = {}
_extras_cache: DetectionExtras | None = None

_QUOTA_HINTS: tuple[str, ...] = ("quota", "rate limit", "capacity", "429")


def _probe_cli(
    binary_name: str,
    argv_template: list[str],
    *,
    quota_tolerant: bool = False,
    extra_ok: Callable[[subprocess.CompletedProcess], bool] = lambda _: True,
) -> tuple[str, subprocess.CompletedProcess] | None:
    """Run a liveness probe for a CLI binary.

    Timeout is _PROBE_TIMEOUT (30s — raised from 10s in Issue #288 because the
    $0 CLIs do a network round-trip on cold start).

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
            timeout=_PROBE_TIMEOUT,
            encoding="utf-8",
            errors="replace",
        )
    except (subprocess.TimeoutExpired, OSError):
        logger.debug("%s liveness probe timed out / OS error", binary_name)
        return None
    if result.returncode != 0:
        stderr_lower = (result.stderr or "").lower()
        if not quota_tolerant or not any(h in stderr_lower for h in _QUOTA_HINTS):
            logger.debug("%s liveness probe non-zero rc=%s", binary_name, result.returncode)
            return None
    if not extra_ok(result):
        return None
    return p, result


def _check_claude_code() -> ProviderHandle | None:
    out = _probe_cli(
        "claude",
        ["-p", "ping", "--output-format", "json", "--no-session-persistence", "--tools", ""],
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


def _non_empty_lines(stdout: str) -> list[str]:
    return [ln for ln in (stdout or "").splitlines() if ln.strip()]


def _check_ollama() -> ProviderHandle | None:
    """Detect a working Ollama install with at least one model.

    Returns None both when Ollama is absent AND when it is installed but has
    no models.  Use detect_extras() to distinguish the two cases — when the
    binary exists but the model list is empty, DetectionExtras.ollama_no_model
    is True and the wizard renders inline pull guidance.
    """
    out = _probe_cli(
        "ollama",
        ["list"],
        extra_ok=lambda r: len(_non_empty_lines(r.stdout)) >= 2,
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


def _check_ollama_no_model() -> bool:
    """Return True when Ollama binary exists, daemon responds, but no models are pulled.

    This is the "Ollama installed but empty" state (Issue #288). The wizard
    renders an inline ``ollama pull qwen2.5:14b`` prompt + Re-check button
    rather than hiding Ollama entirely.

    Only probes if shutil.which finds the binary; avoids a duplicate
    subprocess call when _check_ollama already found >=2 lines.
    """
    if not shutil.which("ollama"):
        return False
    p = _resolve_cli_binary("ollama")
    try:
        result = subprocess.run(
            [p, "list"],
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT,
            encoding="utf-8",
            errors="replace",
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    if result.returncode != 0:
        return False
    lines = _non_empty_lines(result.stdout)
    # Exactly one line = header row only, no models
    return len(lines) == 1


# _LOCAL_BUNDLED_MODULE_PATH is the sentinel file; its presence means the
# [local-ai] extra was installed (llama_cpp wheel exists in the venv).
# We deliberately do NOT import llama_cpp here — the import is lazy inside
# LocalBundledProvider.__init__ — so this file-existence probe never loads
# the ~200 MB shared library.
_LOCAL_BUNDLED_MODULE_PATH = Path(__file__).parent / "local_bundled.py"


def _check_local_bundled() -> ProviderHandle | None:
    """Detect the [local-ai] extra by checking if local_bundled.py exists.

    The module file is present only when `uv sync --extra local-ai` has been
    run. We surface it in the wizard so users who installed the extra can
    select it without knowing the internal provider name.

    Note: selecting local_bundled in the wizard does NOT configure a
    model_path — that must be done in Settings. The wizard simply writes
    providers.primary = "local_bundled" so the user doesn't have to hunt
    for the key name.
    """
    if not _LOCAL_BUNDLED_MODULE_PATH.exists():
        return None
    # Additionally check that llama_cpp is importable (the extra's wheel).
    # Use importlib.util.find_spec which doesn't execute module code.
    try:
        spec = importlib.util.find_spec("llama_cpp")
        if spec is None:
            return None
    except (ModuleNotFoundError, ValueError):
        return None
    return ProviderHandle(
        name="local_bundled",
        binary_path=str(_LOCAL_BUNDLED_MODULE_PATH),
        cost_label="$0 (CPU-local GGUF inference via llama-cpp-python)",
        priority=4,
    )


# Probe registry — the iteration order matches priority order.
_PROBES: list[tuple[str, Callable[[], ProviderHandle | None]]] = [
    ("claude_code_cli", _check_claude_code),
    ("gemini_cli", _check_gemini_cli),
    ("ollama", _check_ollama),
    ("local_bundled", _check_local_bundled),
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
    global _extras_cache

    if not refresh and _detection_cache:
        return sorted(
            [h for h in _detection_cache.values() if h is not None],
            key=lambda h: h.priority,
        )

    _detection_cache.clear()
    for key, check_fn in _PROBES:
        _detection_cache[key] = check_fn()

    # Compute extras: ollama_no_model only when ollama probe returned None
    # (binary exists but no models) vs truly absent.
    ollama_no_model = False
    if _detection_cache.get("ollama") is None:
        ollama_no_model = _check_ollama_no_model()
    _extras_cache = DetectionExtras(ollama_no_model=ollama_no_model)

    return sorted(
        [h for h in _detection_cache.values() if h is not None],
        key=lambda h: h.priority,
    )


def get_detection_extras() -> DetectionExtras:
    """Return the side-channel detection state from the most recent probe run.

    Always call detect_available_providers() first (or trust the cache).
    Returns a zero-value DetectionExtras if detection has not been run yet.
    """
    if _extras_cache is None:
        return DetectionExtras()
    return _extras_cache
