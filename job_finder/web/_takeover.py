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

Both ``bare`` and ``serve`` now run the same **health-gated takeover**:

* a current instance answering ``/__jc_health`` → defer (a *serving* instance is
  never killed — replace one with ``job-cannon stop``);
* a Job-Cannon listener that does not answer ``/__jc_health`` but still serves
  *some* HTTP → a live pre-``/__jc_health`` instance → defer;
* a Job-Cannon listener that is **wedged** (socket held, but no HTTP response
  within a short timeout) → reap its whole process tree, then proceed;
* a foreign listener → refuse (exit non-zero with port guidance);
* a free port → proceed.

The only difference between the modes is the browser: a ``bare`` launch focuses
the browser when it defers (unless ``no_browser``); a ``serve`` supervisor
restart defers silently. ``serve`` no longer unconditionally reclaims the port
(PR5) — it defers to a healthy instance like everything else, so the supervisor
never kills a live app it should have left alone.
"""

from __future__ import annotations

import logging
import sys
import webbrowser
from enum import Enum

import requests

logger = logging.getLogger(__name__)

# Short, fixed timeout for the "is this listener actually serving HTTP?" probe.
# A healthy server answers in well under this; a wedged one (live process holding
# the socket but no longer servicing requests) never answers, so the timeout is
# what distinguishes "live, defer" from "wedged, reap".
_SERVES_HTTP_TIMEOUT = 2.0


class TakeoverAction(Enum):
    """What the caller should do after :func:`claim_or_takeover` returns."""

    PROCEED = "proceed"  # port free (or a wedged orphan was reaped) — caller binds
    EXIT_SUCCESS = "exit_0"  # a live JC is already serving — browser focused
    EXIT_FAILURE = "exit_1"  # a foreign process holds the port — caller exits non-zero


def _focus_browser(url: str, *, no_browser: bool) -> None:
    """Open ``url`` in the default browser unless suppressed.

    ``webbrowser`` is imported at module scope; tests that patch
    ``job_finder.__main__.webbrowser.open`` also affect this call because both
    modules reference the same ``webbrowser`` module object.
    """
    if not no_browser:
        webbrowser.open(url, new=2)


def _port_serves_http(url: str, *, timeout: float = _SERVES_HTTP_TIMEOUT) -> bool:
    """True if ``url`` returns *any* HTTP response within ``timeout``.

    Distinguishes a live instance (current or pre-``/__jc_health``; any status
    code counts) from a wedged orphan whose listening socket is held by a live
    process that no longer answers HTTP (connection failure / read timeout).

    Conservative on ambiguity: any non-connection ``RequestException`` is treated
    as "alive" so an unexpected error never escalates to reaping a process.
    """
    try:
        requests.get(url, timeout=timeout, allow_redirects=False)
    except (requests.ConnectionError, requests.Timeout):
        # Socket failure or no response within the timeout → wedged / not serving.
        # requests wraps raw socket OSErrors in ConnectionError, so these two
        # cover the "not serving" cases.
        return False
    except requests.RequestException:
        # Any other HTTP-level error (e.g. redirect loop, decode error) means we
        # DID reach a server → treat as alive and defer rather than reap.
        return True
    return True


def _print_port_occupied(port: int, listener_desc: str) -> None:
    """Print the bare-mode 'port occupied by a foreign process' guidance."""
    print(
        f"Job Cannon: port {port} is occupied by `{listener_desc}`.\n"
        f"  Use a different port:  job-cannon --port 5001\n"
        f"  Or set env var:        JOB_CANNON_PORT=5001 job-cannon\n"
        f"  Or edit config.yaml:   server.port: 5001\n"
        f"  Or stop the other process.",
        file=sys.stderr,
    )


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
            Both run the same health-gated takeover; the only difference is that
            ``serve`` never focuses a browser when it defers (a supervisor
            restart must stay headless).
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

    # Browser focus is for an interactive bare launch only. A supervisor `serve`
    # restart defers to a live instance silently — it must never pop a browser.
    focus = mode == "bare"

    # 1. A current instance answering /__jc_health → defer. A *serving* instance
    #    is never reaped — to replace one, use `job-cannon stop`. serve defers
    #    too: if a healthy instance is already up, the supervisor has nothing to
    #    do (this is the PR5 health-gating that replaced serve's unconditional
    #    free_jc_port reclaim, which used to kill even a healthy instance).
    if probe_existing_jc(url) is not None:
        print(f"Job Cannon is already running at {url}")
        if focus:
            _focus_browser(url, no_browser=no_browser)
        return TakeoverAction.EXIT_SUCCESS

    # 2. Nothing listening → free port → proceed.
    if not _port_is_listening(client_host, port):
        return TakeoverAction.PROCEED

    # 3. Something is listening — identify it.
    looks_like_jc, cmdline, listener_pid = _listener_looks_like_jc(client_host, port)
    if not looks_like_jc:
        listener_desc = (
            cmdline if cmdline else (f"PID {listener_pid}" if listener_pid else "unknown process")
        )
        _print_port_occupied(port, listener_desc)
        return TakeoverAction.EXIT_FAILURE

    # 4. A Job Cannon listener that does NOT answer /__jc_health. Distinguish a
    #    live pre-/__jc_health instance (serves some HTTP → defer) from a wedged
    #    orphan (listening socket, no HTTP response → reap).
    if _port_serves_http(url):
        print(f"Job Cannon (pre-upgrade instance, PID {listener_pid}) is running at {url}")
        if focus:
            _focus_browser(url, no_browser=no_browser)
        return TakeoverAction.EXIT_SUCCESS

    # 5. Wedged Job Cannon orphan — reap its whole process tree, then proceed.
    #    This is the core fix: bare launch now RECLAIMS a non-responsive orphan
    #    instead of deferring to it (which left it alive forever and stranded
    #    the user). free_jc_port re-confirms JC identity before terminating.
    print(f"Reclaiming a non-responsive Job Cannon instance on port {port} (PID {listener_pid})…")
    logger.warning(
        "Reaping wedged Job Cannon orphan on %s:%d (PID %s) — held the port but did not "
        "answer /__jc_health within %.1fs",
        client_host,
        port,
        listener_pid,
        _SERVES_HTTP_TIMEOUT,
    )
    from job_finder.web.supervisor import free_jc_port

    if not free_jc_port(client_host, port):
        # Race: the listener changed identity between checks → treat as occupied.
        listener_desc = (
            cmdline if cmdline else (f"PID {listener_pid}" if listener_pid else "unknown process")
        )
        _print_port_occupied(port, listener_desc)
        return TakeoverAction.EXIT_FAILURE
    return TakeoverAction.PROCEED
