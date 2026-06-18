"""Read-only prep for the #306 git-history purge (issue #462).

Issue #306 (v5 audit B9) calls for a one-time ``git filter-repo`` purge of
internal planning/eval artifacts from public git history before launch. The
companion untracking work has already landed (``.planning/*`` is gitignored
and the root planning docs are no longer tracked), but the artifacts still
live in *history*. This script produces everything a human needs to run the
(manual) rewrite:

  * precondition checks (filter-repo installed, ``.planning/`` contents
    gitignored, root planning docs no longer tracked),
  * a path inventory (per-prefix add-counts) cross-checked against floors so a
    drifted repo aborts before the human invests in a rewrite,
  * the exact ``git filter-repo --invert-paths …`` invocation, and
  * the post-rewrite verification greps.

NON-DESTRUCTIVE BY CONTRACT. This script only runs read-only inventory /
analyze commands and prints the rewrite command as text. It NEVER executes the
rewrite, force-pushes, or toggles branch protection — those remain the human
leaf of #306 (see ``docs/RUNBOOK-history-purge-306.md``).

Usage:
    uv run python scripts/prep_history_purge_306.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass

# --- Strip targets (LOCKED — see #306 / #462 scope guards) -----------------
# The exact ``--path`` arguments handed to ``git filter-repo --invert-paths``.
# CRITICAL: strip ``round_0/jd/`` ONLY, never the bare ``round_0/`` — the
# sibling ``round_0/dedup_keys.json`` is intentionally retained via a
# ``.gitignore`` negation and must survive the rewrite.
STRIP_PATHS: tuple[str, ...] = (
    ".planning/",
    "PLAN.md",
    "FOLLOWUPS.md",
    "JD-LAYER2-PLAN.md",
    "evals/cascade_audit/artifacts/round_0/jd/",
)

# Path that MUST survive the rewrite — guarded against accidental inclusion.
RETAINED_PATH = "evals/cascade_audit/artifacts/round_0/dedup_keys.json"

# The three root planning docs (added once each → expect exactly 3).
ROOT_DOCS: frozenset[str] = frozenset({"PLAN.md", "FOLLOWUPS.md", "JD-LAYER2-PLAN.md"})

# The #306 acceptance grep — must return nothing post-rewrite.
VERIFICATION_GREP = (
    "git log --all --name-only | "
    'grep -E "^\\.planning/|^PLAN\\.md|^FOLLOWUPS\\.md|^JD-LAYER2-PLAN\\.md|round_0/jd/"'
)


# --- Structured results (immutable) ----------------------------------------
@dataclass(frozen=True)
class Check:
    """A single precondition outcome."""

    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class PreconditionResult:
    """Outcome of all precondition checks (a fresh object per call)."""

    checks: tuple[Check, ...]

    @property
    def ok(self) -> bool:
        return all(c.passed for c in self.checks)


@dataclass(frozen=True)
class PrefixCount:
    """A per-prefix add-count with its sanity floor."""

    label: str
    count: int
    floor: int

    @property
    def ok(self) -> bool:
        return self.count >= self.floor


@dataclass(frozen=True)
class Inventory:
    """The history inventory (a fresh object per call)."""

    counts: tuple[PrefixCount, ...]

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.counts)


def _run(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Run a read-only command, capturing output.

    Every external command is routed through this single helper so tests can
    patch it and assert that no destructive command (``filter-repo
    --invert-paths``, ``push --force``, branch-protection mutation) is ever
    issued.
    """
    return subprocess.run(list(args), capture_output=True, text=True, check=False)


def verify_preconditions() -> PreconditionResult:
    """Assert the repo is in the expected pre-rewrite state.

    Returns a fresh :class:`PreconditionResult`; never mutates shared state.
    """
    checks: list[Check] = []

    # 1. git filter-repo binary present.
    # The #462 spec named ``git filter-repo --help`` exit 0, but that is
    # unreliable on Git-for-Windows (``--help`` opens an HTML doc that may be
    # absent → exit 128) and ``--version`` prints a bare build hash. So we
    # check the binary on PATH OR a clean ``--version`` exit code — exit only,
    # never the garbage stdout. The acceptance criterion ("all checks PASS on
    # the current repo") forces the portable check over the literal one.
    on_path = shutil.which("git-filter-repo") is not None
    version_ok = _run(["git", "filter-repo", "--version"]).returncode == 0
    present = on_path or version_ok
    checks.append(
        Check(
            "git filter-repo installed",
            present,
            f"on PATH={on_path}, `--version` exit 0={version_ok}",
        )
    )

    # 2. .planning/ contents gitignored. The pattern is ``.planning/*`` (glob
    # the contents, not the directory), so ``git check-ignore .planning``
    # returns 1 — we probe a path UNDER .planning instead (a generic path, not
    # the negated/tracked NEXT_STEPS_ATS_COVERAGE.md exception).
    ignored = _run(["git", "check-ignore", ".planning/_purge_probe.tmp"]).returncode == 0
    checks.append(
        Check(
            ".planning/ contents gitignored",
            ignored,
            "git check-ignore .planning/<path> -> exit 0",
        )
    )

    # 3. Root planning docs no longer tracked.
    tracked = _run(["git", "ls-files", *sorted(ROOT_DOCS)]).stdout.strip()
    untracked = tracked == ""
    checks.append(
        Check(
            "root planning docs untracked",
            untracked,
            f"git ls-files -> {tracked!r}",
        )
    )

    return PreconditionResult(checks=tuple(checks))


def _added_paths() -> set[str]:
    """Distinct paths ever ADDED across all refs (``--diff-filter=A``)."""
    out = _run(
        ["git", "log", "--all", "--diff-filter=A", "--name-only", "--pretty=format:"]
    ).stdout
    return {line.strip() for line in out.splitlines() if line.strip()}


def build_inventory() -> Inventory:
    """Count history adds per strip-prefix and cross-check against floors.

    Also kicks off ``git filter-repo --analyze`` (read-only; writes a report
    under ``.git/filter-repo/analysis/`` without rewriting history) as a
    best-effort aid for the human. Returns a fresh :class:`Inventory`.

    Floors sit just below the verified live counts (182 / 3 / 24 as of
    2026-06-18). The ``round_0/jd/`` floor is 20, not the spec's "~25": the
    real distinct add-count is 24, so a 25 floor would false-fail a healthy
    repo. The floors exist to catch a *collapsed* (drifted-to-zero) cohort,
    not to assert an exact magnitude.
    """
    added = _added_paths()
    planning = sum(1 for p in added if p.startswith(".planning/"))
    root_docs = sum(1 for p in added if p in ROOT_DOCS)
    jd = sum(1 for p in added if p.startswith("evals/cascade_audit/artifacts/round_0/jd/"))

    # Best-effort analyze report; never fatal (and never destructive).
    _run(["git", "filter-repo", "--analyze"])

    counts = (
        PrefixCount(".planning/", planning, 180),
        PrefixCount("root planning docs", root_docs, 3),
        PrefixCount("evals/.../round_0/jd/", jd, 20),
    )
    return Inventory(counts=counts)


def build_filter_repo_command() -> str:
    """Return the exact (NON-EXECUTED) ``git filter-repo`` invocation."""
    paths = " ".join(f"--path {p}" for p in STRIP_PATHS)
    return f"git filter-repo --invert-paths {paths}"


def print_runbook(inventory: Inventory) -> str:
    """Render and print the human runbook. Returns the rendered text.

    Pure string assembly — issues no commands, so it cannot be destructive.
    """
    cmd = build_filter_repo_command()
    counts = "\n".join(f"  - {c.label}: {c.count} (floor {c.floor})" for c in inventory.counts)
    text = f"""\
================================================================================
 #306 HISTORY-PURGE RUNBOOK (generated by scripts/prep_history_purge_306.py)
================================================================================

Inventory of paths to strip from history:
{counts}

The artifacts retained on purpose (do NOT strip):
  - {RETAINED_PATH}

--------------------------------------------------------------------------------
STEP 1 - Work on a THROWAWAY fresh clone (filter-repo rewrites every SHA):
--------------------------------------------------------------------------------
  git clone --no-local <origin-url> jc-history-purge
  cd jc-history-purge
  git filter-repo --analyze      # optional: inspect .git/filter-repo/analysis/

--------------------------------------------------------------------------------
STEP 2 - Run the exact rewrite invocation:
--------------------------------------------------------------------------------
  {cmd}

--------------------------------------------------------------------------------
STEP 3 - Verify the purge (this grep must return NOTHING):
--------------------------------------------------------------------------------
  {VERIFICATION_GREP}

  Also confirm the intentionally-retained file SURVIVES:
  git log --all --name-only | grep -F "{RETAINED_PATH}"   # must still appear

================================================================================
 STOP - the destructive force-push to protected `main` is the HUMAN step (#306).
 This script does NOT force-push and does NOT toggle branch protection. Disable
 branch protection, force-push the rewritten refs + tags, re-enable protection,
 then follow the aftermath checklist in docs/RUNBOOK-history-purge-306.md.
 NOTE: anyone who already cloned the repo retains the purged data.
================================================================================
"""
    print(text)
    return text


def main() -> int:
    """Orchestrate the three read-only stages; exit non-zero on any failure."""
    precond = verify_preconditions()
    print("# Preconditions")
    for c in precond.checks:
        print(f"  [{'PASS' if c.passed else 'FAIL'}] {c.name} - {c.detail}")
    if not precond.ok:
        print("\nFAIL: preconditions not met; aborting.", file=sys.stderr)
        return 1

    inventory = build_inventory()
    print("\n# Inventory (distinct paths added across all refs)")
    for pc in inventory.counts:
        print(f"  [{'OK' if pc.ok else 'LOW'}] {pc.label}: {pc.count} (floor {pc.floor})")
    if not inventory.ok:
        print(
            "\nFAIL: an inventory cohort collapsed to/below floor; repo drifted.", file=sys.stderr
        )
        return 1

    print()
    print_runbook(inventory)
    return 0


if __name__ == "__main__":
    sys.exit(main())
