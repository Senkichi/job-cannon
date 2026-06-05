"""URL canonicalization helper for source_urls at the parser boundary.

Phase 49.01 / D-06 / F-05:
Canonicalize source URLs before writing to the jobs table. The canonical form:
  - Strips known tracking-parameter keys (utm_*, mc_*, fbclid, etc.)
  - Sorts remaining query parameters alphabetically
  - Lowercases scheme + hostname (netloc)
  - Preserves path and fragment as-is

On any parse error returns ``(raw, raw)`` — never raises.

NG-03: The canonical URL is NOT used as a dedup key in this phase.
Forensics: the original ``raw`` URL is preserved in ``source_urls_raw`` for
future algorithm iteration without losing source data.

Reference:
    .planning/specs/2026-05-29-ingestion-contract-enforcement.md §13 commit 49.01;
    D-06 in §7; F-05 in §4; NG-03 in §6.
"""

from __future__ import annotations

import logging
import urllib.parse

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tracking-parameter allowlist (keys to strip)
# ---------------------------------------------------------------------------

# Exact-match keys: any query parameter with one of these names is removed.
_TRACKING_EXACT: frozenset[str] = frozenset(
    {
        "gh_jid",
        "refId",
        "trk",
        "lipi",
        "ref",
        "fbclid",
        "mc_cid",
        "mc_eid",
        "_hsenc",
        "_hsmi",
    }
)

# Prefix families: any key whose name starts with one of these prefixes is removed.
_TRACKING_PREFIXES: tuple[str, ...] = (
    "utm_",
    "mc_",
)


def _is_tracking_param(key: str) -> bool:
    """Return True if this query-parameter key should be stripped.

    Checks exact membership first (O(1)) then prefix families.
    """
    if key in _TRACKING_EXACT:
        return True
    return any(key.startswith(prefix) for prefix in _TRACKING_PREFIXES)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def canonicalize_url(raw: str) -> tuple[str, str]:
    """Return ``(canonical, raw)`` for a single URL string.

    Canonical form:
    - Scheme and host lowercased.
    - Tracking query parameters stripped (utm_*, mc_*, fbclid, refId, trk,
      lipi, ref, gh_jid, mc_cid, mc_eid, _hsenc, _hsmi — exact and wildcard).
    - Remaining query parameters sorted alphabetically by key.
    - Path and fragment preserved unchanged.

    On any parse error returns ``(raw, raw)`` — never raises, logs at DEBUG.

    Args:
        raw: The original URL string as received from the parser.

    Returns:
        A ``(canonical_url, raw_url)`` 2-tuple. The second element is always
        the unchanged input; the first is the canonicalized form (or ``raw``
        on error).
    """
    if not raw:
        return raw, raw

    try:
        parsed = urllib.parse.urlsplit(raw)

        # Filter tracking params and sort the survivors alphabetically.
        qs_pairs = [
            (k, v)
            for k, v in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
            if not _is_tracking_param(k)
        ]
        qs_pairs.sort(key=lambda kv: kv[0])

        canonical = urllib.parse.urlunsplit(
            (
                parsed.scheme.lower(),
                parsed.netloc.lower(),  # netloc = host (+ optional :port)
                parsed.path,
                urllib.parse.urlencode(qs_pairs),
                parsed.fragment,
            )
        )
        return canonical, raw

    except Exception:  # pragma: no cover — defensive; urlsplit rarely raises
        logger.debug("canonicalize_url: failed to parse %r, returning raw", raw)
        return raw, raw
