"""Phase D / D2 — email dual-parse shadow guard + corpus provenance (I3).

Covers:
- ``extract_primary`` factor-out (primary-only, no positional fallback).
- The gmail/imap gate dual-parse: when an override yields, the legacy PRIMARY
  parser also runs and the extraction record carries ``legacy_count`` +
  ``extractor="override"``; with no override the legacy path runs exactly once.
- imap record-label regression: mixed-case From addresses land under the
  matched sender_key's canonical label (the gate and the record now agree).
- Shadow comparison in ``record_extraction``: legacy outperforming the
  override ``SHADOW_ROLLBACK_WINS`` times consecutively → auto-rollback
  (status healthy); win-then-loss resets; ``legacy_count=None`` never touches
  the counter.
- Corpus provenance: snapshots carry ``extractor``; ``assemble_inputs``
  excludes override-produced positives from baselines (I3) while keeping
  legacy positives and pre-D2 rows.
- End-to-end: rollback fires through the real ``_record_email_extractions``
  wiring and subsequent baselines contain no override-era positives.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from unittest.mock import patch

from job_finder.models import Job
from job_finder.parsers import extract_primary, extract_with_fallback
from job_finder.web.autoheal import codegen, corpus_store, override_loader
from job_finder.web.autoheal import health_monitor as hm
from job_finder.web.autoheal.override_loader import OverrideLoader
from job_finder.web.db_migrate import run_migrations

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_RECIPE_HTML = (
    "<div class='job'><span class='title'>Engineer</span>"
    "<a href='https://example.com/1'>Apply</a>"
    "<span class='company'>Acme</span></div>" + "<!-- pad -->" * 40
)

_EMAIL_RECIPE = {
    "source": "linkedin",
    "container_selector": "div.job",
    "fields": {
        "title": {"selector": ".title", "attr": "text"},
        "url": {"selector": "a", "attr": "href"},
        "company": {"selector": ".company", "attr": "text"},
    },
}

_FAKE_DATE = datetime(2026, 1, 1)

_LEGACY_JOB = Job(
    title="Legacy Engineer",
    company="Legacy Corp",
    location="Remote",
    source="linkedin",
    source_url="https://example.com/legacy/1",
)


def _conn(tmp_path) -> tuple[str, sqlite3.Connection]:
    db = str(tmp_path / "t.db")
    run_migrations(db)
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    return db, c


def _isolated_loader(tmp_path, monkeypatch) -> tuple[OverrideLoader, object]:
    overrides_dir = tmp_path / "overrides"
    loader = OverrideLoader(overrides_root=overrides_dir)
    monkeypatch.setattr(override_loader, "_LOADER", loader)
    return loader, overrides_dir


def _seed_health(conn, source: str, *, status="healthy", wins=0):
    conn.execute(
        "INSERT INTO source_health (source, surface, status, consecutive_breaks, "
        "baseline_yield, updated_at, shadow_legacy_wins) VALUES (?, 'email', ?, 0, 1.0, '', ?)",
        (source, status, wins),
    )
    conn.commit()


def _health(conn, source):
    return conn.execute(
        "SELECT status, shadow_legacy_wins, heal_attempts FROM source_health WHERE source=?",
        (source,),
    ).fetchone()


def _audit_outcomes(conn, source: str) -> list[str]:
    return [
        r[0]
        for r in conn.execute(
            "SELECT outcome FROM heal_audit WHERE source = ? ORDER BY id", (source,)
        ).fetchall()
    ]


# ---------------------------------------------------------------------------
# extract_primary
# ---------------------------------------------------------------------------


def test_extract_primary_no_positional_fallback():
    """Primary returns [] → extract_primary stays []; the fallback fires only in the two-step path."""
    body = "Check out https://boards.greenhouse.io/acme/jobs/123 Engineer role"

    def _empty_primary(b, d):
        return []

    with (
        patch("job_finder.parsers.has_job_urls", return_value=True),
        patch(
            "job_finder.parsers.positional_fallback", return_value=[_LEGACY_JOB]
        ) as mock_fallback,
    ):
        assert extract_primary(_empty_primary, body, _FAKE_DATE) == []
        mock_fallback.assert_not_called()  # primary-only path never reaches the fallback
        # The two-step path still fires the positional fallback on the same input.
        assert extract_with_fallback(_empty_primary, body, _FAKE_DATE) == [_LEGACY_JOB]


def test_extract_primary_returns_primary_result():
    def _primary(b, d):
        return [_LEGACY_JOB]

    assert extract_primary(_primary, "body", _FAKE_DATE) == [_LEGACY_JOB]


def test_extract_with_fallback_unchanged_when_primary_yields():
    def _primary(b, d):
        return [_LEGACY_JOB]

    assert extract_with_fallback(_primary, "body", _FAKE_DATE) == [_LEGACY_JOB]


# ---------------------------------------------------------------------------
# Gmail gate dual-parse (drives the real fetch_jobs loop)
# ---------------------------------------------------------------------------


def _gmail_source_with_one_message(body: str):
    """Construct a GmailSource (no auth) that yields one linkedin message."""
    from job_finder.sources.gmail_source import GmailSource

    src = GmailSource.__new__(GmailSource)
    src.service = None
    src.parse_failures = []
    src.extraction_records = []

    def _search(query, max_messages=500):
        # Only the two linkedin senders return a message; others none.
        return [{"id": "m1"}] if "linkedin.com" in query else []

    src._search_messages = _search
    src._get_message = lambda msg_id: {"id": msg_id}
    src._extract_body = lambda msg: body
    src._extract_date = lambda msg: _FAKE_DATE
    return src


def test_gmail_override_dual_parses_and_records_provenance(tmp_path, monkeypatch):
    _isolated_loader(tmp_path, monkeypatch)
    override_loader.write_override("email", "linkedin", _EMAIL_RECIPE)
    override_loader.reload()
    src = _gmail_source_with_one_message(_RECIPE_HTML)

    with patch(
        "job_finder.sources.gmail_source.extract_primary", return_value=[_LEGACY_JOB]
    ) as mock_ep:
        jobs, _ = src.fetch_jobs()

    # Two linkedin sender addresses share the label; each message dual-parses.
    assert mock_ep.call_count == 2
    recs = [r for r in src.extraction_records if r["label"] == "linkedin"]
    assert len(recs) == 2
    for rec in recs:
        assert rec["extractor"] == "override"
        assert rec["legacy_count"] == 1
        assert rec["job_count"] >= 1
    # The override's jobs won the dispatch.
    assert all(j.source == "email_recipe" for j in jobs)


def test_gmail_no_override_single_parse(tmp_path, monkeypatch):
    _isolated_loader(tmp_path, monkeypatch)  # empty loader
    src = _gmail_source_with_one_message(_RECIPE_HTML)

    with (
        patch(
            "job_finder.sources.gmail_source.extract_with_fallback", return_value=[_LEGACY_JOB]
        ) as mock_ewf,
        patch("job_finder.sources.gmail_source.extract_primary") as mock_ep,
    ):
        src.fetch_jobs()

    assert mock_ewf.call_count == 2  # once per linkedin sender — no double parse
    mock_ep.assert_not_called()
    for rec in src.extraction_records:
        assert rec["legacy_count"] is None
        assert rec["extractor"] == "legacy"


def test_gmail_override_empty_falls_through(tmp_path, monkeypatch):
    """Override present but matching nothing → legacy path, extractor='legacy'."""
    _isolated_loader(tmp_path, monkeypatch)
    override_loader.write_override("email", "linkedin", _EMAIL_RECIPE)
    override_loader.reload()
    src = _gmail_source_with_one_message("<p>no job markup here</p>" + "x" * 300)

    with patch(
        "job_finder.sources.gmail_source.extract_with_fallback", return_value=[_LEGACY_JOB]
    ) as mock_ewf:
        src.fetch_jobs()

    assert mock_ewf.call_count == 2
    for rec in src.extraction_records:
        assert rec["extractor"] == "legacy"
        assert rec["legacy_count"] is None


# ---------------------------------------------------------------------------
# IMAP gate — mixed-case From regression + dual-parse twin
# ---------------------------------------------------------------------------


def _imap_fetch_with_message(raw_from: str, body_html: str):
    """Drive IMAPSource.fetch_jobs with one fake message via a mocked IMAPClient."""
    import email.message

    from job_finder.sources.imap_source import ImapSource

    msg = email.message.EmailMessage()
    msg["From"] = raw_from
    msg["Subject"] = "Job alert"
    msg["Date"] = "Thu, 01 Jan 2026 00:00:00 +0000"
    msg.set_content(body_html)

    src = ImapSource.__new__(ImapSource)
    src.host = "imap.test"
    src.port = 993
    src.email_address = "user@test"
    src.app_password = "pw"
    src.folder = "INBOX"
    src.parse_failures = []
    src.extraction_records = []

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def login(self, *a):
            pass

        def select_folder(self, *a, **k):
            pass

        def search(self, criteria):
            return [1]

        def fetch(self, uids, parts):
            return {1: {b"BODY[]": msg.as_bytes()}}

        def add_flags(self, *a, **k):
            pass

    with patch("job_finder.sources.imap_source.IMAPClient", _FakeClient):
        jobs, _ = src.fetch_jobs()
    return src, jobs


def test_imap_mixed_case_from_records_canonical_label(tmp_path, monkeypatch):
    """Regression: a mixed-case From address must record under the gate's label."""
    _isolated_loader(tmp_path, monkeypatch)
    src, _ = _imap_fetch_with_message(
        "LinkedIn <JobAlerts-NoReply@LinkedIn.com>", "<p>body</p>" + "x" * 300
    )

    assert len(src.extraction_records) == 1
    assert src.extraction_records[0]["label"] == "linkedin"


def test_imap_override_dual_parses(tmp_path, monkeypatch):
    _isolated_loader(tmp_path, monkeypatch)
    override_loader.write_override("email", "linkedin", _EMAIL_RECIPE)
    override_loader.reload()

    with patch(
        "job_finder.sources.imap_source.extract_primary", return_value=[_LEGACY_JOB]
    ) as mock_ep:
        src, jobs = _imap_fetch_with_message(
            "LinkedIn <jobalerts-noreply@linkedin.com>", _RECIPE_HTML
        )

    mock_ep.assert_called_once()
    rec = src.extraction_records[0]
    assert rec["label"] == "linkedin"
    assert rec["extractor"] == "override"
    assert rec["legacy_count"] == 1


# ---------------------------------------------------------------------------
# Shadow comparison + rollback in record_extraction
# ---------------------------------------------------------------------------


def test_two_consecutive_legacy_wins_roll_back(tmp_path, monkeypatch):
    loader, overrides_dir = _isolated_loader(tmp_path, monkeypatch)
    db, conn = _conn(tmp_path)
    _seed_health(conn, "linkedin")
    override_loader.write_override("email", "linkedin", _EMAIL_RECIPE)
    override_loader.reload()

    hm.record_extraction(
        conn, "linkedin", "email", _RECIPE_HTML, 1, legacy_count=3, extractor="override"
    )
    assert _health(conn, "linkedin")["shadow_legacy_wins"] == 1
    assert (overrides_dir / "email" / "linkedin.json").is_file()  # one fluke ≠ rollback

    hm.record_extraction(
        conn, "linkedin", "email", _RECIPE_HTML, 1, legacy_count=3, extractor="override"
    )

    assert not (overrides_dir / "email" / "linkedin.json").exists()
    health = _health(conn, "linkedin")
    assert health["status"] == "healthy"
    assert health["shadow_legacy_wins"] == 0  # zeroed by rollback (I2)
    assert "rolled_back:legacy_outperformed" in _audit_outcomes(conn, "linkedin")


def test_win_then_loss_resets_counter(tmp_path, monkeypatch):
    loader, overrides_dir = _isolated_loader(tmp_path, monkeypatch)
    db, conn = _conn(tmp_path)
    _seed_health(conn, "linkedin")
    override_loader.write_override("email", "linkedin", _EMAIL_RECIPE)
    override_loader.reload()

    hm.record_extraction(
        conn, "linkedin", "email", _RECIPE_HTML, 1, legacy_count=3, extractor="override"
    )
    hm.record_extraction(
        conn, "linkedin", "email", _RECIPE_HTML, 4, legacy_count=3, extractor="override"
    )

    assert _health(conn, "linkedin")["shadow_legacy_wins"] == 0
    assert (overrides_dir / "email" / "linkedin.json").is_file()  # no rollback
    assert _audit_outcomes(conn, "linkedin") == []


def test_no_legacy_count_never_touches_counter(tmp_path, monkeypatch):
    _isolated_loader(tmp_path, monkeypatch)
    db, conn = _conn(tmp_path)
    _seed_health(conn, "linkedin", wins=1)

    hm.record_extraction(conn, "linkedin", "email", _RECIPE_HTML, 0)

    assert _health(conn, "linkedin")["shadow_legacy_wins"] == 1


def test_ats_surface_calls_unaffected(tmp_path, monkeypatch):
    _isolated_loader(tmp_path, monkeypatch)
    db, conn = _conn(tmp_path)
    _seed_health(conn, "ats:lever", wins=1)

    hm.record_extraction(conn, "ats:lever", "ats", json.dumps([{"x": 1}]) + "z" * 300, 2)

    assert _health(conn, "ats:lever")["shadow_legacy_wins"] == 1


def test_mid_batch_double_trigger_is_safe(tmp_path, monkeypatch):
    """Records 3+4 (drained after the override is gone) re-reach the threshold:
    the second rollback finds no file, zeroes the counter (I2), audits nothing."""
    loader, overrides_dir = _isolated_loader(tmp_path, monkeypatch)
    db, conn = _conn(tmp_path)
    _seed_health(conn, "linkedin")
    override_loader.write_override("email", "linkedin", _EMAIL_RECIPE)
    override_loader.reload()

    for _ in range(4):  # rollback fires after #2; #3-#4 re-trigger against no file
        hm.record_extraction(
            conn, "linkedin", "email", _RECIPE_HTML, 1, legacy_count=3, extractor="override"
        )

    outcomes = _audit_outcomes(conn, "linkedin")
    assert outcomes.count("rolled_back:legacy_outperformed") == 1  # no audit dupe
    assert _health(conn, "linkedin")["shadow_legacy_wins"] == 0  # zeroed by no-op rollback (I2)


# ---------------------------------------------------------------------------
# Corpus provenance (I3)
# ---------------------------------------------------------------------------


def test_snapshot_carries_extractor(tmp_path, monkeypatch):
    _isolated_loader(tmp_path, monkeypatch)
    db, conn = _conn(tmp_path)

    hm.record_extraction(conn, "linkedin", "email", _RECIPE_HTML, 2, extractor="override")

    out = conn.execute("SELECT output_json FROM corpus_sample WHERE source='linkedin'").fetchone()[
        0
    ]
    assert json.loads(out)["extractor"] == "override"


def test_assemble_inputs_excludes_override_positives(tmp_path):
    db, conn = _conn(tmp_path)
    # Pre-D2 row (no extractor key) — must stay baseline-eligible.
    corpus_store.append_sample(
        conn, "linkedin", "email", "pre-d2 working " + "a" * 300, {"job_count": 2}
    )
    # Legacy positive — baseline-eligible.
    corpus_store.append_sample(
        conn,
        "linkedin",
        "email",
        "legacy working " + "b" * 300,
        {"job_count": 2, "extractor": "legacy"},
    )
    # Override positive — EXCLUDED from baselines (I3).
    corpus_store.append_sample(
        conn,
        "linkedin",
        "email",
        "override era " + "c" * 300,
        {"job_count": 5, "extractor": "override"},
    )
    # Zero-yields are failing evidence regardless of extractor.
    corpus_store.append_sample(
        conn,
        "linkedin",
        "email",
        "override broke " + "d" * 300,
        {"job_count": 0, "extractor": "override"},
    )

    inputs = codegen.assemble_inputs(conn, "linkedin", "email")

    joined_baseline = "\n".join(inputs["baseline_samples"])
    assert "pre-d2 working" in joined_baseline
    assert "legacy working" in joined_baseline
    assert "override era" not in joined_baseline
    assert any("override broke" in s for s in inputs["failing_samples"])


# ---------------------------------------------------------------------------
# End-to-end through _record_email_extractions
# ---------------------------------------------------------------------------


def test_end_to_end_shadow_rollback_and_clean_baseline(tmp_path, monkeypatch):
    from job_finder.web.ingestion_runner import _record_email_extractions

    loader, overrides_dir = _isolated_loader(tmp_path, monkeypatch)
    db, conn = _conn(tmp_path)
    _seed_health(conn, "linkedin")
    override_loader.write_override("email", "linkedin", _EMAIL_RECIPE)
    override_loader.reload()

    class _Src:
        extraction_records = [
            {
                "label": "linkedin",
                "raw_text": "override sample " + "e" * 300,
                "job_count": 1,
                "legacy_count": 3,
                "extractor": "override",
            },
            {
                "label": "linkedin",
                "raw_text": "override sample " + "f" * 300,
                "job_count": 1,
                "legacy_count": 3,
                "extractor": "override",
            },
        ]

    _record_email_extractions(_Src(), conn, {})

    # Rollback fired through the real wiring.
    assert not (overrides_dir / "email" / "linkedin.json").exists()
    assert "rolled_back:legacy_outperformed" in _audit_outcomes(conn, "linkedin")
    # Baselines exclude the override-era positives.
    inputs = codegen.assemble_inputs(conn, "linkedin", "email")
    assert all("override sample" not in s for s in inputs["baseline_samples"])
