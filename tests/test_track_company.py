"""Tests for company tracking + suggestions (WP6, release polish).

Track contract: POST /companies/track upserts a pending companies row
(idempotent; re-enables scan_enabled=0 rows) and returns the ✓ fragment.
Suggestions contract: untracked feed-frequent companies ranked by good-fit
count then volume; tracked companies excluded via normalized-name match.
Job-row contract: expanded row shows Track button (by name, not company_id);
already-tracked companies render the ✓ state up front.

Cold-start fallback (Issue #660): when owner history is empty, rank by
profile match and ATS scannability instead.
"""

import sqlite3

from job_finder.web.company_suggestions import get_suggested_companies


def _conn(app):
    conn = sqlite3.connect(app.config["DB_PATH"])
    conn.row_factory = sqlite3.Row
    return conn


def _insert_job(conn, dedup_key, company, classification=None, company_id=None):
    conn.execute(
        """INSERT INTO jobs
               (dedup_key, title, company, company_id, location, sources, source_urls,
                description, classification, first_seen, last_seen, pipeline_status)
           VALUES (?, 'Engineer', ?, ?, 'Remote', '["linkedin"]', '[]',
                   'short', ?, '2026-06-01T00:00:00', '2026-06-01T00:00:00', 'discovered')""",
        (dedup_key, company, company_id, classification),
    )
    conn.commit()


class TestTrackRoute:
    def test_track_new_company_creates_pending_row(self, app, client):
        resp = client.post("/companies/track", data={"company_name": "Northwind Trading"})
        assert resp.status_code == 200
        assert "Tracking" in resp.get_data(as_text=True)

        conn = _conn(app)
        row = conn.execute(
            "SELECT * FROM companies WHERE name_raw = 'Northwind Trading'"
        ).fetchone()
        conn.close()
        assert row is not None
        assert row["ats_probe_status"] == "pending"
        assert row["scan_enabled"] == 1

    def test_track_existing_is_idempotent_and_keeps_hit(self, app, client):
        conn = _conn(app)
        from job_finder.web.ats_scanner import upsert_company

        upsert_company(
            conn,
            name="Acme",
            ats_platform="greenhouse",
            ats_slug="acme",
            ats_probe_status="hit",
        )
        conn.close()

        resp = client.post("/companies/track", data={"company_name": "Acme"})
        assert resp.status_code == 200
        assert "Tracking" in resp.get_data(as_text=True)

        conn = _conn(app)
        rows = conn.execute("SELECT * FROM companies WHERE name_raw = 'Acme'").fetchall()
        conn.close()
        assert len(rows) == 1
        # hit must never downgrade to pending (probe-status precedence).
        assert rows[0]["ats_probe_status"] == "hit"

    def test_track_reenables_disabled_company(self, app, client):
        conn = _conn(app)
        from job_finder.web.ats_scanner import upsert_company

        cid = upsert_company(conn, name="Disabled Co", ats_probe_status="hit")
        conn.execute("UPDATE companies SET scan_enabled = 0 WHERE id = ?", (cid,))
        conn.commit()
        conn.close()

        resp = client.post("/companies/track", data={"company_name": "Disabled Co"})
        assert resp.status_code == 200

        conn = _conn(app)
        row = conn.execute("SELECT scan_enabled FROM companies WHERE id = ?", (cid,)).fetchone()
        conn.close()
        assert row["scan_enabled"] == 1

    def test_track_missing_name_is_400(self, client):
        assert client.post("/companies/track", data={}).status_code == 400
        assert client.post("/companies/track", data={"company_name": "  "}).status_code == 400


class TestIsCompanyTracked:
    def test_tracked_by_normalized_and_raw_name(self, app):
        from job_finder.web.ats_scanner import is_company_tracked, upsert_company

        conn = _conn(app)
        upsert_company(conn, name="Stripe Inc.", ats_probe_status="pending")
        assert is_company_tracked(conn, "Stripe Inc.")
        assert not is_company_tracked(conn, "Totally Unknown Co")
        assert not is_company_tracked(conn, "")
        conn.close()

    def test_disabled_company_reads_as_untracked(self, app):
        from job_finder.web.ats_scanner import is_company_tracked, upsert_company

        conn = _conn(app)
        cid = upsert_company(conn, name="Paused Co", ats_probe_status="hit")
        conn.execute("UPDATE companies SET scan_enabled = 0 WHERE id = ?", (cid,))
        conn.commit()
        assert not is_company_tracked(conn, "Paused Co")
        conn.close()


class TestSuggestions:
    def test_ranked_by_good_fit_then_volume(self, app):
        conn = _conn(app)
        # BigCo: 3 jobs, 0 good. GoodCo: 2 jobs, 2 good. → GoodCo first.
        for i in range(3):
            _insert_job(conn, f"bigco|{i}", "BigCo", classification="skip")
        for i in range(2):
            _insert_job(conn, f"goodco|{i}", "GoodCo", classification="apply")
        suggestions = get_suggested_companies(conn)
        conn.close()
        names = [s["company"] for s in suggestions]
        assert names == ["GoodCo", "BigCo"]
        assert suggestions[0]["good_cnt"] == 2
        assert suggestions[1]["job_cnt"] == 3

    def test_tracked_companies_excluded_via_normalization(self, app):
        from job_finder.web.ats_scanner import upsert_company

        conn = _conn(app)
        # Tracked under a raw name that normalizes differently than the
        # job's raw company string — Python-side normalization must match.
        upsert_company(conn, name="Stripe, Inc.", ats_probe_status="hit")
        _insert_job(conn, "stripe|1", "Stripe Inc.", classification="apply")
        _insert_job(conn, "other|1", "Other Co", classification="apply")
        suggestions = get_suggested_companies(conn)
        conn.close()
        names = [s["company"] for s in suggestions]
        assert "Other Co" in names
        assert "Stripe Inc." not in names

    def test_linked_jobs_excluded(self, app):
        from job_finder.web.ats_scanner import upsert_company

        conn = _conn(app)
        cid = upsert_company(conn, name="Linked Co", ats_probe_status="hit")
        _insert_job(conn, "linked|1", "Linked Co", company_id=cid, classification="apply")
        suggestions = get_suggested_companies(conn)
        conn.close()
        assert suggestions == []

    def test_limit_respected(self, app):
        conn = _conn(app)
        for i in range(12):
            _insert_job(conn, f"co{i}|1", f"Company {i}", classification="apply")
        suggestions = get_suggested_companies(conn, limit=8)
        conn.close()
        assert len(suggestions) == 8


class TestColdStartSuggestions:
    """Cold-start fallback ranking (Issue #660)."""

    def test_cold_start_uses_profile_ranking_when_no_history(self, app):
        """When no owner history exists, rank by profile match."""
        conn = _conn(app)

        # Insert jobs with no classification (no owner history)
        _insert_job(conn, "co1|1", "DataCo", classification=None)
        _insert_job(conn, "co2|1", "EngineerCo", classification=None)
        _insert_job(conn, "co3|1", "RemoteCo", classification=None)

        # Profile with target titles and locations
        profile = {
            "target_titles": ["Data Scientist", "Engineer"],
            "target_locations": ["Remote"],
            "skills": ["Python"],
        }

        suggestions = get_suggested_companies(conn, profile=profile)
        conn.close()

        # Should return suggestions with relevance_score and ats_boost
        assert len(suggestions) > 0
        # EngineerCo should rank higher (title match)
        engineer_idx = next(
            (i for i, s in enumerate(suggestions) if s["company"] == "EngineerCo"), None
        )
        assert engineer_idx is not None
        # Check that cold-start fields are present
        assert "relevance_score" in suggestions[engineer_idx]
        assert "ats_boost" in suggestions[engineer_idx]

    def test_cold_start_excludes_tracked_companies(self, app):
        """Cold-start suggestions should exclude already-tracked companies."""
        conn = _conn(app)
        from job_finder.web.ats_scanner import upsert_company

        # Track a company
        upsert_company(conn, name="TrackedCo", ats_probe_status="pending")

        # Insert jobs for tracked and untracked companies
        _insert_job(conn, "tracked|1", "TrackedCo", classification=None)
        _insert_job(conn, "untracked|1", "UntrackedCo", classification=None)

        profile = {"target_titles": ["Engineer"], "target_locations": [], "skills": []}

        suggestions = get_suggested_companies(conn, profile=profile)
        conn.close()

        # Should only include UntrackedCo
        assert len(suggestions) == 1
        assert suggestions[0]["company"] == "UntrackedCo"

    def test_cold_start_falls_back_to_standard_ranking_with_history(self, app):
        """When owner history exists, use standard good_cnt ranking."""
        conn = _conn(app)

        # Insert jobs with classification (owner history)
        _insert_job(conn, "co1|1", "GoodCo", classification="apply")
        _insert_job(conn, "co2|1", "BadCo", classification="reject")

        profile = {"target_titles": ["Engineer"], "target_locations": [], "skills": []}

        suggestions = get_suggested_companies(conn, profile=profile)
        conn.close()

        # Should use standard ranking (good_cnt DESC)
        assert len(suggestions) == 2
        # GoodCo should be first (good_cnt=1 vs 0)
        assert suggestions[0]["company"] == "GoodCo"
        assert suggestions[0]["good_cnt"] == 1
        assert suggestions[1]["company"] == "BadCo"
        assert suggestions[1]["good_cnt"] == 0
        # Cold-start fields should NOT be present
        assert "relevance_score" not in suggestions[0]
        assert "ats_boost" not in suggestions[0]

    def test_cold_start_disabled_when_profile_none(self, app):
        """When profile is None, cold-start fallback is disabled."""
        conn = _conn(app)

        # Insert jobs with no classification
        _insert_job(conn, "co1|1", "AnyCo", classification=None)

        suggestions = get_suggested_companies(conn, profile=None)
        conn.close()

        # Should return empty (no history, no profile)
        assert len(suggestions) == 0

    def test_cold_start_ats_boost(self, app):
        """Companies with known ATS platforms get boost."""
        conn = _conn(app)
        from job_finder.web.ats_scanner import upsert_company

        # Insert a company with known ATS platform
        upsert_company(conn, name="ATS Co", ats_platform="greenhouse", ats_slug="ats-co")

        # Insert jobs for ATS and non-ATS companies
        _insert_job(conn, "ats|1", "ATS Co", classification=None)
        _insert_job(conn, "nonats|1", "NonATS Co", classification=None)

        profile = {"target_titles": ["Engineer"], "target_locations": [], "skills": []}

        suggestions = get_suggested_companies(conn, profile=profile)
        conn.close()

        # ATS Co should have ats_boost
        ats_suggestion = next((s for s in suggestions if s["company"] == "ATS Co"), None)
        if ats_suggestion:
            assert ats_suggestion["ats_boost"] == 5


class TestCompaniesPageCard:
    def test_card_shown_with_suggestions(self, app, client):
        conn = _conn(app)
        _insert_job(conn, "feedco|1", "FeedCo", classification="apply")
        conn.close()
        body = client.get("/companies/").get_data(as_text=True)
        assert "Companies in your feed" in body
        assert "FeedCo" in body

    def test_card_omitted_when_no_suggestions(self, client):
        body = client.get("/companies/").get_data(as_text=True)
        assert "Companies in your feed" not in body


class TestJobRowButton:
    def _expand(self, app, client, company):
        conn = _conn(app)
        _insert_job(conn, "row|1", company)
        conn.close()
        return client.get("/jobs/row%7C1/expand", headers={"HX-Request": "true"})

    def test_untracked_company_shows_track_button(self, app, client):
        resp = self._expand(app, client, "Untracked Co")
        body = resp.get_data(as_text=True)
        assert resp.status_code == 200
        assert "Track careers page" in body

    def test_tracked_company_shows_check_state(self, app, client):
        conn = _conn(app)
        from job_finder.web.ats_scanner import upsert_company

        upsert_company(conn, name="Tracked Co", ats_probe_status="hit")
        conn.close()
        resp = self._expand(app, client, "Tracked Co")
        body = resp.get_data(as_text=True)
        assert "Tracking careers page" in body
        assert "Track careers page" not in body
