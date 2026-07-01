"""Email sender registry and archival helpers — shared by Gmail API and IMAP sources.

This module contains the shared email-sender logic that both Gmail API and IMAP
ingestion paths use. It includes the sender registry (FROM address → parser mapping),
override resolution, and parse-failure archival helpers.
"""

import logging
import os
from collections.abc import Callable
from datetime import datetime
from typing import NamedTuple

from job_finder.parsers import has_job_urls
from job_finder.parsers.glassdoor_parser import parse_glassdoor_alert
from job_finder.parsers.greenhouse_parser import parse_greenhouse_alert
from job_finder.parsers.indeed_parser import parse_indeed_alert, parse_indeed_match_alert
from job_finder.parsers.jobright_parser import parse_jobright_alert
from job_finder.parsers.linkedin_parser import parse_linkedin_alert
from job_finder.parsers.monster_parser import parse_monster_alert
from job_finder.parsers.trueup_parser import parse_trueup_alert
from job_finder.parsers.ziprecruiter_parser import parse_ziprecruiter_alert
from job_finder.web.user_data_dirs import parse_failures_dir

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Email-sender registry — THE single source of truth for the alert senders we
# parse. One row per FROM address: its parser, its canonical health label, and
# (optionally) the Settings form key the address may be overridden under.
#
# The three lookup maps below (address→parser, address→label, override-key→
# address) are DERIVED from this table, so they can never drift out of sync the
# way three hand-maintained dicts could. Adding a new alert source is a single
# SenderSpec row — no separate edits to forget. test_autoheal_email_capture and
# test_gmail_sender_overrides pin the derived shapes; form keys mirror
# settings.py `_parse_form_to_config` and config.example.yaml's `senders:` block.
# ---------------------------------------------------------------------------


class SenderSpec(NamedTuple):
    """One alert sender: FROM address → parser + canonical label + override key.

    Attributes:
        address: The exact FROM address matched on (historical SENDER_PARSERS /
            SENDER_LABEL key).
        parser: The parser callable for this sender's email body.
        label: Canonical one-per-parser health label (both LinkedIn addresses
            collapse to "linkedin").
        override_key: Settings form key (sources.imap.senders.<key>) the user
            may override this FROM address under, or None if not overridable.
    """

    address: str
    parser: Callable
    label: str
    override_key: str | None = None


SENDERS: tuple[SenderSpec, ...] = (
    SenderSpec(
        "jobalerts-noreply@linkedin.com", parse_linkedin_alert, "linkedin", "linkedin_alerts"
    ),
    SenderSpec("jobs-noreply@linkedin.com", parse_linkedin_alert, "linkedin", "linkedin_jobs"),
    SenderSpec("noreply@glassdoor.com", parse_glassdoor_alert, "glassdoor", "glassdoor"),
    SenderSpec("alert@indeed.com", parse_indeed_alert, "indeed", "indeed"),
    SenderSpec("donotreply@match.indeed.com", parse_indeed_match_alert, "indeed"),
    SenderSpec(
        "no-reply@ziprecruiter.com", parse_ziprecruiter_alert, "ziprecruiter", "ziprecruiter"
    ),
    SenderSpec("no-reply@us.greenhouse-jobs.com", parse_greenhouse_alert, "greenhouse"),
    SenderSpec("hello@trueup.io", parse_trueup_alert, "trueup"),
    SenderSpec("monster@notifications.monster.com", parse_monster_alert, "monster"),
    SenderSpec("noreply@jobright.ai", parse_jobright_alert, "jobright", "jobright"),
)

# Derived lookup maps — insertion order preserved from SENDERS.
SENDER_PARSERS: dict[str, Callable] = {s.address: s.parser for s in SENDERS}
SENDER_LABEL: dict[str, str] = {s.address: s.label for s in SENDERS}
# Settings-overridable senders: form key → DEFAULT address it swaps out.
_OVERRIDABLE_SENDERS: dict[str, str] = {
    s.override_key: s.address for s in SENDERS if s.override_key is not None
}


def resolve_sender_parsers(config: dict | None = None) -> dict:
    """Return SENDER_PARSERS with any user-overridden FROM addresses swapped in.

    Wires ``sources.imap.senders.<key>`` (saved by the Settings page) into the
    address→parser map. For each overridable sender key, if the config supplies a
    non-empty address that differs from the default, the default key is *renamed*
    to the override (its parser function is preserved). Non-overridable senders
    (greenhouse, indeed-match, trueup, monster) are untouched.

    This function calls ``normalize_email_senders`` internally to heal legacy
    ``sources.gmail.senders`` configs by relocating them to ``sources.imap.senders``.

    Safety invariant: ``resolve_sender_parsers(None)``, ``resolve_sender_parsers({})``,
    and any config with no senders overrides all return a dict equal to
    ``SENDER_PARSERS`` — the no-override path is identical to today's behaviour.

    Args:
        config: Full config dict (or None). Reads ``sources.imap.senders`` after
            normalization.

    Returns:
        A new dict mapping sender address → parser function.
    """
    from job_finder.config import normalize_email_senders

    # Heal legacy configs on-the-fly
    if config is not None:
        config = normalize_email_senders(config)

    parsers = dict(SENDER_PARSERS)
    senders = (config or {}).get("sources", {}).get("imap", {}).get("senders", {}) or {}
    for sender_key, default in _OVERRIDABLE_SENDERS.items():
        override = senders.get(sender_key)
        if (
            isinstance(override, str)
            and override.strip()
            and override != default
            and default in parsers
        ):
            parsers[override] = parsers.pop(default)
    return parsers


def resolve_sender_label(config: dict | None = None) -> dict:
    """Return SENDER_LABEL with overridden FROM addresses mapped to the canonical label.

    For each overridden sender, the new address is ADDED to the label map pointing
    at the same canonical label as the default (the default entry is kept too, so
    autoheal recipes that key on the canonical label keep resolving). This mirrors
    ``resolve_sender_parsers`` so the resolved address has both a parser and a label.

    This function calls ``normalize_email_senders`` internally to heal legacy
    ``sources.gmail.senders`` configs by relocating them to ``sources.imap.senders``.

    Safety invariant: the no-override path (None / {} / no senders) returns a dict
    equal to ``SENDER_LABEL``.

    Args:
        config: Full config dict (or None). Reads ``sources.imap.senders`` after
            normalization.

    Returns:
        A new dict mapping sender address → canonical label.
    """
    from job_finder.config import normalize_email_senders

    # Heal legacy configs on-the-fly
    if config is not None:
        config = normalize_email_senders(config)

    labels = dict(SENDER_LABEL)
    senders = (config or {}).get("sources", {}).get("imap", {}).get("senders", {}) or {}
    for sender_key, default in _OVERRIDABLE_SENDERS.items():
        override = senders.get(sender_key)
        if (
            isinstance(override, str)
            and override.strip()
            and override != default
            and default in labels
        ):
            labels[override] = labels[default]
    return labels


# Meta-email indicator phrases (checked against lowercased first 200 chars of body)
_ARCHIVE_META_INDICATORS = [
    "job alert digest",
    "weekly digest",
    "unsubscribe from",
    "confirm your email",
    "email preferences",
]


def _should_archive_failure(body: str, jobs: list, sender: str) -> bool:
    """Return True if this parser result is a genuine extraction failure.

    A zero-job result is only a *failure* when the email actually carried job
    listings we expected to extract. Many job-board emails are non-job
    notifications the parsers deliberately skip — Glassdoor company-follow /
    brand-update digests, LinkedIn "your job alert has been created"
    confirmations, marketing/onboarding blasts — and they legitimately contain
    zero job listings. Archiving those is a false positive (it pollutes the
    parse-failures dir and emits misleading "Parse failure archived" logs).

    The distinguishing signal is structural: does the body contain at least one
    recognised job-listing URL? This is the same ``has_job_urls`` predicate
    ``extract_with_fallback`` already uses to decide whether there is anything
    to extract — so archival now agrees with the rest of the pipeline:
    no job URLs => nothing was expected => not a failure.

    Archival is therefore triggered when ALL hold:
    - Parser found zero jobs (``jobs`` is empty).
    - Body is long enough to be a real email (>= 500 chars after stripping).
    - Body contains a recognised job-listing URL (jobs were expected but the
      parser returned none — probable template/format drift).
    - Body is not a known digest/confirmation meta-email.

    Args:
        body: Raw email body string.
        jobs: List of Job objects returned by the parser (empty = parse failure).
        sender: Sender email address.

    Returns:
        True if the failure should be archived.
    """
    if jobs:
        return False
    if not body or len(body.strip()) < 500:
        return False
    if not has_job_urls(body):
        return False
    preamble = body[:200].lower()
    return not any(indicator in preamble for indicator in _ARCHIVE_META_INDICATORS)


def _archive_parse_failure(sender: str, body: str, *, failures_dir: str | None = None) -> None:
    """Archive HTML body from a failed parse to the parse-failures directory.

    Filename: {sender_domain}_{ISO_timestamp}.html
    Creates directory if needed. Logs warning on write failure — never raises.

    Args:
        sender: Sender email address (used for filename prefix).
        body: Raw email body HTML to archive.
        failures_dir: Directory to write failure files into. Defaults to
            ``str(parse_failures_dir())`` (resolved at call time so that
            JOB_CANNON_USER_DATA_DIR overrides are honoured in tests).
    """
    resolved_dir = failures_dir if failures_dir is not None else str(parse_failures_dir())
    try:
        os.makedirs(resolved_dir, exist_ok=True)
        domain = sender.split("@")[-1].replace(".", "_") if "@" in sender else sender
        ts = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
        path = f"{resolved_dir}/{domain}_{ts}.html"
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
        logger.info("Parse failure archived: %s", path)
    except Exception as e:
        logger.warning("Failed to archive parse failure: %s", e)
