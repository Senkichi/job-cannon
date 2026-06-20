"""Tests for the LLM jd-content adjudicator (PR2) — adjudicate_jd + backfill.

The LLM itself is mocked: these verify the call wiring (parse / error / missing
field) and the backfill state machine (CLEAN stamps, AMBIGUOUS-YES stamps,
AMBIGUOUS-NO heals + re-queues, undetermined is left to retry, stamped rows are
not re-selected).
"""

from __future__ import annotations

import json
import types

from job_finder.db._jd_content_contract import JD_CONTENT_VERSION, JD_OFFSITE

# Bodies engineered for a deterministic verdict:
_CLEAN_JD = (
    "We are looking for a Senior Data Scientist. Responsibilities include building "
    "models and running experiments. Qualifications: Python, SQL, statistics. What "
    "you'll do: ship models to production and mentor analysts. " * 4
)
# Grounded (mentions data/platform) + substantial + NO shape heading -> AMBIGUOUS.
_AMBIGUOUS_JD = (
    "About Acme. Acme builds data platforms for the enterprise. Our data tooling "
    "is best in class and our platform scales globally. We value bold engineers. " * 5
)


def _insert(conn, dedup_key, *, title, jd, classification="apply", tier="exhausted"):
    conn.execute(
        "INSERT INTO jobs (dedup_key, title, company, location, sources, "
        "unresolved_reasons, classification, sub_scores_json, fit_analysis, jd_full, "
        "enrichment_tier, first_seen, last_seen, pipeline_status) "
        "VALUES (?, ?, 'Acme Corp', '', '[\"careers_page\"]', '[]', ?, '{}', 'fit', ?, ?, "
        "'2026-01-01', '2026-01-01', 'discovered')",
        (dedup_key, title, classification, jd, tier),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# adjudicate_jd — call wiring
# ---------------------------------------------------------------------------


def _patch_call_model(monkeypatch, *, data=None, raises=False):
    from job_finder.web import jd_adjudicator

    def fake(*args, **kwargs):
        if raises:
            raise RuntimeError("boom")
        return types.SimpleNamespace(data=data)

    monkeypatch.setattr(jd_adjudicator, "call_model", fake)


def test_adjudicate_true(monkeypatch, migrated_db):
    from job_finder.web.jd_adjudicator import adjudicate_jd

    _path, conn = migrated_db
    _patch_call_model(monkeypatch, data={"is_job_description": True, "confidence": 0.9})
    assert adjudicate_jd("Data Scientist", "Acme", _CLEAN_JD, conn, {}) is True


def test_adjudicate_false(monkeypatch, migrated_db):
    from job_finder.web.jd_adjudicator import adjudicate_jd

    _path, conn = migrated_db
    _patch_call_model(monkeypatch, data={"is_job_description": False})
    assert adjudicate_jd("Data Scientist", "Acme", _CLEAN_JD, conn, {}) is False


def test_adjudicate_error_returns_none(monkeypatch, migrated_db):
    from job_finder.web.jd_adjudicator import adjudicate_jd

    _path, conn = migrated_db
    _patch_call_model(monkeypatch, raises=True)
    assert adjudicate_jd("Data Scientist", "Acme", _CLEAN_JD, conn, {}) is None


def test_adjudicate_missing_field_returns_none(monkeypatch, migrated_db):
    from job_finder.web.jd_adjudicator import adjudicate_jd

    _path, conn = migrated_db
    _patch_call_model(monkeypatch, data={"confidence": 0.5})  # no is_job_description
    assert adjudicate_jd("Data Scientist", "Acme", _CLEAN_JD, conn, {}) is None


def test_adjudicate_empty_jd_returns_none(migrated_db):
    from job_finder.web.jd_adjudicator import adjudicate_jd

    _path, conn = migrated_db
    assert adjudicate_jd("Data Scientist", "Acme", None, conn, {}) is None


# ---------------------------------------------------------------------------
# run_jd_adjudication_backfill — the state machine
# ---------------------------------------------------------------------------


def test_backfill_state_machine(monkeypatch, migrated_db):
    from job_finder.web import jd_adjudicator
    from job_finder.web.jd_adjudicator import run_jd_adjudication_backfill

    _path, conn = migrated_db
    _insert(conn, "acme|clean", title="Senior Data Scientist", jd=_CLEAN_JD)
    _insert(conn, "acme|yes", title="Data Platform Engineer YES", jd=_AMBIGUOUS_JD)
    _insert(conn, "acme|no", title="Data Platform Engineer NO", jd=_AMBIGUOUS_JD)
    _insert(conn, "acme|maybe", title="Data Platform Engineer MAYBE", jd=_AMBIGUOUS_JD)

    def fake_adjudicate(title, company, jd_full, c, config):
        if "YES" in (title or ""):
            return True
        if "NO" in (title or ""):
            return False
        return None  # MAYBE -> undetermined

    monkeypatch.setattr(jd_adjudicator, "adjudicate_jd", fake_adjudicate)

    summary = run_jd_adjudication_backfill(conn, {}, limit=50)

    assert summary["scanned"] == 4
    assert summary["llm_calls"] == 3  # the 3 AMBIGUOUS rows (clean skipped the LLM)
    assert summary["kept"] == 2  # clean + yes
    assert summary["rejected"] == 1  # no
    assert summary["undetermined"] == 1  # maybe

    def row(dk):
        return conn.execute(
            "SELECT jd_full, jd_adjudicated_version, unresolved_reasons, classification "
            "FROM jobs WHERE dedup_key = ?",
            (dk,),
        ).fetchone()

    # CLEAN: stamped, body kept, still scored.
    clean = row("acme|clean")
    assert clean["jd_adjudicated_version"] == JD_CONTENT_VERSION
    assert clean["jd_full"] is not None
    assert clean["classification"] == "apply"

    # AMBIGUOUS-YES: stamped, kept.
    yes = row("acme|yes")
    assert yes["jd_adjudicated_version"] == JD_CONTENT_VERSION
    assert yes["jd_full"] is not None

    # AMBIGUOUS-NO: healed — body cleared, quarantined, declassified.
    no = row("acme|no")
    assert no["jd_full"] is None
    assert JD_OFFSITE in json.loads(no["unresolved_reasons"])
    assert no["classification"] is None

    # AMBIGUOUS-undetermined: left unstamped for retry, body intact.
    maybe = row("acme|maybe")
    assert maybe["jd_adjudicated_version"] is None
    assert maybe["jd_full"] is not None


def test_backfill_skips_already_adjudicated(monkeypatch, migrated_db):
    from job_finder.web import jd_adjudicator
    from job_finder.web.jd_adjudicator import run_jd_adjudication_backfill

    _path, conn = migrated_db
    _insert(conn, "acme|done", title="Data Platform Engineer YES", jd=_AMBIGUOUS_JD)

    calls = {"n": 0}

    def fake_adjudicate(*a, **k):
        calls["n"] += 1
        return True

    monkeypatch.setattr(jd_adjudicator, "adjudicate_jd", fake_adjudicate)

    run_jd_adjudication_backfill(conn, {}, limit=50)  # stamps the row
    assert calls["n"] == 1
    run_jd_adjudication_backfill(conn, {}, limit=50)  # already stamped -> not re-selected
    assert calls["n"] == 1
