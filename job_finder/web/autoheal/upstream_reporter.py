"""Upstream contribution channel — consent-gated heal bundles (Phase D / D5).

Every adoption writes a scrubbed contribution bundle to
``<userdata>/heal_contrib/``. NOTHING leaves the machine from this module:
the dashboard renders pending bundles behind an explicit user action
(pre-filled GitHub issue link / copy button), and the optional maintainer
auto-PR path is a separate, default-off flag (see ``maintainer_pr``).

Bundle samples are already PII-scrubbed at corpus capture time
(``corpus_store.append_sample``); the bundle never re-reads raw inputs.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path

from job_finder.json_utils import utc_now_iso

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
# Failing-sample clip: enough structure for a maintainer to reproduce the
# break without shipping a whole rendered page in every bundle.
MAX_SAMPLE_CHARS = 20_000

# Maintainer auto-PR budgets: per-call subprocess timeout and a total
# wall-clock budget for the whole sequence (cold path — once per adoption).
GH_CALL_TIMEOUT_S = 30
PR_TOTAL_BUDGET_S = 60
# PR-body sample clip (the full bundle stays on disk).
_PR_BODY_SAMPLE_CHARS = 5_000


def _contrib_root(root: Path | str | None = None) -> Path:
    if root is not None:
        return Path(root)
    from job_finder.web.user_data_dirs import user_data_root

    return user_data_root() / "heal_contrib"


def build_bundle(conn: sqlite3.Connection, source: str, surface: str, recipe_dict: dict) -> dict:
    """Assemble a contribution bundle for an adopted recipe.

    ``failing_sample`` is the newest zero-yield ``corpus_sample.raw_text``
    (PII-scrubbed at capture), clipped to ``MAX_SAMPLE_CHARS``; ``drift`` is
    a ``source_health`` excerpt.
    """
    failing_sample = ""
    rows = conn.execute(
        "SELECT raw_text, output_json FROM corpus_sample WHERE source = ? ORDER BY id DESC",
        (source,),
    ).fetchall()
    for raw_text, output_json in rows:
        try:
            job_count = int(json.loads(output_json).get("job_count", 0))
        except (ValueError, TypeError, AttributeError):
            job_count = 0
        if job_count == 0:
            failing_sample = (raw_text or "")[:MAX_SAMPLE_CHARS]
            break

    drift: dict = {}
    health = conn.execute(
        "SELECT consecutive_breaks, baseline_yield, last_signal "
        "FROM source_health WHERE source = ?",
        (source,),
    ).fetchone()
    if health is not None:
        drift = {
            "consecutive_breaks": health[0],
            "baseline_yield": health[1],
            "last_signal": health[2],
        }

    from job_finder.web.update_check import current_version

    return {
        "schema_version": SCHEMA_VERSION,
        "source": source,
        "surface": surface,
        "recipe": recipe_dict,
        "failing_sample": failing_sample,
        "drift": drift,
        "created_at": utc_now_iso(),
        "app_version": current_version(),
    }


def write_bundle(bundle: dict, *, contrib_root: Path | str | None = None) -> Path:
    """Atomically write *bundle* to the contrib dir; return the final path.

    Filename: ``<sanitized source>-<UTC yyyymmddHHMMSS>.json`` (timestamp
    from the bundle's ``created_at`` — never ``datetime.now()``). A second
    adoption of the same source within the same second overwrites — newest
    bundle wins, which is the right dedup for an unreviewed queue.
    """
    out_dir = _contrib_root(contrib_root)
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_source = re.sub(r"[^A-Za-z0-9._-]+", "-", str(bundle.get("source") or "unknown"))
    stamp = re.sub(r"[^0-9]", "", str(bundle.get("created_at") or utc_now_iso()))[:14]
    out_path = out_dir / f"{safe_source}-{stamp}.json"

    fd, tmp_path = tempfile.mkstemp(dir=out_dir, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(bundle, fh, indent=2)
        os.replace(tmp_path, out_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return out_path


# ---------------------------------------------------------------------------
# Maintainer auto-PR (default-off; remote-only — never touches the local tree)
# ---------------------------------------------------------------------------

_COMMIT_MUTATION = (
    "mutation($input: CreateCommitOnBranchInput!) "
    "{ createCommitOnBranch(input: $input) { commit { oid } } }"
)


def _gh(args: list[str], timeout_s: float):
    """Single subprocess seam for all gh invocations (tests fake this)."""
    return subprocess.run(
        ["gh", *args], capture_output=True, text=True, timeout=timeout_s
    )


def maintainer_pr(bundle: dict, autoheal_cfg: dict) -> str | None:
    """Open/refresh the idempotent maintainer PR shipping *bundle*'s recipe.

    Gated on ``autoheal.maintainer_auto_pr`` (default false) + a configured
    ``upstream_repo`` + ``gh`` on PATH (silent debug skip otherwise).
    Remote-only and idempotent by construction: the branch name is the
    deterministic ``heal/<surface>-<file_key>`` — a second adoption commits
    onto the existing branch instead of opening a duplicate PR.

    Returns an audit outcome (``contrib_pr_opened`` / ``contrib_pr_updated``
    / ``contrib_pr_failed``) or None when the path is disabled/skipped.
    Never raises; on failure the bundle remains on disk.
    """
    if not autoheal_cfg.get("maintainer_auto_pr", False):
        return None
    repo = str(autoheal_cfg.get("upstream_repo") or "").strip()
    if not repo:
        logger.debug("upstream_reporter: maintainer_auto_pr set but no upstream_repo; skipping")
        return None
    if shutil.which("gh") is None:
        logger.debug("upstream_reporter: gh not on PATH; skipping maintainer PR")
        return None
    try:
        return _maintainer_pr_sequence(bundle, repo)
    except Exception:
        logger.exception("upstream_reporter: maintainer PR sequence failed")
        return "contrib_pr_failed"


def _maintainer_pr_sequence(bundle: dict, repo: str) -> str:
    deadline = time.monotonic() + PR_TOTAL_BUDGET_S

    def _remaining() -> float:
        return max(1.0, min(GH_CALL_TIMEOUT_S, deadline - time.monotonic()))

    surface = str(bundle.get("surface") or "email")
    source = str(bundle.get("source") or "unknown")
    file_key = source.split(":", 1)[1] if ":" in source else source
    branch = f"heal/{surface}-{file_key}"
    recipe_path = f"job_finder/data/default_overrides/{surface}/{file_key}.json"
    contents_b64 = base64.b64encode(
        (json.dumps(bundle.get("recipe") or {}, indent=2) + "\n").encode("utf-8")
    ).decode("ascii")

    # 1. Existing heal branch? Commit onto it; else branch off main.
    ref = _gh(["api", f"repos/{repo}/git/ref/heads/{branch}"], _remaining())
    if ref.returncode == 0:
        head_oid = json.loads(ref.stdout)["object"]["sha"]
    else:
        base = _gh(["api", f"repos/{repo}/git/ref/heads/main"], _remaining())
        if base.returncode != 0:
            logger.warning("upstream_reporter: base ref lookup failed: %s", base.stderr[-300:])
            return "contrib_pr_failed"
        head_oid = json.loads(base.stdout)["object"]["sha"]
        created = _gh(
            [
                "api",
                f"repos/{repo}/git/refs",
                "-f",
                f"ref=refs/heads/{branch}",
                "-f",
                f"sha={head_oid}",
            ],
            _remaining(),
        )
        if created.returncode != 0:
            logger.warning("upstream_reporter: ref create failed: %s", created.stderr[-300:])
            return "contrib_pr_failed"

    # 2. Commit the recipe file via createCommitOnBranch (remote-only).
    commit = _gh(
        [
            "api",
            "graphql",
            "-f",
            f"query={_COMMIT_MUTATION}",
            "-f",
            f"input[branch][repositoryNameWithOwner]={repo}",
            "-f",
            f"input[branch][branchName]={branch}",
            "-f",
            f"input[message][headline]=heal({surface}): shipped default for {source}",
            "-f",
            f"input[expectedHeadOid]={head_oid}",
            "-f",
            f"input[fileChanges][additions][][path]={recipe_path}",
            "-f",
            f"input[fileChanges][additions][][contents]={contents_b64}",
        ],
        _remaining(),
    )
    if commit.returncode != 0:
        logger.warning("upstream_reporter: commit failed: %s", commit.stderr[-300:])
        return "contrib_pr_failed"

    # 3. At most one open PR per branch: refresh wins over duplicate.
    pr_list = _gh(
        ["pr", "list", "--repo", repo, "--head", branch, "--state", "open", "--json", "number"],
        _remaining(),
    )
    if pr_list.returncode != 0:
        logger.warning("upstream_reporter: pr list failed: %s", pr_list.stderr[-300:])
        return "contrib_pr_failed"
    if json.loads(pr_list.stdout or "[]"):
        return "contrib_pr_updated"  # commit landed on the existing PR

    sample = str(bundle.get("failing_sample") or "")[:_PR_BODY_SAMPLE_CHARS]
    body = (
        f"Automated heal contribution for `{source}` (surface: {surface}).\n\n"
        f"Drift: `{json.dumps(bundle.get('drift') or {})}`\n"
        f"App version: {bundle.get('app_version')}\n\n"
        "PII-scrubbed failing sample (clipped):\n\n"
        f"````\n{sample}\n````\n"
    )
    pr_create = _gh(
        [
            "pr",
            "create",
            "--repo",
            repo,
            "--head",
            branch,
            "--title",
            f"heal({surface}): shipped default for {source}",
            "--body",
            body,
        ],
        _remaining(),
    )
    if pr_create.returncode != 0:
        logger.warning("upstream_reporter: pr create failed: %s", pr_create.stderr[-300:])
        return "contrib_pr_failed"
    return "contrib_pr_opened"


def pending_bundles(*, contrib_root: Path | str | None = None) -> list[dict]:
    """All bundles on disk, newest first, each with its ``filename``. Never raises."""
    try:
        root = _contrib_root(contrib_root)
        if not root.is_dir():
            return []
        bundles: list[dict] = []
        for path in root.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            data["filename"] = path.name
            bundles.append(data)
        bundles.sort(key=lambda b: str(b.get("created_at") or ""), reverse=True)
        return bundles
    except Exception:
        logger.exception("upstream_reporter: pending_bundles failed")
        return []
