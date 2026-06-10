"""Autoheal VALIDATE gate — subprocess corpus replay + regression proof.

No arbitrary code executes here: the worker subprocess only runs OUR
interpreters (``RecipeExtractor`` / ``extract_field``) over a candidate
recipe and stored corpus samples. The subprocess exists purely for the
wall-clock timeout — a pathological generated regex (ReDoS) must not hang
the ingestion thread. It is NOT a security sandbox.

Gate — a candidate must pass ALL of:
(a) every prior-working corpus sample → ≥1 valid Job (title + url present);
(b) every failing sample → ≥1 Job (the break is actually fixed);
(c) optional ``pytest`` run over test files matching the source token,
    skipped cleanly when none exist.

Worker protocol: parent writes ``{surface, candidate, corpus_samples,
failing_samples}`` to a temp JSON, spawns ``python -m
job_finder.web.autoheal.validator <in> <out>``, kills it on timeout, and
reads back ``{ok, reason}``.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from job_finder.web.autoheal.recipe_schema import (
    AtsAliasRecipe,
    HtmlRecipe,
    recipe_to_dict,
    validate_recipe,
)

logger = logging.getLogger(__name__)

# Exit code 5 = pytest collected no tests; treated as a clean skip.
_PYTEST_NO_TESTS_COLLECTED = 5


@dataclass(frozen=True)
class Verdict:
    """Validation verdict. ``ok=True`` means the candidate may be adopted."""

    ok: bool
    reason: str | None = None


def validate(
    candidate: HtmlRecipe | AtsAliasRecipe,
    surface: str,
    corpus_samples: list[str],
    failing_samples: list[str],
    *,
    timeout_s: float,
) -> Verdict:
    """Replay the candidate over stored samples in a timeout-guarded subprocess."""
    payload = {
        "surface": surface,
        "candidate": recipe_to_dict(candidate),
        "corpus_samples": list(corpus_samples),
        "failing_samples": list(failing_samples),
    }

    with tempfile.TemporaryDirectory(prefix="autoheal_validate_") as td:
        in_path = Path(td) / "input.json"
        out_path = Path(td) / "verdict.json"
        in_path.write_text(json.dumps(payload), encoding="utf-8")

        try:
            proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "job_finder.web.autoheal.validator",
                    str(in_path),
                    str(out_path),
                ],
                timeout=timeout_s,
                capture_output=True,
            )
        except subprocess.TimeoutExpired:
            logger.warning("autoheal validator: worker timed out after %ss", timeout_s)
            return Verdict(False, "timeout")

        if proc.returncode != 0 or not out_path.is_file():
            logger.warning(
                "autoheal validator: worker failed (rc=%s): %s",
                proc.returncode,
                proc.stderr.decode("utf-8", errors="replace")[-500:],
            )
            return Verdict(False, "worker_error")

        try:
            raw = json.loads(out_path.read_text(encoding="utf-8"))
        except ValueError:
            return Verdict(False, "worker_error")

    if not raw.get("ok"):
        return Verdict(False, raw.get("reason") or "rejected")

    # Gate (c): optional pytest over matching test files (skip cleanly if absent)
    pytest_reason = _pytest_gate(candidate.source, timeout_s=timeout_s)
    if pytest_reason is not None:
        return Verdict(False, pytest_reason)

    return Verdict(True)


# ---------------------------------------------------------------------------
# Gate (c) — optional matching-test pytest run
# ---------------------------------------------------------------------------


def _pytest_gate(source: str, *, timeout_s: float) -> str | None:
    """Run pytest over test files matching the source token; None = pass/skip.

    Skips cleanly (returns None) when the tests/ directory or any matching
    file is absent — e.g. an installed (non-repo) deployment. test_autoheal_*
    files are excluded so a heal of a source whose name appears in autoheal
    test filenames cannot recurse into this suite.
    """
    token = re.sub(r"[^a-z0-9_]", "", source.split(":", 1)[-1].lower())
    if not token:
        return None
    repo_root = Path(__file__).resolve().parents[3]
    tests_dir = repo_root / "tests"
    if not tests_dir.is_dir():
        return None
    files = sorted(
        p
        for p in tests_dir.glob(f"test_*{token}*.py")
        if not p.name.startswith("test_autoheal")
    )
    if not files:
        return None

    try:
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                *map(str, files),
                "-q",
                "-n0",
                "--tb=no",
                "-p",
                "no:cacheprovider",
            ],
            cwd=repo_root,
            timeout=max(timeout_s, 120),
            capture_output=True,
        )
    except subprocess.TimeoutExpired:
        return "pytest_timeout"
    if proc.returncode in (0, _PYTEST_NO_TESTS_COLLECTED):
        return None
    return "pytest_failed"


# ---------------------------------------------------------------------------
# Worker (runs in the subprocess)
# ---------------------------------------------------------------------------


def _replay(surface: str, candidate_dict: dict, corpus: list[str], failing: list[str]) -> dict:
    """Apply the candidate to every sample; return {ok, reason}."""
    candidate = validate_recipe(surface, candidate_dict)

    if surface == "email":
        from job_finder.web.autoheal.recipe_extractor import RecipeExtractor

        extractor = RecipeExtractor(candidate, job_source="email_recipe")

        def yields(sample: str) -> bool:
            return any(j.title and j.source_url for j in extractor(sample))

    else:
        from job_finder.web._field_alias import (
            JOB_TITLE_FIELDS,
            JOB_URL_FIELDS,
            extract_field,
            find_job_array,
        )

        title_keys = JOB_TITLE_FIELDS + [
            k for k in candidate.title_fields if k not in JOB_TITLE_FIELDS
        ]
        url_keys = JOB_URL_FIELDS + [k for k in candidate.url_fields if k not in JOB_URL_FIELDS]

        def _locate_array(data) -> list | None:
            found = find_job_array(data)
            if found is not None:
                return found
            if isinstance(data, dict):
                for key in candidate.array_keys:
                    if key in data and isinstance(data[key], list):
                        return data[key]
            return None

        def yields(sample: str) -> bool:
            try:
                data = json.loads(sample)
            except ValueError:
                return False
            postings = _locate_array(data)
            if not postings:
                return False
            for posting in postings:
                if not isinstance(posting, dict):
                    continue
                if extract_field(posting, title_keys) and extract_field(posting, url_keys):
                    return True
            return False

    # Gate (a): every prior-working sample must still extract — else regression
    for sample in corpus:
        if not yields(sample):
            return {"ok": False, "reason": "regression"}
    # Gate (b): every failing sample must now extract — else the break is unfixed
    for sample in failing:
        if not yields(sample):
            return {"ok": False, "reason": "target_unfixed"}
    return {"ok": True, "reason": None}


def _worker_main(in_path: str, out_path: str) -> None:
    payload = json.loads(Path(in_path).read_text(encoding="utf-8"))
    verdict = _replay(
        payload["surface"],
        payload["candidate"],
        payload["corpus_samples"],
        payload["failing_samples"],
    )
    Path(out_path).write_text(json.dumps(verdict), encoding="utf-8")


if __name__ == "__main__":
    _worker_main(sys.argv[1], sys.argv[2])
