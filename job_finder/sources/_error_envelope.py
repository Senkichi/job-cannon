"""Generic detector for SERP vendors that return HTTP 200 wrapping an error envelope.

Several SERP-backed sources return a ``200`` transport status with a JSON body
that actually denotes a credential / quota / auth failure — e.g. Thordata's
``{"message": "Package has expired!", "status": "error"}`` (the retired
scaleserp / jsearch backends behaved similarly). Left undetected, the source
yields ``0`` jobs *silently* instead of a degraded/credential-invalid signal.

This module recognizes that shape **generically** so every current and future
SERP source benefits without a per-vendor copy of the check. It is a pure,
side-effect-free function: it takes a parsed body and returns a normalized
reason string (or ``None``); callers decide whether to raise.

Conservative by design: it only fires when an error marker is actually present,
so a legitimately-empty result set (e.g. ``{"jobs_results": []}``) is never
misread as a failure.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Lowercased substrings in an error message that mark a credential/quota/auth
# failure wrapped in a 200 body.
_ERROR_KEYWORDS: frozenset[str] = frozenset(
    {
        "expired",
        "invalid",
        "unauthorized",
        "forbidden",
        "package",
        "quota",
        "exhausted",
        "credit",
    }
)

# Fields that may carry a human-readable error string in a vendor envelope.
# ``error`` may itself be a nested object (Google-style ``{"error": {"message"}}``).
_MESSAGE_FIELDS: tuple[str, ...] = ("message", "error", "status_message", "error_message")

# String values of a status field that denote failure outright.
_ERROR_STATUS_STRINGS: frozenset[str] = frozenset({"error", "fail", "failed", "failure"})


def _message_text(data: dict) -> str | None:
    """First truthy message string among the known fields (handles nested ``{'error': {'message'}}``)."""
    for field in _MESSAGE_FIELDS:
        val = data.get(field)
        if isinstance(val, str) and val.strip():
            return val.strip()
        if isinstance(val, dict):
            inner = val.get("message")
            if isinstance(inner, str) and inner.strip():
                return inner.strip()
    return None


def detect_vendor_error_envelope(data: object, *, source: str = "") -> str | None:
    """Return a reason string when *data* is a 200-wrapped vendor error envelope, else ``None``.

    Detection (conservative — a legitimately-empty body returns ``None``):
      1. A message field whose text contains any credential/quota keyword.
      2. A ``status`` field that is an explicit error string (``"error"`` /
         ``"failed"`` ...) **and** an accompanying message — the message
         requirement keeps a bare status token from false-firing.

    Returns a NEW string; never mutates *data*. ``source`` is woven into the
    reason for an actionable banner message.
    """
    if not isinstance(data, dict):
        return None

    message = _message_text(data)
    lowered = message.lower() if message else ""

    if message and any(keyword in lowered for keyword in _ERROR_KEYWORDS):
        return _format_reason(source, message)

    status = data.get("status")
    if isinstance(status, str) and status.strip().lower() in _ERROR_STATUS_STRINGS and message:
        return _format_reason(source, message)

    return None


def _format_reason(source: str, message: str) -> str:
    """Build the human-readable reason string raised by the calling source."""
    prefix = f"{source} account error: " if source else "vendor error: "
    return f"{prefix}{message}"
