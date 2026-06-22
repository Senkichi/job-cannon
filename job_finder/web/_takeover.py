"""Single pre-bind authority for "is a Job Cannon already serving this
``(host, port)``, and what should this launch do about it?"

Historically the already-running decision was scattered across three separate
steps in :func:`job_finder.__main__.main`:

* Step 0 (``serve`` only) — :func:`job_finder.web.supervisor.free_jc_port`
  reclaimed the port from a crashed JC orphan.
* Step 1 — the ``/__jc_health`` HTTP probe focused-and-exited on a live instance.
* Step 2 — a port-listening + psutil-cmdline gate focused-and-exited (or refused)
  on a pre-``/__jc_health`` instance.

Bare ``job-cannon`` ran only Steps 1-2 (it *deferred* to an orphan instead of
reclaiming the port), while ``serve`` ran Step 0 first. That split is the root
of the multi-process accumulation problem: the only routine that actually reaps
an orphan tree (``free_jc_port``) was reachable only via ``serve``, a command
the user never types.

This module folds those three steps into one function, :func:`claim_or_takeover`,
that every launch path calls before binding.

PR1 is **behaviour-neutral**: ``mode="bare"`` reproduces the legacy
focus-and-exit Steps 1-2 exactly, and ``mode="serve"`` reproduces the legacy
``free_jc_port`` reclaim exactly. Later PRs converge both modes onto a single
health-gated takeover (a wedged orphan is reaped rather than deferred to).
"""

from __future__ import annotations

import logging
import sys
import webbrowser
from enum import Enum

logger = logging.getLogger(__name__)


class TakeoverAction(Enum):
    """What the caller should do after :func:`claim_or_takeover` returns."""

    PROCEED = "proceed"  # port free (or a wedged orphan was reaped) — caller binds
    EXIT_SUCCESS = "exit_0"  # a healthy JC is already serving — browser focused
    EXIT_FAILURE = "exit_1"  # a foreign process holds the port — caller exits non-zero


def _focus_browser(url: str, *, no_browser: bool) -> None:
    """Open ``url`` in the default browser unless suppressed.

    ``webbrowser`` is imported at module scope; tests that patch
    ``job_finder.__main__.webbrowser.open`` also affect this call because both
    modules reference the same ``webbrowser`` module object.
    """
    if not no_browser:
        webbrowser.open(url, new=2)


def claim_or_takeover(
    client_host: str,
    port: int,
    url: str,
    *,
    mode: str,
    no_browser: bool = False,
) -> TakeoverAction:
    """Decide what a launch should do about anything already on ``(host, port)``.

    Args:
        client_host: Client-facing host (loopback after the wildcard-bind split).
        port: Port the app intends to bind.
        url: User-facing base URL (e.g. ``http://127.0.0.1:5000``).
        mode: ``"bare"`` (interactive ``job-cannon``) or ``"serve"`` (supervisor).
        no_browser: Suppress the browser focus (``JOB_CANNON_NO_BROWSER``).

    Returns:
        A :class:`TakeoverAction` the caller acts on (proceed / exit 0 / exit 1).
    """
    # Lazy imports: these primitives live in job_finder.__main__, which imports
    # this module — importing them at module scope would create a cycle. This
    # mirrors the existing lazy-import pattern in job_finder.web.supervisor.
    from job_finder.__main__ import (
        _listener_looks_like_jc,
        _port_is_listening,
        probe_existing_jc,
    )

    if mode == "serve":
        # Supervisor entry — TAKE OVER: reclaim the port from a confirmed-JC
        # orphan (free_jc_port kills the whole identified process tree). A
        # foreign listener is never killed; the caller surfaces guidance.
        from job_finder.web.supervisor import free_jc_port

        if free_jc_port(client_host, port):
            return TakeoverAction.PROCEED
        print(
            f"job-cannon serve: port {port} is occupied by a process that is "
            f"not Job Cannon — refusing to kill it.\n"
            f"  Use a different port:  job-cannon serve --port 5001\n"
            f"  Or stop the other process.",
            file=sys.stderr,
        )
        return TakeoverAction.EXIT_FAILURE

    # mode == "bare": legacy Steps 1-2 — focus-and-exit on a live instance, no
    # reaping. (PR2 converges this onto the health-gated takeover.)
    #
    # Step 1: HTTP probe — matches post-plan instances responding at /__jc_health.
    if probe_existing_jc(url) is not None:
        print(f"Job Cannon is already running at {url}")
        _focus_browser(url, no_browser=no_browser)
        return TakeoverAction.EXIT_SUCCESS

    # Step 2: port-listening + psutil cmdline — matches pre-plan instances during
    # the upgrade window (no /__jc_health endpoint yet).
    if _port_is_listening(client_host, port):
        looks_like_jc, cmdline, listener_pid = _listener_looks_like_jc(client_host, port)
        if looks_like_jc:
            print(f"Job Cannon (pre-upgrade instance, PID {listener_pid}) is running at {url}")
            _focus_browser(url, no_browser=no_browser)
            return TakeoverAction.EXIT_SUCCESS
        listener_desc = (
            cmdline if cmdline else (f"PID {listener_pid}" if listener_pid else "unknown process")
        )
        print(
            f"Job Cannon: port {port} is occupied by `{listener_desc}`.\n"
            f"  Use a different port:  job-cannon --port 5001\n"
            f"  Or set env var:        JOB_CANNON_PORT=5001 job-cannon\n"
            f"  Or edit config.yaml:   server.port: 5001\n"
            f"  Or stop the other process.",
            file=sys.stderr,
        )
        return TakeoverAction.EXIT_FAILURE

    return TakeoverAction.PROCEED
