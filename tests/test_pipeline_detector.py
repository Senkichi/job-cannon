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
        from job_finder.web.pipeline_detector import _company_in_email

        result = _company_in_email(
            "BetterHelp",
            body="Thank you for your interest in BETTERHELP.",
            subject="Application update",
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
        from job_finder.web.pipeline_detector import _company_in_email

        result = _company_in_email(
            "Alameda County",
            body="Thank you for your interest in Alameda County.",
            subject="Update",
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

        # Only company signal: Thumbtack in body, but no ATS domain, old timing
        email = self._make_email(
            message_id="low_conf_001",
            subject="Application update",
            body="Thank you for your interest in Thumbtack. Unfortunately, we are not moving forward.",
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

        email = self._make_email(
            message_id="no_company_001",
            subject="Phone screen - Information Systems Manager",
            body="We'd like to schedule a phone screen for the Information Systems Manager role with Alameda County.",
            from_address="no-reply@governmentjobs.com",
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
