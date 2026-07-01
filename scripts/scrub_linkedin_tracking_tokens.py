"""Scrub LinkedIn tracking token values from .eml test fixtures.

Replaces the *values* of midToken= and trkEmail= URL parameters with
same-length sequences of 'X' characters so URL structure, MIME boundaries,
and quoted-printable line wrapping remain byte-compatible.

Usage:
    python scripts/scrub_linkedin_tracking_tokens.py [--dry-run]

    --dry-run  Print what would change without writing files.
    --verify   After scrubbing, run a parse-equivalence check using the
               IMAP round-trip path and report pass/fail.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

FIXTURES_DIR = Path(__file__).parent.parent / "tests" / "fixtures" / "emails"

FIXTURE_FILES = [
    "linkedin_alert.eml",
    "linkedin_alert_2.eml",
    "linkedin_alert_3.eml",
    "linkedin_alert_4.eml",
    "linkedin_jobs.eml",
    "linkedin_jobs_2.eml",
    "linkedin_jobs_3.eml",
    "linkedin_jobs_4.eml",
]

# Parameters whose *values* we want to replace.
# midToken values are already REDACTED; trkEmail values contain member-linked
# session IDs (fmid_…, ssid_…) and need scrubbing.
# We keep both in the pattern so the script is idempotent and handles any
# future re-introduction of live midToken values.
TOKEN_PARAMS = (b"midToken", b"trkEmail")

# Characters that terminate a URL parameter value in QP-encoded email lines:
# - & starts next parameter
# - whitespace ends the URL
# - \r or \n ends the line
# NOTE: we do NOT treat = as a terminator here because QP-encoded = signs
# inside values appear as =3D (three bytes), not a bare =.  Bare = in QP
# means "soft line break" and would be at end of a physical line, but our
# measurement shows these files use very long single-line URLs without QP
# soft breaks, so this is safe.
_VALUE_RE = re.compile(rb"(" + b"|".join(TOKEN_PARAMS) + rb")=([^&\s\r\n]+)")


def _scrub_value(match: re.Match[bytes]) -> bytes:
    """Replace the token value with same-length 'X' padding.

    Preserves the parameter name and '=' sign; replaces only the value.
    Uses 'X' (0x58) — a printable ASCII character that never appears in
    QP special sequences (which use '=') and is safe in URLs.
    """
    param: bytes = match.group(1)
    value: bytes = match.group(2)
    return param + b"=" + b"X" * len(value)


def scrub_file(path: Path, *, dry_run: bool = False) -> tuple[int, int]:
    """Scrub one fixture file.

    Returns (original_count, scrubbed_count) where both are the number of
    token parameter occurrences found (should be equal).
    """
    raw = path.read_bytes()

    occurrences_before = len(_VALUE_RE.findall(raw))
    scrubbed, n_subs = _VALUE_RE.subn(_scrub_value, raw)
    occurrences_after = len(_VALUE_RE.findall(scrubbed))

    if n_subs == 0:
        print(f"  {path.name}: no tokens found — already clean or unexpected format")
        return 0, 0

    # Verify byte-length is unchanged (same-length replacement guarantee)
    if len(scrubbed) != len(raw):
        raise RuntimeError(
            f"BUG: {path.name} length changed after scrub "
            f"({len(raw)} -> {len(scrubbed)}) — scrub is NOT byte-compatible"
        )

    if not dry_run:
        path.write_bytes(scrubbed)
        print(f"  {path.name}: {n_subs} replacements, length unchanged ({len(raw)} bytes)")
    else:
        print(f"  {path.name}: would replace {n_subs} token values (dry-run, length check passed)")

    return occurrences_before, occurrences_after


def verify_parse_equivalence(baseline: dict[str, list[dict]]) -> bool:
    """Re-parse all fixtures and compare job lists against the baseline.

    Returns True if extraction is identical, False otherwise.
    """
    import email as _email
    import email.policy

    sys.path.insert(0, str(Path(__file__).parent.parent))
    from job_finder.sources.email_senders import SENDER_PARSERS
    from job_finder.sources.imap_source import ImapSource

    _fixtures_by_sender: dict[str, list[str]] = {
        "jobalerts-noreply@linkedin.com": [
            "linkedin_alert.eml",
            "linkedin_alert_2.eml",
            "linkedin_alert_3.eml",
            "linkedin_alert_4.eml",
        ],
        "jobs-noreply@linkedin.com": [
            "linkedin_jobs.eml",
            "linkedin_jobs_2.eml",
            "linkedin_jobs_3.eml",
            "linkedin_jobs_4.eml",
        ],
    }

    imap = ImapSource()
    ok = True

    for sender, parser_func in SENDER_PARSERS.items():
        if "linkedin" not in sender:
            continue
        for fname in _fixtures_by_sender.get(sender, []):
            fpath = FIXTURES_DIR / fname
            with open(fpath, "rb") as f:
                eml_bytes = f.read()
            message = _email.message_from_bytes(eml_bytes, policy=_email.policy.default)
            body = imap._extract_body(message)
            date = imap._extract_date(message)
            jobs = parser_func(body, date or "")

            before = baseline.get(fname, [])
            after = [
                {
                    "title": j.title,
                    "company": j.company,
                    "source_url": str(j.source_url) if j.source_url else None,
                }
                for j in jobs
            ]

            if before != after:
                print(f"  MISMATCH {fname}:")
                print(f"    before: {before}")
                print(f"    after:  {after}")
                ok = False
            else:
                print(f"  OK {fname}: {len(jobs)} jobs identical")

    return ok


def _capture_baseline() -> dict[str, list[dict]]:
    """Capture current parse results as baseline before scrubbing."""
    import email as _email
    import email.policy

    sys.path.insert(0, str(Path(__file__).parent.parent))
    from job_finder.sources.email_senders import SENDER_PARSERS
    from job_finder.sources.imap_source import ImapSource

    _fixtures_by_sender: dict[str, list[str]] = {
        "jobalerts-noreply@linkedin.com": [
            "linkedin_alert.eml",
            "linkedin_alert_2.eml",
            "linkedin_alert_3.eml",
            "linkedin_alert_4.eml",
        ],
        "jobs-noreply@linkedin.com": [
            "linkedin_jobs.eml",
            "linkedin_jobs_2.eml",
            "linkedin_jobs_3.eml",
            "linkedin_jobs_4.eml",
        ],
    }

    imap = ImapSource()
    baseline: dict[str, list[dict]] = {}

    for sender, parser_func in SENDER_PARSERS.items():
        if "linkedin" not in sender:
            continue
        for fname in _fixtures_by_sender.get(sender, []):
            fpath = FIXTURES_DIR / fname
            with open(fpath, "rb") as f:
                eml_bytes = f.read()
            message = _email.message_from_bytes(eml_bytes, policy=_email.policy.default)
            body = imap._extract_body(message)
            date = imap._extract_date(message)
            jobs = parser_func(body, date or "")
            baseline[fname] = [
                {
                    "title": j.title,
                    "company": j.company,
                    "source_url": str(j.source_url) if j.source_url else None,
                }
                for j in jobs
            ]

    return baseline


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be replaced without modifying files.",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="After scrubbing, verify parse equivalence against pre-scrub baseline.",
    )
    args = parser.parse_args()

    if not FIXTURES_DIR.exists():
        print(f"ERROR: fixtures dir not found: {FIXTURES_DIR}", file=sys.stderr)
        return 1

    # Capture baseline before any changes (only needed for --verify)
    baseline: dict[str, list[dict]] = {}
    if args.verify and not args.dry_run:
        print("Capturing pre-scrub baseline...")
        baseline = _capture_baseline()

    print(f"\nScrubbing {len(FIXTURE_FILES)} LinkedIn fixture files...")
    total_before = 0
    total_after = 0

    for fname in FIXTURE_FILES:
        fpath = FIXTURES_DIR / fname
        if not fpath.exists():
            print(f"  {fname}: NOT FOUND — skipping")
            continue
        b, a = scrub_file(fpath, dry_run=args.dry_run)
        total_before += b
        total_after += a

    print(f"\nTotal token occurrences: {total_before} found, {total_after} remaining after scrub")

    if args.verify and not args.dry_run:
        print("\nVerifying parse equivalence...")
        ok = verify_parse_equivalence(baseline)
        if ok:
            print("\nPARSE EQUIVALENCE: PASS — all jobs identical before/after scrub")
        else:
            print("\nPARSE EQUIVALENCE: FAIL — extraction changed after scrub!", file=sys.stderr)
            return 2

    if not args.dry_run:
        print("\nDone. Run tests to confirm:")
        print(
            "  uv run --active pytest tests/test_linkedin_parser.py "
            "tests/test_imap_parser_roundtrip.py -n0 -q --tb=short"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
