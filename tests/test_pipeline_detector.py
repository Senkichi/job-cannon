"""Unit and integration tests for pipeline_detector.py.

Tests the detection engine: Gmail query, classify, match, score, auto-update or queue.
Uses unittest.mock to mock Gmail API calls.
"""

from datetime import datetime, timedelta

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _insert_job(
    conn,
    dedup_key,
    title,
    company,
    pipeline_status="reviewing",
    first_seen=None,
    location="Remote",
):
    """Insert a test job row into the migrated DB."""
    if first_seen is None:
        first_seen = (datetime.now() - timedelta(days=5)).isoformat()
    last_seen = datetime.now().isoformat()
    conn.execute(
        """INSERT OR IGNORE INTO jobs
           (dedup_key, title, company, location, sources, source_urls,
            source_id, salary_min, salary_max, description,
            first_seen, last_seen, score, score_breakdown, user_interest,
            pipeline_status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            dedup_key,
            title,
            company,
            location,
            '["linkedin"]',
            '["https://linkedin.com/jobs/1"]',
            "1",
            150000,
            200000,
            "Test job description",
            first_seen,
            last_seen,
            8.5,
            "{}",
            "interested",
            pipeline_status,
        ),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Unit tests: _classify_email
# ---------------------------------------------------------------------------


class TestClassifyEmail:
    """Tests for _classify_email(subject, body)."""

    def test_classify_rejection_unfortunately_subject(self):
        from job_finder.web.pipeline_detector import _classify_email

        result = _classify_email(
            subject="unfortunately, we will not be moving forward",
            body="Thank you for your interest.",
        )
        assert result == "rejection"

    def test_classify_rejection_not_moving_forward_body(self):
        from job_finder.web.pipeline_detector import _classify_email

        result = _classify_email(
            subject="Your application at Stripe",
            body="After careful consideration, we are not moving forward with your application.",
        )
        assert result == "rejection"

    def test_classify_rejection_other_candidates(self):
        from job_finder.web.pipeline_detector import _classify_email

        result = _classify_email(
            subject="Application update",
            body="We have decided to move forward with other candidates.",
        )
        assert result == "rejection"

    def test_classify_interview_subject_interview(self):
        from job_finder.web.pipeline_detector import _classify_email

        result = _classify_email(
            subject="Interview invitation - Senior Data Scientist",
            body="We would love to schedule an interview with you.",
        )
        assert result == "interview"

    def test_classify_interview_next_steps(self):
        from job_finder.web.pipeline_detector import _classify_email

        result = _classify_email(
            subject="Next steps for your application",
            body="We would like to discuss next steps in the hiring process.",
        )
        assert result == "interview"

    def test_classify_interview_phone_screen(self):
        from job_finder.web.pipeline_detector import _classify_email

        result = _classify_email(
            subject="Phone screen invitation",
            body="We would like to schedule a phone screen with you.",
        )
        assert result == "interview"

    def test_classify_confirmation_application_received(self):
        from job_finder.web.pipeline_detector import _classify_email

        result = _classify_email(
            subject="Application received - Data Scientist",
            body="We have received your application and will review it shortly.",
        )
        assert result == "confirmation"

    def test_classify_confirmation_thank_you_for_applying(self):
        from job_finder.web.pipeline_detector import _classify_email

        result = _classify_email(
            subject="Thank you for applying",
            body="Thank you for applying to the Data Scientist position.",
        )
        assert result == "confirmation"

    def test_classify_none_unrelated_newsletter(self):
        from job_finder.web.pipeline_detector import _classify_email

        result = _classify_email(
            subject="Weekly newsletter - Top 10 jobs this week",
            body="Check out these exciting opportunities in data science.",
        )
        assert result is None

    def test_classify_none_marketing_email(self):
        from job_finder.web.pipeline_detector import _classify_email

        result = _classify_email(
            subject="Special offer: 50% off premium membership",
            body="Upgrade your account today and get access to premium features.",
        )
        assert result is None


# ---------------------------------------------------------------------------
# Unit tests: _company_in_email
# ---------------------------------------------------------------------------


class TestCompanyInEmail:
    """Tests for _company_in_email(company, body, subject)."""

    def test_exact_match_in_body(self):
        from job_finder.web.pipeline_detector import _company_in_email

        result = _company_in_email(
            "Stripe",
            body="Dear candidate, Stripe has reviewed your application.",
            subject="Update from Stripe",
        )
        assert result is True

    def test_exact_match_in_subject(self):
        from job_finder.web.pipeline_detector import _company_in_email

        result = _company_in_email(
            "Stripe",
            body="We have reviewed your application.",
            subject="Stripe - application update",
        )
        assert result is True

    def test_no_substring_false_positive(self):
        """'Apple' should NOT match 'Pineapple'."""
        from job_finder.web.pipeline_detector import _company_in_email

        result = _company_in_email(
            "Apple",
            body="We work with Pineapple Corp on this initiative.",
            subject="Update from Pineapple",
        )
        assert result is False

    def test_case_insensitive_match(self):
        """Company must match in subject or sender (case-insensitive)."""
        from job_finder.web.pipeline_detector import _company_in_email

        # Subject has BETTERHELP (upper); body is irrelevant under tightened rules.
        result = _company_in_email(
            "BetterHelp",
            body="",
            subject="Application update from BETTERHELP",
        )
        assert result is True

    def test_no_match_unrelated_company(self):
        from job_finder.web.pipeline_detector import _company_in_email

        result = _company_in_email(
            "Stripe",
            body="We regret to inform you that we are going in a different direction.",
            subject="Application update",
        )
        assert result is False

    def test_multi_word_company_requires_all_sig_words(self):
        """'Alameda County' should NOT match email mentioning only 'county'."""
        from job_finder.web.pipeline_detector import _company_in_email

        result = _company_in_email(
            "Alameda County",
            body="The county has reviewed your application.",
            subject="County position update",
        )
        assert result is False

    def test_multi_word_company_matches_when_all_present(self):
        """Distinctive token match in subject."""
        from job_finder.web.pipeline_detector import _company_in_email

        result = _company_in_email(
            "Alameda County",
            body="",
            subject="Thank you for your interest in Alameda County.",
        )
        assert result is True


# ---------------------------------------------------------------------------
# Unit tests: _title_in_email
# ---------------------------------------------------------------------------


class TestTitleInEmail:
    """Tests for _title_in_email(title, subject, body)."""

    def test_single_sig_word_matches(self):
        """Title with 1 significant word: that word must appear."""
        from job_finder.web.pipeline_detector import _title_in_email

        # "Senior Data Scientist" -> sig words: ["scientist"] (senior, data are stop words)
        assert _title_in_email("Senior Data Scientist", "Scientist role", "") is True

    def test_single_sig_word_no_match(self):
        from job_finder.web.pipeline_detector import _title_in_email

        assert _title_in_email("Senior Data Scientist", "Manager role", "") is False

    def test_multi_sig_words_requires_two_matches(self):
        """Title with 3 sig words needs 2+ to match."""
        from job_finder.web.pipeline_detector import _title_in_email

        # "Information Systems Manager" -> sig words: ["information", "systems", "manager"]
        # Only "manager" in text -> 1 match < 2 required -> False
        assert (
            _title_in_email(
                "Information Systems Manager",
                "Manager position available",
                "",
            )
            is False
        )

    def test_multi_sig_words_two_matches_sufficient(self):
        from job_finder.web.pipeline_detector import _title_in_email

        assert (
            _title_in_email(
                "Information Systems Manager",
                "Information Systems role",
                "",
            )
            is True
        )

    def test_multi_sig_words_all_match(self):
        from job_finder.web.pipeline_detector import _title_in_email

        assert (
            _title_in_email(
                "Software Engineer",
                "Software Engineer position",
                "",
            )
            is True
        )


# ---------------------------------------------------------------------------
# Unit tests: _sender_is_ats
# ---------------------------------------------------------------------------


class TestSenderIsAts:
    """Tests for _sender_is_ats(from_address)."""

    def test_greenhouse_sender(self):
        from job_finder.web.pipeline_detector import _sender_is_ats

        assert _sender_is_ats("no-reply@greenhouse.io") is True

    def test_lever_sender(self):
        from job_finder.web.pipeline_detector import _sender_is_ats

        assert _sender_is_ats("noreply@lever.co") is True

    def test_ashby_sender(self):
        from job_finder.web.pipeline_detector import _sender_is_ats

        assert _sender_is_ats("no-reply@ashbyhq.com") is True

    def test_subdomain_ats_match(self):
        from job_finder.web.pipeline_detector import _sender_is_ats

        assert _sender_is_ats("jobs@mail.greenhouse.io") is True

    def test_with_name_and_brackets(self):
        from job_finder.web.pipeline_detector import _sender_is_ats

        assert _sender_is_ats("Stripe Recruiting <noreply@greenhouse.io>") is True

    def test_gmail_not_ats(self):
        from job_finder.web.pipeline_detector import _sender_is_ats

        assert _sender_is_ats("recruiter@gmail.com") is False

    def test_yahoo_not_ats(self):
        from job_finder.web.pipeline_detector import _sender_is_ats

        assert _sender_is_ats("hr@yahoo.com") is False

    def test_empty_string(self):
        from job_finder.web.pipeline_detector import _sender_is_ats

        assert _sender_is_ats("") is False


# ---------------------------------------------------------------------------
# Unit tests: score_match
# ---------------------------------------------------------------------------


class TestScoreMatch:
    """Tests for score_match(email, job)."""

    def _make_job(
        self,
        company="Stripe",
        title="Senior Data Scientist",
        pipeline_status="reviewing",
        first_seen=None,
    ):
        """Return a minimal job dict for testing."""
        if first_seen is None:
            first_seen = (datetime.now() - timedelta(days=5)).isoformat()
        return {
            "dedup_key": f"{company.lower()}|{title.lower()}|remote",
            "company": company,
            "title": title,
            "pipeline_status": pipeline_status,
            "first_seen": first_seen,
        }

    def _make_email(
        self, subject="", body="", from_address="", date=None, detection_type="rejection"
    ):
        """Return a minimal email dict for testing."""
        if date is None:
            date = datetime.now().isoformat()
        return {
            "message_id": "test_msg_001",
            "subject": subject,
            "body": body,
            "from_address": from_address,
            "date": date,
            "detection_type": detection_type,
        }

    def test_three_signal_match_auto_update_threshold(self):
        from job_finder.web.pipeline_detector import score_match

        job = self._make_job(company="Stripe", title="Senior Data Scientist")
        email = self._make_email(
            subject="Update on your application at Stripe",
            body="Dear candidate, Stripe is not moving forward with your application. Senior Data Scientist position.",
            from_address="noreply@greenhouse.io",
            detection_type="rejection",
        )
        score, signals = score_match(email, job)
        assert score >= 3
        assert "company" in signals
        assert "ats_domain" in signals

    def test_one_signal_match_review_queue_threshold(self):
        from job_finder.web.pipeline_detector import score_match

        job = self._make_job(company="Stripe", title="Senior Data Scientist")
        email = self._make_email(
            subject="Update on your Stripe application",
            body="We reviewed your application to Stripe. Unfortunately we are going with other candidates.",
            from_address="recruiter@gmail.com",
            detection_type="rejection",
        )
        # Only company signal should match (no ATS domain, title might or might not match)
        score, signals = score_match(email, job)
        assert score >= 1
        assert "company" in signals

    def test_zero_signal_match_no_match(self):
        from job_finder.web.pipeline_detector import score_match

        job = self._make_job(company="Stripe", title="Senior Data Scientist")
        email = self._make_email(
            subject="Application update",
            body="We are not moving forward at this time.",
            from_address="recruiter@gmail.com",
            # old date far from job creation
            date=(datetime.now() - timedelta(days=120)).isoformat(),
            detection_type="rejection",
        )
        score, signals = score_match(email, job)
        assert score == 0
        assert signals == []

    def test_ats_domain_only_counts_with_detection_type(self):
        """ATS domain signal only counts if detection_type is not None (Pitfall 3)."""
        from job_finder.web.pipeline_detector import score_match

        job = self._make_job(company="Stripe", title="Senior Data Scientist")
        email = self._make_email(
            subject="New job alert",
            body="Check out these new jobs.",
            from_address="alerts@greenhouse.io",
            detection_type=None,  # Not classified as pipeline email
        )
        score, signals = score_match(email, job)
        assert "ats_domain" not in signals


# ---------------------------------------------------------------------------
# Unit tests: _extract_snippet
# ---------------------------------------------------------------------------


class TestExtractSnippet:
    """Tests for _extract_snippet(body, detection_type)."""

    def test_returns_relevant_sentence_for_rejection(self):
        from job_finder.web.pipeline_detector import _extract_snippet

        body = "Thank you for applying. Unfortunately, we are not moving forward with your application. We wish you all the best."
        snippet = _extract_snippet(body, "rejection")
        assert "unfortunately" in snippet.lower() or "not moving forward" in snippet.lower()

    def test_max_200_chars(self):
        from job_finder.web.pipeline_detector import _extract_snippet

        body = "Unfortunately " + "x" * 300 + " other candidates are a better fit."
        snippet = _extract_snippet(body, "rejection")
        assert len(snippet) <= 200

    def test_fallback_to_first_sentence(self):
        from job_finder.web.pipeline_detector import _extract_snippet

        body = "Dear candidate. We wanted to reach out about your application."
        snippet = _extract_snippet(body, "rejection")
        assert len(snippet) > 0
        assert len(snippet) <= 200

    def test_returns_empty_for_empty_body(self):
        from job_finder.web.pipeline_detector import _extract_snippet

        snippet = _extract_snippet("", "rejection")
        assert snippet == ""


# ---------------------------------------------------------------------------
# Integration tests: get_pending_detections and resolve_detection (db.py helpers)
# ---------------------------------------------------------------------------


class TestDbHelpers:
    """Integration tests for get_pending_detections() and resolve_detection()."""

    def test_get_pending_detections_returns_pending_only(self, migrated_db):
        from job_finder.db import get_pending_detections

        path, conn = migrated_db
        _insert_job(conn, "stripe|senior data scientist|remote", "Senior Data Scientist", "Stripe")
        now = datetime.now().isoformat()
        # Insert one pending and one confirmed detection
        conn.execute(
            """INSERT INTO pipeline_detections
               (gmail_message_id, detection_type, job_id, confidence_score,
                matched_signals, snippet, email_subject, email_from,
                email_date, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "msg_pending",
                "rejection",
                "stripe|senior data scientist|remote",
                2,
                '["company"]',
                "snippet",
                "subj",
                "from@gmail.com",
                now,
                "pending",
                now,
            ),
        )
        conn.execute(
            """INSERT INTO pipeline_detections
               (gmail_message_id, detection_type, job_id, confidence_score,
                matched_signals, snippet, email_subject, email_from,
                email_date, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "msg_confirmed",
                "rejection",
                "stripe|senior data scientist|remote",
                3,
                '["company","ats_domain","timing"]',
                "snippet",
                "subj",
                "from@greenhouse.io",
                now,
                "confirmed",
                now,
            ),
        )
        conn.commit()

        results = get_pending_detections(conn)
        assert len(results) == 1
        assert results[0]["gmail_message_id"] == "msg_pending"
        assert results[0]["status"] == "pending"

    def test_get_pending_detections_includes_job_details(self, migrated_db):
        from job_finder.db import get_pending_detections

        path, conn = migrated_db
        _insert_job(conn, "stripe|senior data scientist|remote", "Senior Data Scientist", "Stripe")
        now = datetime.now().isoformat()
        conn.execute(
            """INSERT INTO pipeline_detections
               (gmail_message_id, detection_type, job_id, confidence_score,
                matched_signals, snippet, email_subject, email_from,
                email_date, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "msg_001",
                "rejection",
                "stripe|senior data scientist|remote",
                2,
                '["company"]',
                "We are not moving forward.",
                "Update",
                "from@gmail.com",
                now,
                "pending",
                now,
            ),
        )
        conn.commit()

        results = get_pending_detections(conn)
        assert len(results) == 1
        r = results[0]
        assert "job_title" in r
        assert "job_company" in r
        assert r["job_title"] == "Senior Data Scientist"
        assert r["job_company"] == "Stripe"

    def test_resolve_detection_updates_status_confirmed(self, migrated_db):
        from job_finder.db import resolve_detection

        path, conn = migrated_db
        now = datetime.now().isoformat()
        conn.execute(
            """INSERT INTO pipeline_detections
               (gmail_message_id, detection_type, confidence_score,
                email_date, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("msg_resolve", "rejection", 2, now, "pending", now),
        )
        conn.commit()

        detection_id = conn.execute(
            "SELECT id FROM pipeline_detections WHERE gmail_message_id = 'msg_resolve'"
        ).fetchone()[0]

        resolve_detection(conn, detection_id, "confirmed")

        row = conn.execute(
            "SELECT status, resolved_at FROM pipeline_detections WHERE id = ?",
            (detection_id,),
        ).fetchone()
        assert row[0] == "confirmed"
        assert row[1] is not None  # resolved_at was set

    def test_resolve_detection_updates_status_dismissed(self, migrated_db):
        from job_finder.db import resolve_detection

        path, conn = migrated_db
        now = datetime.now().isoformat()
        conn.execute(
            """INSERT INTO pipeline_detections
               (gmail_message_id, detection_type, confidence_score,
                email_date, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("msg_dismiss", "interview", 1, now, "pending", now),
        )
        conn.commit()

        detection_id = conn.execute(
            "SELECT id FROM pipeline_detections WHERE gmail_message_id = 'msg_dismiss'"
        ).fetchone()[0]

        resolve_detection(conn, detection_id, "dismissed")

        row = conn.execute(
            "SELECT status FROM pipeline_detections WHERE id = ?",
            (detection_id,),
        ).fetchone()
        assert row[0] == "dismissed"


# ---------------------------------------------------------------------------
# Integration tests: _process_email (high-confidence and low-confidence)
# ---------------------------------------------------------------------------


class TestProcessEmail:
    """Integration tests for the full email processing flow."""

    def _make_email(
        self, message_id, subject, body, from_address, detection_type="rejection", date=None
    ):
        if date is None:
            date = datetime.now().isoformat()
        return {
            "message_id": message_id,
            "subject": subject,
            "body": body,
            "from_address": from_address,
            "date": date,
            "detection_type": detection_type,
        }

    def test_high_confidence_calls_update_pipeline_status(self, migrated_db):
        """3+ signal match should call update_pipeline_status with source='auto-detected'."""
        from job_finder.web.pipeline_detector import _process_email

        path, conn = migrated_db
        _insert_job(
            conn,
            "stripe|senior data scientist|remote",
            "Senior Data Scientist",
            "Stripe",
            pipeline_status="reviewing",
        )

        jobs = conn.execute("SELECT * FROM jobs").fetchall()
        jobs = [dict(j) for j in jobs]

        email = self._make_email(
            message_id="high_conf_001",
            subject="Stripe - Senior Data Scientist - Unfortunately",
            body="Dear candidate, Stripe has reviewed your Senior Data Scientist application. Unfortunately, we are not moving forward with other candidates at this time.",
            from_address="no-reply@greenhouse.io",
            detection_type="rejection",
        )

        result = _process_email(email, conn, jobs)

        # Job status should be updated to 'rejected'
        row = conn.execute(
            "SELECT pipeline_status FROM jobs WHERE dedup_key = ?",
            ("stripe|senior data scientist|remote",),
        ).fetchone()
        assert row[0] == "rejected"

        # pipeline_events should have an auto-detected entry
        event = conn.execute(
            "SELECT * FROM pipeline_events WHERE job_id = ? AND source = 'auto-detected'",
            ("stripe|senior data scientist|remote",),
        ).fetchone()
        assert event is not None

        # pipeline_detections should have an auto-applied record
        detection = conn.execute(
            "SELECT * FROM pipeline_detections WHERE gmail_message_id = 'high_conf_001'"
        ).fetchone()
        assert detection is not None
        assert detection["status"] == "auto-applied"

    def test_low_confidence_inserts_pending_detection(self, migrated_db):
        """1-2 signal match should insert pending detection, NOT update status."""
        from job_finder.web.pipeline_detector import _process_email

        path, conn = migrated_db
        _insert_job(
            conn,
            "thumbtack|senior data scientist|remote",
            "Senior Data Scientist",
            "Thumbtack",
            pipeline_status="reviewing",
        )

        jobs = conn.execute("SELECT * FROM jobs").fetchall()
        jobs = [dict(j) for j in jobs]

        # Company in subject (passes new attribution gate); body keeps the
        # rejection keyword. No ATS sender, old timing -> score = 1 ("company"
        # only) -> pending detection.
        email = self._make_email(
            message_id="low_conf_001",
            subject="Thumbtack - application update",
            body="Unfortunately, we are not moving forward.",
            from_address="hr@gmail.com",
            detection_type="rejection",
        )

        _process_email(email, conn, jobs)

        # Job status should NOT be changed
        row = conn.execute(
            "SELECT pipeline_status FROM jobs WHERE dedup_key = ?",
            ("thumbtack|senior data scientist|remote",),
        ).fetchone()
        assert row[0] == "reviewing"

        # pipeline_detections should have a pending record
        detection = conn.execute(
            "SELECT * FROM pipeline_detections WHERE gmail_message_id = 'low_conf_001'"
        ).fetchone()
        assert detection is not None
        assert detection["status"] == "pending"

    def test_message_dedup_prevents_reprocessing(self, migrated_db):
        """Same gmail_message_id should not be processed twice."""
        from job_finder.web.pipeline_detector import _process_email

        path, conn = migrated_db
        _insert_job(
            conn,
            "betterhelp|data scientist|remote",
            "Data Scientist",
            "BetterHelp",
            pipeline_status="reviewing",
        )

        jobs = conn.execute("SELECT * FROM jobs").fetchall()
        jobs = [dict(j) for j in jobs]

        email = self._make_email(
            message_id="dedup_test_001",
            subject="BetterHelp - Update",
            body="BetterHelp is not moving forward with your application.",
            from_address="hr@gmail.com",
            detection_type="rejection",
        )

        # Process twice
        _process_email(email, conn, jobs)
        _process_email(email, conn, jobs)

        # email_parse_log should have exactly one entry for this message
        count = conn.execute(
            "SELECT COUNT(*) FROM email_parse_log WHERE message_id = 'dedup_test_001'"
        ).fetchone()[0]
        assert count == 1

        # pipeline_detections should have exactly one entry
        count = conn.execute(
            "SELECT COUNT(*) FROM pipeline_detections WHERE gmail_message_id = 'dedup_test_001'"
        ).fetchone()[0]
        assert count == 1

    def test_zero_signal_email_silently_dropped(self, migrated_db):
        """0-signal email should not create any detections."""
        from job_finder.web.pipeline_detector import _process_email

        path, conn = migrated_db
        _insert_job(
            conn,
            "stripe|senior data scientist|remote",
            "Senior Data Scientist",
            "Stripe",
            pipeline_status="reviewing",
        )

        jobs = conn.execute("SELECT * FROM jobs").fetchall()
        jobs = [dict(j) for j in jobs]

        email = self._make_email(
            message_id="zero_signal_001",
            subject="Rejection",
            body="We are not moving forward at this time.",
            from_address="hr@gmail.com",
            # Old date: far from job first_seen
            date=(datetime.now() - timedelta(days=120)).isoformat(),
            detection_type="rejection",
        )

        _process_email(email, conn, jobs)

        # No detection record should be inserted (score was 0)
        count = conn.execute(
            "SELECT COUNT(*) FROM pipeline_detections WHERE gmail_message_id = 'zero_signal_001'"
        ).fetchone()[0]
        assert count == 0

    def test_no_company_match_skipped_even_with_other_signals(self, migrated_db):
        """Email with title/timing signals but no company match should be skipped."""
        from job_finder.web.pipeline_detector import _process_email

        path, conn = migrated_db
        _insert_job(
            conn,
            "acme|software engineer|remote",
            "Software Engineer",
            "Acme Corp",
            pipeline_status="applied",
        )

        jobs = [dict(j) for j in conn.execute("SELECT * FROM jobs").fetchall()]

        # gmail.com is in PERSONAL_EMAIL_DOMAINS so the off-platform
        # fallback can't extract a company either -> truly skipped.
        email = self._make_email(
            message_id="no_company_001",
            subject="Phone screen - Information Systems Manager",
            body="We'd like to schedule a phone screen for the Information Systems Manager role with Alameda County.",
            from_address="recruiter@gmail.com",
            detection_type="interview",
        )

        result = _process_email(email, conn, jobs)
        assert result == "skipped"

        count = conn.execute(
            "SELECT COUNT(*) FROM pipeline_detections WHERE gmail_message_id = 'no_company_001'"
        ).fetchone()[0]
        assert count == 0

    def test_none_detection_type_skipped(self, migrated_db):
        """Emails with None detection_type (unclassified) should be skipped."""
        from job_finder.web.pipeline_detector import _process_email

        path, conn = migrated_db
        _insert_job(
            conn,
            "stripe|senior data scientist|remote",
            "Senior Data Scientist",
            "Stripe",
            pipeline_status="reviewing",
        )

        jobs = conn.execute("SELECT * FROM jobs").fetchall()
        jobs = [dict(j) for j in jobs]

        email = self._make_email(
            message_id="unclassified_001",
            subject="Newsletter",
            body="Top jobs this week!",
            from_address="newsletter@jobsite.com",
            detection_type=None,
        )

        _process_email(email, conn, jobs)

        count = conn.execute(
            "SELECT COUNT(*) FROM pipeline_detections WHERE gmail_message_id = 'unclassified_001'"
        ).fetchone()[0]
        assert count == 0


# ---------------------------------------------------------------------------
# Edge-case tests: score_match, _company_in_email, _title_in_email (DEBT-06)
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Edge cases for scoring and matching helpers — prevents false-positive auto-status updates.

    Note: test_no_substring_false_positive already exists in TestCompanyInEmail and
    covers the 'Apple' vs 'Pineapple' case. These tests cover additional null/empty
    and boundary scenarios that were missing coverage.
    """

    def test_company_in_email_null_company_returns_false(self):
        """_company_in_email(None, ...) should return False, not raise AttributeError."""
        from job_finder.web.pipeline_detector import _company_in_email

        result = _company_in_email(
            None,
            body="Dear candidate, Stripe has reviewed your application.",
            subject="Update from Stripe",
        )
        assert result is False

    def test_company_in_email_empty_company_returns_false(self):
        """_company_in_email('', ...) should return False without matching everything."""
        from job_finder.web.pipeline_detector import _company_in_email

        result = _company_in_email(
            "",
            body="Dear candidate, Stripe has reviewed your application.",
            subject="Update from Stripe",
        )
        assert result is False

    def test_title_in_email_empty_title_returns_false(self):
        """_title_in_email('', ...) should return False, not crash."""
        from job_finder.web.pipeline_detector import _title_in_email

        result = _title_in_email(
            "",
            subject="Senior Data Scientist role",
            body="We have reviewed your application for the Senior Data Scientist position.",
        )
        assert result is False

    def test_score_match_null_first_seen_no_crash(self):
        """score_match() with job first_seen=None should return valid (score, signals) tuple."""
        from job_finder.web.pipeline_detector import score_match

        job = {
            "dedup_key": "stripe|senior-data-scientist|remote",
            "company": "Stripe",
            "title": "Senior Data Scientist",
            "pipeline_status": "reviewing",
            "first_seen": None,
        }
        email = {
            "message_id": "edge_null_first_seen_001",
            "subject": "Update on your application at Stripe",
            "body": "Stripe has reviewed your application.",
            "from_address": "recruiter@gmail.com",
            "date": datetime.now().isoformat(),
            "detection_type": "rejection",
        }

        score, signals = score_match(email, job)

        # Should not raise; score and signals are valid types
        assert isinstance(score, int)
        assert isinstance(signals, list)
        # Company signal should still match even with null first_seen
        assert "company" in signals
        # Timing signal should NOT fire when first_seen is None
        assert "timing" not in signals

    def test_score_match_overlapping_timing(self):
        """score_match() with email date within 2 hours of first_seen gets timing signal."""
        from job_finder.web.pipeline_detector import score_match

        # Email arrived 90 minutes after job was first seen — well within 60-day window
        first_seen = datetime.now() - timedelta(hours=2)
        email_date = datetime.now() - timedelta(minutes=30)

        job = {
            "dedup_key": "acme|engineer|remote",
            "company": "Acme Corp",
            "title": "Software Engineer",
            "pipeline_status": "reviewing",
            "first_seen": first_seen.isoformat(),
        }
        email = {
            "message_id": "edge_timing_001",
            "subject": "Application update",
            "body": "We have reviewed your application.",
            "from_address": "hr@gmail.com",
            "date": email_date.isoformat(),
            "detection_type": "rejection",
        }

        score, signals = score_match(email, job)

        # Timing signal should fire — email is within 60-day window
        assert "timing" in signals
        # Score should count the timing signal exactly once (not double-counted)
        timing_count = signals.count("timing")
        assert timing_count == 1

    def test_score_match_unclassified_ats_signal(self):
        """score_match() with ATS sender but detection_type=None should not include ats_domain."""
        from job_finder.web.pipeline_detector import score_match

        job = {
            "dedup_key": "stripe|senior-data-scientist|remote",
            "company": "Stripe",
            "title": "Senior Data Scientist",
            "pipeline_status": "reviewing",
            "first_seen": (datetime.now() - timedelta(days=5)).isoformat(),
        }
        email = {
            "message_id": "edge_unclassified_ats_001",
            "subject": "New job alert from Stripe",
            "body": "Check out new Stripe roles.",
            "from_address": "alerts@greenhouse.io",
            "date": datetime.now().isoformat(),
            "detection_type": None,  # Unclassified — not a pipeline email
        }

        score, signals = score_match(email, job)

        # ATS domain should NOT contribute to score when detection_type is None
        assert "ats_domain" not in signals


# ---------------------------------------------------------------------------
# Tests: update_pipeline_status duplicate event guard
# ---------------------------------------------------------------------------


class TestUpdatePipelineStatusDedup:
    """Test that update_pipeline_status skips event insertion if status already matches."""

    def test_same_status_does_not_create_duplicate_event(self, migrated_db):
        """Calling update_pipeline_status twice with same status inserts only one pipeline_events row."""
        from job_finder.db import update_pipeline_status

        path, conn = migrated_db
        _insert_job(
            conn,
            "stripe|senior-data-scientist|remote",
            "Senior Data Scientist",
            "Stripe",
            pipeline_status="reviewing",
        )

        # First call transitions from reviewing -> rejected
        update_pipeline_status(
            conn, "stripe|senior-data-scientist|remote", "rejected", source="auto-detected"
        )

        # Second call with same status should be a no-op
        update_pipeline_status(
            conn, "stripe|senior-data-scientist|remote", "rejected", source="auto-detected"
        )

        # Only one pipeline_events row should exist
        count = conn.execute(
            "SELECT COUNT(*) FROM pipeline_events WHERE job_id = 'stripe|senior-data-scientist|remote'"
        ).fetchone()[0]
        assert count == 1

    def test_different_status_creates_new_event(self, migrated_db):
        """Calling update_pipeline_status with a different status creates a new event."""
        from job_finder.db import update_pipeline_status

        path, conn = migrated_db
        _insert_job(
            conn,
            "stripe|senior-data-scientist|remote",
            "Senior Data Scientist",
            "Stripe",
            pipeline_status="reviewing",
        )

        # Transition reviewing -> applied
        update_pipeline_status(
            conn, "stripe|senior-data-scientist|remote", "applied", source="manual"
        )

        # Transition applied -> rejected (different status)
        update_pipeline_status(
            conn, "stripe|senior-data-scientist|remote", "rejected", source="auto-detected"
        )

        # Two events should exist (one per unique transition)
        count = conn.execute(
            "SELECT COUNT(*) FROM pipeline_events WHERE job_id = 'stripe|senior-data-scientist|remote'"
        ).fetchone()[0]
        assert count == 2

    def test_same_status_does_not_change_job_status(self, migrated_db):
        """Calling update_pipeline_status with same status leaves jobs.pipeline_status unchanged."""
        from job_finder.db import update_pipeline_status

        path, conn = migrated_db
        _insert_job(
            conn,
            "stripe|senior-data-scientist|remote",
            "Senior Data Scientist",
            "Stripe",
            pipeline_status="rejected",
        )

        # Already at rejected — calling again should be no-op
        update_pipeline_status(
            conn, "stripe|senior-data-scientist|remote", "rejected", source="auto-detected"
        )

        row = conn.execute(
            "SELECT pipeline_status FROM jobs WHERE dedup_key = 'stripe|senior-data-scientist|remote'"
        ).fetchone()
        assert row["pipeline_status"] == "rejected"

        # No events should have been created
        count = conn.execute(
            "SELECT COUNT(*) FROM pipeline_events WHERE job_id = 'stripe|senior-data-scientist|remote'"
        ).fetchone()[0]
        assert count == 0


# ---------------------------------------------------------------------------
# Regression tests: the false-positive patterns from the 2026-05-26 audit.
# Each case is a real example from the production DB. The fix is the
# tighter `_company_in_email` (subject + sender attribution only, with
# distinctive-token filter that drops COMPANY_STOP_WORDS).
# ---------------------------------------------------------------------------


class TestCompanyMatcherFalsePositiveRegressions:
    """Each name documents the company that was incorrectly auto-promoted."""

    def test_meru_health_not_matched_by_midi_health_email(self):
        """`Meru Health` reduced to single sig-word 'health' under old rules."""
        from job_finder.web.pipeline_detector import _company_in_email

        assert (
            _company_in_email(
                "Meru Health",
                body="Greenhouse interview reminder body text",
                subject="Reminder for interview with Midi Health for the Senior role",
                from_address="Greenhouse <no-reply@greenhouse.io>",
            )
            is False
        )

    def test_cvs_health_not_matched_by_midi_health_email(self):
        from job_finder.web.pipeline_detector import _company_in_email

        assert (
            _company_in_email(
                "CVS Health",
                body="",
                subject="Midi Health Interview Confirmation - Zoom Link Enclosed",
                from_address="Patricia Stark <patricia.stark@midihealth.com>",
            )
            is False
        )

    def test_john_muir_health_not_matched_by_midi_health(self):
        from job_finder.web.pipeline_detector import _company_in_email

        assert (
            _company_in_email(
                "John Muir Health",
                body="",
                subject="Next Steps with Midi Health - Interview Availability",
                from_address="Patricia Stark <patricia.stark@midihealth.com>",
            )
            is False
        )

    def test_relx_inc_company_not_matched_by_hinge_health_email(self):
        """`RELX Inc. Company` reduced to single sig-word 'company' under old rules."""
        from job_finder.web.pipeline_detector import _company_in_email

        assert (
            _company_in_email(
                "RELX Inc. Company",
                body="",
                subject="Samuel, Thank you for applying to Hinge Health!",
                from_address="Hinge Health Hiring Team <no-reply@greenhouse.io>",
            )
            is False
        )

    def test_eqt_corporation_not_matched_by_mozilla_email(self):
        """`EQT Corporation` reduces to 'corporation' (stop-word) under old rules."""
        from job_finder.web.pipeline_detector import _company_in_email

        assert (
            _company_in_email(
                "EQT Corporation",
                body="",
                subject="Thank you for applying to Mozilla!",
                from_address="no-reply@us.greenhouse-mail.io",
            )
            is False
        )

    def test_us_tech_solutions_not_matched_by_cadence_email(self):
        """`US Tech Solutions` reduces to 'solutions' (stop-word) under new rules."""
        from job_finder.web.pipeline_detector import _company_in_email

        assert (
            _company_in_email(
                "US Tech Solutions",
                body="",
                subject="Next steps for the Data Analytics Lead role at Cadence",
                from_address="Rachel Oh <rachel.oh@cadencerp.com>",
            )
            is False
        )

    def test_target_not_matched_by_gitlab_interview(self):
        """`Target` is short and not in subject — body 'target start date' is ignored."""
        from job_finder.web.pipeline_detector import _company_in_email

        assert (
            _company_in_email(
                "Target",
                body="Please pick a target start date for the interview.",
                subject="Your interview with GitLab is scheduled for Apr 28",
                from_address="GitLab <no-reply@interviews.modernloop.io>",
            )
            is False
        )

    def test_youtube_not_matched_by_okta_application(self):
        """Marketing footer 'follow us on YouTube' must not attribute."""
        from job_finder.web.pipeline_detector import _company_in_email

        assert (
            _company_in_email(
                "YouTube",
                body="Follow us on YouTube, LinkedIn, Twitter for updates.",
                subject="Thank you for applying to Okta!",
                from_address="no-reply@okta.com",
            )
            is False
        )

    def test_apple_not_matched_by_ironclad_application(self):
        """Body mentions like 'Apple stock options' must not attribute."""
        from job_finder.web.pipeline_detector import _company_in_email

        assert (
            _company_in_email(
                "Apple",
                body="Apple stock options as part of our benefits package.",
                subject="Ironclad | Samuel, we received your application!",
                from_address="Ironclad Hiring Team <no-reply@ashbyhq.com>",
            )
            is False
        )


class TestCompanyMatcherTruePositiveRegressions:
    """Confirm the tightened matcher still accepts every observed legit case."""

    def test_hinge_health_matched_by_own_email(self):
        from job_finder.web.pipeline_detector import _company_in_email

        assert (
            _company_in_email(
                "Hinge Health",
                body="",
                subject="Samuel, Thank you for applying to Hinge Health!",
                from_address="Hinge Health Hiring Team <no-reply@greenhouse.io>",
            )
            is True
        )

    def test_gitlab_matched_by_own_email(self):
        from job_finder.web.pipeline_detector import _company_in_email

        assert (
            _company_in_email(
                "GitLab",
                body="",
                subject="Thank you for applying to GitLab",
                from_address="no-reply@us.greenhouse-mail.io",
            )
            is True
        )

    def test_scale_matched_by_own_email(self):
        from job_finder.web.pipeline_detector import _company_in_email

        assert (
            _company_in_email(
                "Scale",
                body="",
                subject="Samuel, Phone Interview with Scale!",
                from_address="Bryce Knox <bryce.knox@scale.com>",
            )
            is True
        )

    def test_doximity_matched_by_own_email(self):
        from job_finder.web.pipeline_detector import _company_in_email

        assert (
            _company_in_email(
                "Doximity",
                body="",
                subject="Doximity Next Steps",
                from_address="Tiffany Nguyen <tinguyen@doximity.com>",
            )
            is True
        )

    def test_company_matched_via_sender_when_subject_is_generic(self):
        """Subject lacks company; sender domain has it."""
        from job_finder.web.pipeline_detector import _company_in_email

        assert (
            _company_in_email(
                "Anthropic",
                body="",
                subject="Your application has been received",
                from_address="careers@anthropic.com",
            )
            is True
        )


class TestAutoApplyThreshold:
    """The new threshold: score>=4 OR (score>=3 AND 'ats_domain')."""

    def _make_processed_email(self, subject, body, from_address, detection_type):
        return {
            "message_id": "thresh_test_001",
            "subject": subject,
            "body": body,
            "from_address": from_address,
            "date": datetime.now().isoformat() + "Z",
            "detection_type": detection_type,
        }

    def _insert_job(self, conn, dedup_key, title, company, status="discovered"):
        conn.execute(
            "INSERT INTO jobs (dedup_key, title, company, location, sources, "
            "source_urls, salary_min, salary_max, description, first_seen, "
            "last_seen, pipeline_status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                dedup_key,
                title,
                company,
                "Remote",
                '["linkedin"]',
                '["https://x"]',
                None,
                None,
                "",
                datetime.now().isoformat(),
                datetime.now().isoformat(),
                status,
            ),
        )
        conn.commit()

    def test_score_3_without_corroborator_is_pending_not_auto(self, migrated_db):
        """The exact FP pattern: company+title+timing but no ATS / sender-co — must be pending."""
        from job_finder.web.pipeline_detector import _process_email

        _, conn = migrated_db
        self._insert_job(conn, "acme|senior data analyst|remote", "Senior Data Analyst", "Acme")
        jobs = [dict(j) for j in conn.execute("SELECT * FROM jobs").fetchall()]

        # Acme in subject + "analyst" in subject + recent timing.
        # Sender is an Otter.ai meeting summary (not an ATS, not Acme's domain).
        email = self._make_processed_email(
            subject="Acme Next Steps for Senior Analyst role",
            body="We'd like to schedule a phone screen.",
            from_address="Samuel Martin via Otter.ai <no-reply@otter.ai>",
            detection_type="interview",
        )
        result = _process_email(email, conn, jobs)
        assert result == "queued", "score=3 without any corroborator must be pending"
        status = conn.execute(
            "SELECT pipeline_status FROM jobs WHERE dedup_key=?",
            ("acme|senior data analyst|remote",),
        ).fetchone()[0]
        assert status == "discovered", "Job must NOT have been auto-promoted"

    def test_score_3_with_sender_company_is_auto(self, migrated_db):
        """Company emailing from its own domain (no-reply@waymo.com) auto-applies."""
        from job_finder.web.pipeline_detector import _process_email

        _, conn = migrated_db
        self._insert_job(
            conn, "waymo|sr product data scientist|remote", "Sr Product Data Scientist", "Waymo"
        )
        jobs = [dict(j) for j in conn.execute("SELECT * FROM jobs").fetchall()]

        email = self._make_processed_email(
            subject="Thank You for Applying to Waymo!",
            body="",
            from_address="no-reply@waymo.com",
            detection_type="confirmation",
        )
        result = _process_email(email, conn, jobs)
        assert result == "auto_updated"
        status = conn.execute(
            "SELECT pipeline_status FROM jobs WHERE dedup_key=?",
            ("waymo|sr product data scientist|remote",),
        ).fetchone()[0]
        assert status == "applied"

    def test_score_3_with_ats_domain_is_auto(self, migrated_db):
        """ATS sender provides the corroborating unfakeable signal -> auto-apply allowed."""
        from job_finder.web.pipeline_detector import _process_email

        _, conn = migrated_db
        self._insert_job(conn, "scale|analytics lead|remote", "Analytics Lead", "Scale")
        jobs = [dict(j) for j in conn.execute("SELECT * FROM jobs").fetchall()]

        # Scale in subject + "analytics" in subject + recent + ats sender (greenhouse).
        email = self._make_processed_email(
            subject="Scale - Analytics Lead role next steps",
            body="",
            from_address="no-reply@greenhouse.io",
            detection_type="interview",
        )
        result = _process_email(email, conn, jobs)
        assert result == "auto_updated"
        status = conn.execute(
            "SELECT pipeline_status FROM jobs WHERE dedup_key=?",
            ("scale|analytics lead|remote",),
        ).fetchone()[0]
        assert status == "phone_screen"


class TestSenderMatchesCompany:
    """The new corroborator signal — sender domain belongs to the company."""

    def test_company_own_noreply_domain(self):
        from job_finder.web.pipeline_detector._signals import _sender_matches_company

        assert _sender_matches_company("no-reply@waymo.com", "Waymo") is True

    def test_company_with_stop_word_only_uses_distinctive_tokens(self):
        """`Hinge Health` -> distinctive=['hinge'] -> sender 'hingehealth.com' matches."""
        from job_finder.web.pipeline_detector._signals import _sender_matches_company

        assert (
            _sender_matches_company("Lily Fang <lily.fang@hingehealth.com>", "Hinge Health")
            is True
        )

    def test_personal_address_does_not_match(self):
        from job_finder.web.pipeline_detector._signals import _sender_matches_company

        assert _sender_matches_company("samuel.martin@gmail.com", "Waymo") is False

    def test_third_party_ats_does_not_match(self):
        """Greenhouse-mail.io is the ATS, not the company — must not corroborate."""
        from job_finder.web.pipeline_detector._signals import _sender_matches_company

        assert _sender_matches_company("no-reply@us.greenhouse-mail.io", "Anthropic") is False

    def test_company_with_all_stop_words_never_matches(self):
        """`EQT Corporation` -> distinctive=[] (eqt<4, corporation in stop set) -> never matches."""
        from job_finder.web.pipeline_detector._signals import _sender_matches_company

        # Even if EQT sent from eqt.com, distinctive set is empty so we fail closed.
        assert _sender_matches_company("careers@eqt.com", "EQT Corporation") is False

    def test_unrelated_domain_does_not_match(self):
        from job_finder.web.pipeline_detector._signals import _sender_matches_company

        assert _sender_matches_company("no-reply@okta.com", "YouTube") is False


class TestDismissedJobsExcludedFromCandidates:
    """Manual dismissal must be terminal — auto-detect cannot resurrect."""

    def test_dismissed_job_not_in_active_pool(self, migrated_db):
        from job_finder.web.pipeline_detector import _load_active_jobs

        _, conn = migrated_db
        now = datetime.now().isoformat()
        conn.execute(
            "INSERT INTO jobs (dedup_key, title, company, location, sources, "
            "source_urls, salary_min, salary_max, description, first_seen, "
            "last_seen, pipeline_status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "apple|data sci|remote",
                "Senior DS",
                "Apple",
                "Remote",
                '["linkedin"]',
                '["https://x"]',
                None,
                None,
                "",
                now,
                now,
                "dismissed",
            ),
        )
        conn.commit()

        active = _load_active_jobs(conn)
        assert all(j["dedup_key"] != "apple|data sci|remote" for j in active)

    def test_dismissed_in_inactive_statuses_constant(self):
        from job_finder.web.pipeline_detector import INACTIVE_STATUSES

        assert "dismissed" in INACTIVE_STATUSES


# ---------------------------------------------------------------------------
# Phase B Option 1: off-platform application capture
# ---------------------------------------------------------------------------


class TestExtractCompanyFromSender:
    """Tests for the sender-domain extraction heuristic."""

    def test_extracts_simple_company_domain(self):
        from job_finder.web.pipeline_detector._off_platform import (
            _extract_company_from_sender,
        )

        assert _extract_company_from_sender("no-reply@waymo.com") == "Waymo"

    def test_strips_subdomain_noise(self):
        from job_finder.web.pipeline_detector._off_platform import (
            _extract_company_from_sender,
        )

        # careers.acme.com → Acme
        assert _extract_company_from_sender("hr@careers.acme.com") == "Acme"

    def test_handles_two_label_public_suffix(self):
        from job_finder.web.pipeline_detector._off_platform import (
            _extract_company_from_sender,
        )

        # careers@acme.co.uk → Acme (NOT Co)
        assert _extract_company_from_sender("hr@careers.acme.co.uk") == "Acme"

    def test_rejects_ats_domain(self):
        from job_finder.web.pipeline_detector._off_platform import (
            _extract_company_from_sender,
        )

        # Greenhouse-routed thank-you doesn't identify a specific employer
        assert _extract_company_from_sender("no-reply@us.greenhouse-mail.io") is None
        assert _extract_company_from_sender("hr@ashbyhq.com") is None
        assert _extract_company_from_sender("notifications@greenhouse.io") is None

    def test_rejects_personal_email_service(self):
        from job_finder.web.pipeline_detector._off_platform import (
            _extract_company_from_sender,
        )

        assert _extract_company_from_sender("samuel@gmail.com") is None
        assert _extract_company_from_sender("user@yahoo.com") is None
        assert _extract_company_from_sender("user@proton.me") is None

    def test_rejects_scheduling_tool(self):
        from job_finder.web.pipeline_detector._off_platform import (
            _extract_company_from_sender,
        )

        assert _extract_company_from_sender("no-reply@otter.ai") is None
        assert _extract_company_from_sender("invites@calendly.com") is None
        # modernloop is already in ATS_DOMAINS — covered via that path
        assert _extract_company_from_sender("invites@modernloop.io") is None

    def test_handles_name_bracket_format(self):
        from job_finder.web.pipeline_detector._off_platform import (
            _extract_company_from_sender,
        )

        assert _extract_company_from_sender("Waymo Careers <no-reply@waymo.com>") == "Waymo"

    def test_returns_none_for_empty_or_unparseable(self):
        from job_finder.web.pipeline_detector._off_platform import (
            _extract_company_from_sender,
        )

        assert _extract_company_from_sender("") is None
        assert _extract_company_from_sender("no-at-sign") is None
        assert _extract_company_from_sender("@") is None

    def test_titlecases_single_token_compound(self):
        from job_finder.web.pipeline_detector._off_platform import (
            _extract_company_from_sender,
        )

        # hingehealth.com → Hingehealth (compound stays one token —
        # dedup pass normalises through whitespace to attribute to an
        # existing "Hinge Health" job)
        assert _extract_company_from_sender("hr@hingehealth.com") == "Hingehealth"


class TestNormalizeForDedup:
    """Tests for the dedup normalisation helper."""

    def test_strips_whitespace_for_match(self):
        from job_finder.web.pipeline_detector._off_platform import (
            _normalize_for_dedup,
        )

        assert _normalize_for_dedup("Hinge Health") == _normalize_for_dedup("Hingehealth")

    def test_strips_punctuation_for_match(self):
        from job_finder.web.pipeline_detector._off_platform import (
            _normalize_for_dedup,
        )

        assert _normalize_for_dedup("AT&T") == _normalize_for_dedup("at-t")

    def test_handles_empty_string(self):
        from job_finder.web.pipeline_detector._off_platform import (
            _normalize_for_dedup,
        )

        assert _normalize_for_dedup("") == ""

    def test_case_insensitive(self):
        from job_finder.web.pipeline_detector._off_platform import (
            _normalize_for_dedup,
        )

        assert _normalize_for_dedup("WAYMO") == _normalize_for_dedup("waymo")


class TestTryCreateStubJob:
    """Tests for _try_create_stub_job dedup + insertion logic."""

    def _make_email(self, from_address, subject="Thank you for applying"):
        return {
            "message_id": "off_msg_001",
            "subject": subject,
            "body": "We received your application.",
            "from_address": from_address,
            "date": datetime.now().isoformat(),
            "detection_type": "confirmation",
        }

    def test_creates_stub_when_no_existing_job(self, migrated_db):
        from job_finder.web.pipeline_detector._off_platform import (
            _try_create_stub_job,
        )

        path, conn = migrated_db
        email = self._make_email("hr@brand-new-co.com")

        stub = _try_create_stub_job(email, conn)

        assert stub is not None
        assert stub["company"] == "Brand-New-Co"
        assert stub["attributed_existing"] is False
        # Verify row landed in jobs
        row = conn.execute(
            "SELECT title, company, pipeline_status, jd_full, sources "
            "FROM jobs WHERE dedup_key = ?",
            (stub["dedup_key"],),
        ).fetchone()
        assert row is not None
        assert row["company"] == "Brand-New-Co"
        assert row["pipeline_status"] == "discovered"
        # jd_full=NULL is the trigger for enrichment_backfill pickup
        assert row["jd_full"] is None
        assert "off_platform_email" in row["sources"]

    def test_attributes_to_existing_job_by_normalised_company(self, migrated_db):
        from job_finder.web.pipeline_detector._off_platform import (
            _try_create_stub_job,
        )

        path, conn = migrated_db
        _insert_job(
            conn,
            "hinge|sde|remote",
            "Software Engineer",
            "Hinge Health",
            pipeline_status="reviewing",
        )

        # hingehealth.com → "Hingehealth", which normalises to "hingehealth"
        # which matches "Hinge Health" → "hingehealth"
        email = self._make_email("hr@hingehealth.com")
        stub = _try_create_stub_job(email, conn)

        assert stub is not None
        assert stub["dedup_key"] == "hinge|sde|remote"
        assert stub["attributed_existing"] is True
        # No duplicate inserted
        count = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE company = 'Hinge Health'"
        ).fetchone()[0]
        assert count == 1

    def test_returns_none_when_extraction_fails(self, migrated_db):
        from job_finder.web.pipeline_detector._off_platform import (
            _try_create_stub_job,
        )

        path, conn = migrated_db
        # ATS sender → can't attribute
        email = self._make_email("no-reply@greenhouse.io")

        stub = _try_create_stub_job(email, conn)

        assert stub is None
        # No row created
        count = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        assert count == 0


class TestProcessEmailOffPlatform:
    """Integration: end-to-end off-platform capture via _process_email."""

    def _make_email(
        self,
        message_id,
        subject,
        body,
        from_address,
        detection_type="confirmation",
        date=None,
    ):
        if date is None:
            date = datetime.now().isoformat()
        return {
            "message_id": message_id,
            "subject": subject,
            "body": body,
            "from_address": from_address,
            "date": date,
            "detection_type": detection_type,
        }

    def test_confirmation_with_unknown_company_creates_stub_and_applies(self, migrated_db):
        """Confirmation email from a company-domain sender we have no job for
        creates a stub job, marks it applied, writes pipeline_events +
        pipeline_detections, and returns auto_updated."""
        from job_finder.web.pipeline_detector import _process_email

        path, conn = migrated_db
        # No existing jobs at all
        jobs = []

        email = self._make_email(
            message_id="off_e2e_001",
            subject="Thank you for applying",
            body="We received your application and will be in touch.",
            from_address="no-reply@waymo.com",
            detection_type="confirmation",
        )

        result = _process_email(email, conn, jobs)
        assert result == "auto_updated"

        # Stub job exists
        stub = conn.execute(
            "SELECT dedup_key, company, pipeline_status FROM jobs WHERE company = 'Waymo'"
        ).fetchone()
        assert stub is not None
        assert stub["pipeline_status"] == "applied"

        # pipeline_events row with source='off-platform'
        event = conn.execute(
            "SELECT * FROM pipeline_events WHERE job_id = ? AND source = 'off-platform'",
            (stub["dedup_key"],),
        ).fetchone()
        assert event is not None

        # pipeline_detections row with off_platform_stub marker
        det = conn.execute(
            "SELECT matched_signals, status FROM pipeline_detections WHERE gmail_message_id = ?",
            ("off_e2e_001",),
        ).fetchone()
        assert det is not None
        assert "off_platform_stub" in det["matched_signals"]
        assert det["status"] == "auto-applied"

    def test_interview_email_creates_stub_and_moves_to_phone_screen(self, migrated_db):
        """Interview-type detection should land the stub in phone_screen,
        not applied."""
        from job_finder.web.pipeline_detector import _process_email

        path, conn = migrated_db

        email = self._make_email(
            message_id="off_e2e_002",
            subject="Phone screen invitation",
            body="We'd like to schedule a phone screen with you.",
            from_address="recruiting@anthropic.com",
            detection_type="interview",
        )

        result = _process_email(email, conn, [])
        assert result == "auto_updated"

        stub = conn.execute(
            "SELECT pipeline_status FROM jobs WHERE company = 'Anthropic'"
        ).fetchone()
        assert stub is not None
        assert stub["pipeline_status"] == "phone_screen"

    def test_rejection_with_unknown_company_does_NOT_create_stub(self, migrated_db):
        """Rejection emails are NOT used for off-platform stubbing — no
        value tracking applications we lost without ever seeing them."""
        from job_finder.web.pipeline_detector import _process_email

        path, conn = migrated_db

        email = self._make_email(
            message_id="off_e2e_rej",
            subject="Application update",
            body="Unfortunately, we are not moving forward.",
            from_address="hr@brand-new-co.com",
            detection_type="rejection",
        )

        result = _process_email(email, conn, [])
        assert result == "skipped"

        # No stub created
        count = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE company = 'Brand-New-Co'"
        ).fetchone()[0]
        assert count == 0

    def test_confirmation_from_ats_sender_falls_through_to_skipped(self, migrated_db):
        """Confirmation email from an ATS sender (no employer identity in
        domain) should still be skipped — Option 1's rules can't extract
        the company. Option 2 (LLM fallback) would handle this case."""
        from job_finder.web.pipeline_detector import _process_email

        path, conn = migrated_db

        email = self._make_email(
            message_id="off_e2e_ats",
            subject="Thank you for applying",
            body="We received your application.",
            from_address="no-reply@us.greenhouse-mail.io",
            detection_type="confirmation",
        )

        result = _process_email(email, conn, [])
        assert result == "skipped"

        count = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        assert count == 0

    def test_confirmation_dedups_to_existing_job(self, migrated_db):
        """Existing Hinge Health job + confirmation from hingehealth.com
        should attribute to existing job, not create a duplicate."""
        from job_finder.web.pipeline_detector import _process_email

        path, conn = migrated_db
        _insert_job(
            conn,
            "hingehealth|sde|remote",
            "Software Engineer",
            "Hinge Health",
            pipeline_status="reviewing",
        )

        email = self._make_email(
            message_id="off_e2e_dedup",
            subject="Thank you for applying",
            body="We received your application.",
            from_address="hr@hingehealth.com",
            detection_type="confirmation",
        )

        result = _process_email(email, conn, [])
        assert result == "auto_updated"

        # Existing job promoted to applied
        row = conn.execute(
            "SELECT pipeline_status FROM jobs WHERE dedup_key = ?",
            ("hingehealth|sde|remote",),
        ).fetchone()
        assert row["pipeline_status"] == "applied"

        # No duplicate created
        count = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE company LIKE '%inge%ealth%'"
        ).fetchone()[0]
        assert count == 1
