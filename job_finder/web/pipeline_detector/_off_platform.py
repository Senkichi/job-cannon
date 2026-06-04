"""Off-platform application capture for the pipeline detector.

When a confirmation or interview email arrives for a company we have no
job for (user applied directly via the company site, a referral, or
cold email — bypassing JF entirely), the company-mandatory gate in
``_processing._process_email`` drops the email and the application is
lost. After 3 days the Gmail lookback window ages out and we never see
it again.

This module recovers those cases with a rules-only sender-domain
heuristic:

  1. Extract the host from the sender address.
  2. Reject if the host is an ATS (greenhouse-mail.io, ashbyhq.com,
     etc.), a personal mail service (gmail.com, …), or a scheduling
     tool (calendly.com, otter.ai, …) — none of those identify a
     specific employer.
  3. Reduce the remaining host to its registrable domain (subdomains
     stripped, ``co.uk``-style two-label TLDs special-cased).
  4. Title-case the SLD as the candidate company name.

The dedup pass then normalises both the extracted candidate and every
existing job's company string (lowercase + alphanumeric only) before
comparison so that ``hingehealth.com`` correctly attributes to an
already-ingested ``"Hinge Health"`` job rather than creating a
duplicate stub.

Stubs are inserted with ``pipeline_status='discovered'`` so that
``update_pipeline_status`` (called by the orchestrator) produces a
proper ``pipeline_events`` row capturing the transition to ``applied``
or ``phone_screen``. ``jd_full=NULL`` makes them visible to the
nightly enrichment_backfill, which fills the description; scoring then
follows naturally once jd_full is present.

What this module deliberately does NOT do (deferred to Option 2):
  - LLM extraction when the sender is an ATS/generic forwarder. The
    audit showed third-party recruiter pings ("Vishal Mehta (Atrium
    Works)") and Greenhouse-routed thank-yous would slip through the
    rules.
  - Title extraction from the subject. Stubs land with a placeholder
    title and rely on enrichment to discover the real role (if the
    careers page is reachable at all).
"""

import logging
import re
import sqlite3
import time

from job_finder.web.pipeline_detector._constants import ATS_DOMAINS

logger = logging.getLogger(__name__)


# Mail services where the sender domain says nothing about the employer.
PERSONAL_EMAIL_DOMAINS = frozenset(
    {
        "gmail.com",
        "outlook.com",
        "hotmail.com",
        "live.com",
        "msn.com",
        "yahoo.com",
        "ymail.com",
        "proton.me",
        "protonmail.com",
        "icloud.com",
        "me.com",
        "aol.com",
        "mail.com",
        "gmx.com",
        "fastmail.com",
        "duck.com",
    }
)

# Scheduling / meeting tools — sender domain identifies the tool, not the
# employer. (ATS-classified ones like modernloop.io are already blocked
# via ATS_DOMAINS; this list is the non-ATS overlap.)
SCHEDULING_DOMAINS = frozenset(
    {
        "calendly.com",
        "savvycal.com",
        "doodle.com",
        "otter.ai",
        "zoom.us",
        "google.com",  # Meet invites
        "microsoft.com",  # Teams invites
        "webex.com",
    }
)

# Two-label public suffixes we routinely see. Not a full Public Suffix
# List — just enough that a sender like ``careers@acme.co.uk`` resolves
# to ``acme`` rather than ``co``. Extend as new TLDs show up.
_TWO_LABEL_PUBLIC_SUFFIXES = frozenset(
    {
        "co.uk",
        "co.in",
        "co.jp",
        "co.kr",
        "co.nz",
        "co.za",
        "com.au",
        "com.br",
        "com.mx",
        "com.sg",
    }
)


def _normalize_for_dedup(name: str) -> str:
    """Strip whitespace + punctuation and lowercase for dedup comparison.

    ``"Hinge Health"`` and ``"hingehealth"`` both reduce to
    ``"hingehealth"``. ``"AT&T"`` and ``"at-t"`` both reduce to
    ``"att"``. Used only on company names for attributing off-platform
    emails to existing jobs; not safe for general string normalisation.
    """
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def _registrable_domain(host: str) -> str | None:
    """Reduce ``careers.acme.co.uk`` → ``acme.co.uk``.

    Returns the SLD label (e.g. ``"acme"``) only via
    ``_extract_company_from_sender``; this helper returns the full
    registrable domain so the caller can title-case the SLD label and
    keep the suffix for tests/logging.
    """
    if not host:
        return None
    parts = host.split(".")
    if len(parts) < 2:
        return None
    # Check two-label public suffix first
    if len(parts) >= 3:
        candidate_suffix = ".".join(parts[-2:])
        if candidate_suffix in _TWO_LABEL_PUBLIC_SUFFIXES:
            return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _extract_sender_domain(from_address: str) -> str | None:
    """Pull the host portion out of a ``Name <addr@host>`` or ``addr@host``."""
    if not from_address:
        return None
    match = re.search(r"@([\w.-]+)", from_address)
    if not match:
        return None
    return match.group(1).lower()


def _extract_company_from_sender(from_address: str) -> str | None:
    """Heuristically extract a candidate company name from a sender.

    Returns ``None`` for:
      - empty / unparseable senders
      - ATS / scheduling / personal-mail domains (no employer signal)
      - hosts with fewer than two labels (e.g. ``localhost``)

    Otherwise returns the title-cased SLD (e.g. ``"Waymo"``,
    ``"Hingehealth"``). Multi-word company names that present as a
    single token in the domain (``hingehealth.com``) come back as
    ``"Hingehealth"``; the dedup pass normalises through whitespace so
    they still attribute to a pre-existing ``"Hinge Health"`` job.
    """
    domain = _extract_sender_domain(from_address)
    if not domain:
        return None

    # ATS domains (handles both exact match and subdomain match).
    if any(domain == d or domain.endswith("." + d) for d in ATS_DOMAINS):
        return None

    if domain in PERSONAL_EMAIL_DOMAINS:
        return None

    if domain in SCHEDULING_DOMAINS or any(domain.endswith("." + d) for d in SCHEDULING_DOMAINS):
        return None

    registrable = _registrable_domain(domain)
    if not registrable:
        return None

    sld = registrable.split(".")[0]
    if not sld:
        return None

    return sld.title()


def _find_existing_job_by_normalized_company(
    conn: sqlite3.Connection, candidate: str
) -> sqlite3.Row | None:
    """Return the most recent job whose normalised company matches candidate.

    Scans every row in jobs. Acceptable here because this only runs
    when the company-mandatory gate has already failed (rare) and the
    jobs table is bounded by the user's personal pipeline (~12k rows
    on this machine). If volume grows we can pre-compute a normalised
    column or add an index.
    """
    target = _normalize_for_dedup(candidate)
    if not target:
        return None
    rows = conn.execute(
        "SELECT dedup_key, company, pipeline_status, first_seen FROM jobs"
    ).fetchall()
    best: sqlite3.Row | None = None
    for row in rows:
        if _normalize_for_dedup(row["company"]) == target:
            if best is None or (row["first_seen"] or "") > (best["first_seen"] or ""):
                best = row
    return best


def _try_create_stub_job(email: dict, conn: sqlite3.Connection) -> dict | None:
    """Create (or attribute to) a stub job for an off-platform application.

    Returns a dict ``{"dedup_key", "company", "attributed_existing"}``
    on success, or ``None`` when no company could be extracted (caller
    should fall through to ``return "skipped"``).

    Dedup contract: if an existing job's normalised company matches
    the extracted candidate, we attribute to that job rather than
    creating a duplicate stub — even when the existing job is
    archived/rejected/dismissed. (The orchestrator's
    ``update_pipeline_status`` call will then resurrect it to
    applied/phone_screen, which is the right behaviour when the user
    re-applies to a previously-dismissed company.)
    """
    from_address = email.get("from_address", "")
    candidate = _extract_company_from_sender(from_address)
    if not candidate:
        return None

    existing = _find_existing_job_by_normalized_company(conn, candidate)
    if existing is not None:
        return {
            "dedup_key": existing["dedup_key"],
            "company": existing["company"],
            "attributed_existing": True,
        }

    dedup_key = f"{candidate.lower()}|off-platform|{int(time.time() * 1000)}"
    # Route the stub through upsert_job so it passes the same typed contract as
    # every other write (D-15) instead of a raw INSERT bypass. The synthetic
    # dedup_key encodes the email path's own uniqueness rule, so we build a
    # ParsedJob directly rather than via from_job (which would re-derive the
    # key from company|title). pipeline_status defaults to 'discovered' on
    # INSERT — matching the prior raw-INSERT value.
    from job_finder.db import upsert_job
    from job_finder.parsed_job import ParsedJob

    parsed = ParsedJob(
        title="(off-platform — title TBD)",
        company=candidate,
        dedup_key=dedup_key,
        location="",
        sources=["off_platform_email"],
        source_urls=[],
    )
    upsert_job(conn, parsed)
    logger.info(
        "off-platform stub created for %s (dedup_key=%s)",
        candidate,
        dedup_key,
    )
    return {
        "dedup_key": dedup_key,
        "company": candidate,
        "attributed_existing": False,
    }
