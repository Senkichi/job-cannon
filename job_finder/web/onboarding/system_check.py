"""First-run system diagnostics (STRANGE-WIZ-03, Phase 42).

Three checks run from the welcome route GET handler — D-10 makes them warning-only,
failures do not block advance. Diagnostic strings per D-11 MUST name the failing entity
(file path, port number, host name) so the user sees what to fix.

Stdlib-only — no new dependencies. Uses Path.touch()/unlink() for the DB-writable probe
because os.access(p, os.W_OK) is unreliable on Windows.
"""

from __future__ import annotations

import logging
import socket
from dataclasses import dataclass
from pathlib import Path

from job_finder.web import user_data_dirs

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CheckResult:
    """Outcome of a single system check.

    Attributes:
        name: Short display label (e.g., "DB writable", "Port 5000 free", "Network reachable").
        ok: True if the check passed.
        detail: Diagnostic string. On failure, names the failing entity per D-11.
            On success, contains the verified entity (DB path / port / host) for context.
    """

    name: str
    ok: bool
    detail: str


# --- D-11 strings ---

def check_db_writable() -> CheckResult:
    """Touch-and-unlink probe in the user_data_root parent of jobs.db.

    Names the file path on failure (D-11 — "DB-not-writable names the file path").
    Uses Path.touch()/unlink() — os.access(p, os.W_OK) lies on Windows.
    """
    try:
        db_file = user_data_dirs.db_path()
    except Exception as e:
        return CheckResult("DB writable", False, f"could not resolve user_data_root: {e}")

    try:
        db_file.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return CheckResult("DB writable", False, f"{db_file}: cannot create parent dir: {e}")

    probe = db_file.parent / ".write_probe_phase42"
    try:
        probe.touch()
        probe.unlink()
    except OSError as e:
        return CheckResult("DB writable", False, f"{db_file}: {e}")

    return CheckResult("DB writable", True, str(db_file))


def check_port_free(port: int = 5000) -> CheckResult:
    """Check that 127.0.0.1:port has no listener.

    Names the port number in BOTH ok and !ok paths (D-11 — "port-conflict names the
    conflicting port").
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            # connect_ex returns 0 if connection succeeded — port IS taken
            result = s.connect_ex(("127.0.0.1", port))
    except OSError as e:
        return CheckResult(f"Port {port} free", False, f"Port {port}: socket probe failed: {e}")

    if result == 0:
        return CheckResult(f"Port {port} free", False, f"Port {port} is in use by another process")
    return CheckResult(f"Port {port} free", True, f"Port {port} available")


def check_network(host: str = "imap.gmail.com", timeout: float = 2.0) -> CheckResult:
    """DNS-resolve `host` to confirm outbound network reachability.

    Names the host on BOTH ok and !ok paths (D-11 — "no-network names the host that
    failed; try imap.gmail.com first since IMAP is the default ingest").
    """
    original_timeout = socket.getdefaulttimeout()
    try:
        socket.setdefaulttimeout(timeout)
        try:
            resolved = socket.gethostbyname(host)
        finally:
            socket.setdefaulttimeout(original_timeout)
    except socket.gaierror as e:
        return CheckResult("Network reachable", False, f"{host}: name resolution failed ({e})")
    except OSError as e:
        return CheckResult("Network reachable", False, f"{host}: socket error ({e})")

    return CheckResult("Network reachable", True, f"{host} → {resolved}")


def run_all() -> list[CheckResult]:
    """Run all three checks in order; never raise (warning-only per D-10).

    Returns a list so the welcome template can iterate and render each line independently,
    which prevents a single failure from hiding the others (D-11 last sentence).
    """
    return [check_db_writable(), check_port_free(), check_network()]
