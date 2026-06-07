"""Reusable PII scrubbing for captured parser inputs.

Phase A of parser auto-heal stores real email/HTML/JSON the parsers saw. This
strips obvious personal data BEFORE anything is written to disk. The deny-list
seeds from the same rules the fixture-PII test enforces, plus any caller-supplied
identifiers (the local user's name/email from config), so a public multi-user
release scrubs each user's own identity rather than a hardcoded one.
"""

from __future__ import annotations

import re

# Seed identifiers (kept in sync with tests/test_imap_parser_roundtrip.py).
DEFAULT_DENYLIST: tuple[str, ...] = ("senki", "senkichi", "@users.noreply.github.com")

_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_TO_HEADER_RE = re.compile(r"^\s*(to|cc|bcc|delivered-to|x-original-to)\s*:.*$", re.IGNORECASE)
_REDACTED = "[redacted]"


def scrub_text(text: str, identifiers: tuple[str, ...] | list[str] | None = None) -> str:
    """Return *text* with recipient headers dropped and PII redacted.

    Idempotent and never raises on str input. ``identifiers`` extends (does not
    replace) DEFAULT_DENYLIST — pass the local user's name/email from config.
    """
    if not text:
        return text or ""
    deny = tuple(DEFAULT_DENYLIST) + tuple(identifiers or ())

    kept = [ln for ln in text.splitlines() if not _TO_HEADER_RE.match(ln)]
    out = "\n".join(kept)

    out = _EMAIL_RE.sub(_REDACTED, out)
    for ident in deny:
        if not ident:
            continue
        out = re.sub(re.escape(ident), _REDACTED, out, flags=re.IGNORECASE)
    return out
