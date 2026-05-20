"""One-shot CLI: migrate plaintext secrets from config.yaml to the OS keyring.

Idempotent — re-running after a successful migration prints
"nothing to migrate" and exits 0. Use ``--dry-run`` to preview without
writing. Use ``--force`` to skip the keyring-availability probe (helpful
on hosts where the probe is flaky but write+read in fact succeeds).

Usage::

    python -m job_finder.migrate_secrets
    python -m job_finder.migrate_secrets --dry-run
    python -m job_finder.migrate_secrets --force

Config path resolution follows ``job_finder.config.load_config``:
explicit arg first, then ``$JOB_CANNON_CONFIG``, then
``user_data_dirs.config_path()``.
"""

from __future__ import annotations

import argparse
import logging
import sys

from job_finder import secrets as jf_secrets
from job_finder.config import load_config
from job_finder.web import user_data_dirs

logger = logging.getLogger(__name__)


def _scrub_secret_in_place(config: dict, dotted_path: str) -> None:
    """Set the secret leaf to "" inside `config` (mutates in place)."""
    parts = dotted_path.split(".")
    node: object = config
    for p in parts[:-1]:
        if not isinstance(node, dict) or p not in node:
            return
        node = node[p]
    if isinstance(node, dict) and parts[-1] in node:
        node[parts[-1]] = ""


def _find_plaintext_secrets(config: dict) -> dict[str, str]:
    """Return {canonical_name: plaintext_value} for every populated secret."""
    found: dict[str, str] = {}
    for name in jf_secrets.SECRET_ENV_VARS:
        v = jf_secrets._walk_config(config, name)
        if v:
            found[name] = v
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Migrate plaintext secrets from config.yaml to the OS keyring."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be migrated without writing anything.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Proceed even if the keyring-availability probe failed.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    keyring_ok = jf_secrets.probe_keyring_backend()
    if not keyring_ok and not args.force:
        print(
            "ERROR: OS keyring backend is unavailable. "
            "Install gnome-keyring/kwallet (Linux) or pass --force to override.",
            file=sys.stderr,
        )
        return 1

    config_path = user_data_dirs.config_path()
    try:
        config = load_config(str(config_path), allow_missing=False)
    except FileNotFoundError:
        print(f"ERROR: config.yaml not found at {config_path}", file=sys.stderr)
        return 1

    found = _find_plaintext_secrets(config)
    if not found:
        print("No plaintext secrets found in config.yaml — nothing to migrate.")
        return 0

    print(f"Found {len(found)} plaintext secret(s):")
    for name in found:
        print(f"  {name}")

    if args.dry_run:
        print("Dry-run: not modifying anything.")
        return 0

    # --force was passed but the probe failed earlier — re-clear the unavailable
    # flag so set_secret() doesn't refuse the write. If the underlying backend
    # is truly broken the keyring call itself will raise.
    if args.force and not keyring_ok:
        jf_secrets._KEYRING_UNAVAILABLE = False

    for name, value in found.items():
        jf_secrets.set_secret(name, value)
        _scrub_secret_in_place(config, name)

    # Atomic write via the same helper Settings.save uses — matches the
    # temp-file + os.replace + POSIX-chmod-0600 contract.
    from job_finder.web.blueprints.settings import _write_config

    _write_config(config, str(config_path))

    print(
        f"Migrated {len(found)} secret(s) to OS keyring; "
        "cleared plaintext values from config.yaml."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
