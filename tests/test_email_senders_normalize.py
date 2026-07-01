"""Tests for normalize_email_senders — idempotence and legacy config healing."""

from job_finder.config import normalize_email_senders
from job_finder.sources.email_senders import resolve_sender_label, resolve_sender_parsers


def test_normalize_email_senders_idempotent():
    """Applying normalize_email_senders twice is byte-stable."""
    # Build a populated legacy config with real user overrides
    legacy_cfg = {
        "sources": {
            "gmail": {
                "enabled": False,
                "senders": {
                    "linkedin_alerts": "custom@x.com",
                    "glassdoor": "g@x.com",
                },
                "lookback_days": 14,
            }
        }
    }

    # Apply once
    normalized_once = normalize_email_senders(legacy_cfg)

    # Apply twice
    normalized_twice = normalize_email_senders(normalized_once)

    # Byte-stable: n(n(cfg)) == n(cfg)
    assert normalized_once == normalized_twice


def test_normalize_email_senders_relocates_and_deletes_legacy_keys():
    """Legacy keys are moved to sources.imap and deleted from sources.gmail."""
    legacy_cfg = {
        "sources": {
            "gmail": {
                "enabled": False,
                "senders": {
                    "linkedin_alerts": "custom@x.com",
                    "glassdoor": "g@x.com",
                },
                "lookback_days": 14,
            }
        }
    }

    normalized = normalize_email_senders(legacy_cfg)

    # After normalize, sources.imap carries the values
    assert normalized["sources"]["imap"]["senders"] == {
        "linkedin_alerts": "custom@x.com",
        "glassdoor": "g@x.com",
    }
    assert normalized["sources"]["imap"]["lookback_days"] == 14

    # 'senders' and 'lookback_days' are ABSENT from sources.gmail (delete-after-move)
    assert "senders" not in normalized.get("sources", {}).get("gmail", {})
    assert "lookback_days" not in normalized.get("sources", {}).get("gmail", {})

    # sources.gmail.enabled still present
    assert normalized["sources"]["gmail"]["enabled"] is False


def test_normalize_email_senders_preserves_resolver_behavior():
    """Resolvers over normalized config equal result over original legacy config."""
    legacy_cfg = {
        "sources": {
            "gmail": {
                "enabled": False,
                "senders": {
                    "linkedin_alerts": "custom@x.com",
                    "glassdoor": "g@x.com",
                },
                "lookback_days": 14,
            }
        }
    }

    # Resolve from original legacy config (resolvers call normalize internally)
    parsers_from_legacy = resolve_sender_parsers(legacy_cfg)
    labels_from_legacy = resolve_sender_label(legacy_cfg)

    # Resolve from normalized config
    normalized_cfg = normalize_email_senders(legacy_cfg)
    parsers_from_normalized = resolve_sender_parsers(normalized_cfg)
    labels_from_normalized = resolve_sender_label(normalized_cfg)

    # Overrides preserved
    assert parsers_from_legacy == parsers_from_normalized
    assert labels_from_legacy == labels_from_normalized

    # Verify the override is in the resolved maps
    assert "custom@x.com" in parsers_from_legacy
    assert "g@x.com" in parsers_from_legacy
    assert labels_from_legacy["custom@x.com"] == "linkedin"
    assert labels_from_legacy["g@x.com"] == "glassdoor"


def test_normalize_email_senders_noop_when_no_legacy_keys():
    """Config without legacy keys is returned unchanged (shallow copy for immutability)."""
    cfg_no_legacy = {
        "sources": {
            "imap": {
                "enabled": True,
                "senders": {"linkedin_alerts": "override@x.com"},
                "lookback_days": 30,
            },
            "gmail": {"enabled": False},
        }
    }

    normalized = normalize_email_senders(cfg_no_legacy)

    # No changes made
    assert normalized["sources"]["imap"]["senders"] == {"linkedin_alerts": "override@x.com"}
    assert normalized["sources"]["imap"]["lookback_days"] == 30
    assert normalized["sources"]["gmail"]["enabled"] is False


def test_normalize_email_senders_empty_config():
    """Empty config is handled gracefully."""
    empty_cfg = {}
    normalized = normalize_email_senders(empty_cfg)
    assert normalized == {"sources": {}}


def test_normalize_email_senders_does_not_clobber_current_imap_values():
    """When both legacy gmail and current imap keys are populated simultaneously
    (e.g. a hand-restored backup, a partially migrated file), the current
    sources.imap value must survive -- not be silently reverted to the stale
    legacy one. The legacy key is still popped from sources.gmail either way."""
    cfg = {
        "sources": {
            "gmail": {
                "senders": {"linkedin_alerts": "old@x.com"},
                "lookback_days": 14,
            },
            "imap": {
                "senders": {"linkedin_alerts": "custom@x.com"},
                "lookback_days": 30,
            },
        }
    }

    normalized = normalize_email_senders(cfg)

    # Current imap values are preserved, not clobbered by the stale legacy ones.
    assert normalized["sources"]["imap"]["senders"] == {"linkedin_alerts": "custom@x.com"}
    assert normalized["sources"]["imap"]["lookback_days"] == 30

    # Legacy keys are still popped from sources.gmail regardless.
    assert "senders" not in normalized.get("sources", {}).get("gmail", {})
    assert "lookback_days" not in normalized.get("sources", {}).get("gmail", {})


def test_normalize_email_senders_partial_legacy():
    """Config with only one legacy key (senders OR lookback_days) is handled."""
    # Only senders legacy
    cfg_senders_only = {
        "sources": {
            "gmail": {
                "enabled": False,
                "senders": {"linkedin_alerts": "custom@x.com"},
            }
        }
    }
    normalized = normalize_email_senders(cfg_senders_only)
    assert normalized["sources"]["imap"]["senders"] == {"linkedin_alerts": "custom@x.com"}
    assert "senders" not in normalized["sources"]["gmail"]
    assert normalized["sources"]["gmail"]["enabled"] is False

    # Only lookback_days legacy
    cfg_lookback_only = {
        "sources": {
            "gmail": {
                "enabled": False,
                "lookback_days": 21,
            }
        }
    }
    normalized = normalize_email_senders(cfg_lookback_only)
    assert normalized["sources"]["imap"]["lookback_days"] == 21
    assert "lookback_days" not in normalized["sources"]["gmail"]
    assert normalized["sources"]["gmail"]["enabled"] is False
