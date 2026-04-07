"""Tests for job_finder.web.notifier module.

Covers:
- send_notification daemon thread behavior
- Graceful fallback when win11toast is unavailable
- Toggle gating for each notification type
- URL construction for job detail and settings pages
- Label mapping for pipeline change types
- 24-hour notification cooldown guard (TestNotifierDedup)
"""

import threading
import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, call, patch


class TestSendNotification(unittest.TestCase):
    """Test send_notification fires in a daemon thread."""

    def test_fires_daemon_thread(self):
        """send_notification starts a daemon thread."""
        from job_finder.web.notifier import send_notification

        created_threads = []
        original_thread = threading.Thread

        def capture_thread(*args, **kwargs):
            t = original_thread(*args, **kwargs)
            created_threads.append(t)
            return t

        with patch("threading.Thread", side_effect=capture_thread) as mock_thread:
            send_notification("Test Title", "Test Body")
            mock_thread.assert_called_once()
            call_kwargs = mock_thread.call_args
            assert call_kwargs.kwargs.get("daemon") is True, "Thread must be daemon=True"

    def test_does_not_block_caller(self):
        """send_notification returns immediately without waiting for thread."""
        import time

        from job_finder.web.notifier import send_notification

        with patch("threading.Thread") as mock_thread:
            t_instance = MagicMock()
            mock_thread.return_value = t_instance
            start = time.time()
            send_notification("Title", "Body")
            elapsed = time.time() - start
            assert elapsed < 1.0, f"send_notification blocked for {elapsed:.2f}s"
            t_instance.start.assert_called_once()

    def test_passes_url_as_on_click(self):
        """send_notification passes url as on_click kwarg to toast."""
        import sys
        from job_finder.web.notifier import send_notification

        with patch("threading.Thread") as mock_thread:
            t_instance = MagicMock()
            mock_thread.return_value = t_instance

            send_notification("Title", "Body", url="http://localhost:5000/jobs/abc")

            mock_thread.assert_called_once()
            # Get the target function and invoke it to verify on_click is passed
            target_fn = mock_thread.call_args.kwargs["target"]

        # Call the target function with win11toast.toast mocked
        mock_toast = MagicMock()
        fake_win11toast = MagicMock()
        fake_win11toast.toast = mock_toast
        with patch.dict(sys.modules, {"win11toast": fake_win11toast}):
            target_fn()

        mock_toast.assert_called_once()
        _, toast_kwargs = mock_toast.call_args
        assert "on_click" in toast_kwargs, "toast must receive on_click kwarg when url is provided"
        assert toast_kwargs["on_click"] == "http://localhost:5000/jobs/abc"

    def test_no_url_omits_on_click(self):
        """send_notification without url does not pass on_click to toast."""
        import sys

        from job_finder.web.notifier import send_notification

        with patch("threading.Thread") as mock_thread:
            t_instance = MagicMock()
            mock_thread.return_value = t_instance
            send_notification("Title", "Body")  # no url
            target_fn = mock_thread.call_args.kwargs["target"]

        mock_toast = MagicMock()
        fake_win11toast = MagicMock()
        fake_win11toast.toast = mock_toast
        with patch.dict(sys.modules, {"win11toast": fake_win11toast}):
            target_fn()

        mock_toast.assert_called_once()
        _, toast_kwargs = mock_toast.call_args
        assert "on_click" not in toast_kwargs, "on_click must not be passed when url is None"


class TestFallbackGraceful(unittest.TestCase):
    """Test graceful fallback when win11toast is not importable."""

    def test_no_exception_on_import_error(self):
        """send_notification silently swallows ImportError from win11toast."""
        import sys
        from job_finder.web.notifier import send_notification

        # Simulate win11toast not being installed
        with patch.dict(sys.modules, {"win11toast": None}):
            # Should not raise any exception
            t = None

            with patch("threading.Thread") as mock_thread:
                # Create a real thread that will simulate the import failure
                captured_target = []

                def capture_thread_call(*args, **kwargs):
                    captured_target.append(kwargs.get("target"))
                    m = MagicMock()
                    m.start = MagicMock()
                    return m

                mock_thread.side_effect = capture_thread_call
                send_notification("Title", "Body")

            # Call the captured target directly to test it doesn't raise
            if captured_target and captured_target[0]:
                try:
                    captured_target[0]()
                except Exception as e:
                    self.fail(f"Thread target raised exception: {e}")

    def test_no_exception_on_toast_error(self):
        """send_notification silently swallows any exception from toast."""
        import sys

        from job_finder.web.notifier import send_notification

        captured_target = []

        def capture_thread_call(*args, **kwargs):
            captured_target.append(kwargs.get("target"))
            m = MagicMock()
            m.start = MagicMock()
            return m

        with patch("threading.Thread") as mock_thread:
            mock_thread.side_effect = capture_thread_call
            send_notification("Title", "Body")

        # Inject a mock win11toast whose toast() raises
        failing_module = MagicMock()
        failing_module.toast.side_effect = RuntimeError("toast crash!")
        assert captured_target and captured_target[0]
        with patch.dict(sys.modules, {"win11toast": failing_module}):
            try:
                captured_target[0]()
            except Exception as e:
                self.fail(f"Thread target must swallow exceptions, got: {e}")


class TestToggleGating(unittest.TestCase):
    """Test that each notification type respects its config toggle."""

    def setUp(self):
        """Reset cooldown state before each test to prevent cross-test pollution."""
        from job_finder.web import notifier
        notifier._NOTIFY_SEEN.clear()

    def test_notify_high_score_sends_when_enabled(self):
        """notify_high_score sends notification when high_score toggle is True."""
        from job_finder.web.notifier import notify_high_score

        config = {"notifications": {"high_score": True}}

        with patch("job_finder.web.notifier.send_notification") as mock_send:
            notify_high_score("Data Scientist", "Acme Corp", 82.0, "acme|data-scientist|sf", config)
            mock_send.assert_called_once()

    def test_notify_high_score_skips_when_disabled(self):
        """notify_high_score does NOT send when high_score toggle is False."""
        from job_finder.web.notifier import notify_high_score

        config = {"notifications": {"high_score": False}}

        with patch("job_finder.web.notifier.send_notification") as mock_send:
            notify_high_score("Data Scientist", "Acme Corp", 82.0, "acme|data-scientist|sf", config)
            mock_send.assert_not_called()

    def test_notify_high_score_default_enabled(self):
        """notify_high_score sends when notifications section absent (defaults to True)."""
        from job_finder.web.notifier import notify_high_score

        config = {}  # no notifications section

        with patch("job_finder.web.notifier.send_notification") as mock_send:
            notify_high_score("Data Scientist", "Acme Corp", 82.0, "acme|data-scientist|sf", config)
            mock_send.assert_called_once()

    def test_notify_pipeline_change_sends_when_enabled(self):
        """notify_pipeline_change sends when pipeline_change toggle is True."""
        from job_finder.web.notifier import notify_pipeline_change

        config = {"notifications": {"pipeline_change": True}}

        with patch("job_finder.web.notifier.send_notification") as mock_send:
            notify_pipeline_change("rejection", "Data Scientist", "Acme Corp", "acme|ds|sf", config)
            mock_send.assert_called_once()

    def test_notify_pipeline_change_skips_when_disabled(self):
        """notify_pipeline_change does NOT send when pipeline_change toggle is False."""
        from job_finder.web.notifier import notify_pipeline_change

        config = {"notifications": {"pipeline_change": False}}

        with patch("job_finder.web.notifier.send_notification") as mock_send:
            notify_pipeline_change("rejection", "Data Scientist", "Acme Corp", "acme|ds|sf", config)
            mock_send.assert_not_called()

    def test_notify_budget_alert_sends_when_enabled(self):
        """notify_budget_alert sends when budget_alert toggle is True."""
        from job_finder.web.notifier import notify_budget_alert

        config = {"notifications": {"budget_alert": True}}

        with patch("job_finder.web.notifier.send_notification") as mock_send:
            notify_budget_alert(85.0, config)
            mock_send.assert_called_once()

    def test_notify_budget_alert_skips_when_disabled(self):
        """notify_budget_alert does NOT send when budget_alert toggle is False."""
        from job_finder.web.notifier import notify_budget_alert

        config = {"notifications": {"budget_alert": False}}

        with patch("job_finder.web.notifier.send_notification") as mock_send:
            notify_budget_alert(85.0, config)
            mock_send.assert_not_called()


class TestHighScoreURL(unittest.TestCase):
    """Test URL construction for high-score notifications."""

    def setUp(self):
        """Reset cooldown state before each test."""
        from job_finder.web import notifier
        notifier._NOTIFY_SEEN.clear()

    def test_url_points_to_job_detail(self):
        """notify_high_score constructs URL pointing to job detail page."""
        from job_finder.web.notifier import notify_high_score

        config = {"notifications": {"high_score": True}}

        with patch("job_finder.web.notifier.send_notification") as mock_send:
            notify_high_score("Data Scientist", "Acme Corp", 82.0, "acme|data-scientist|sf", config)
            call_args = mock_send.call_args
            url = call_args.kwargs.get("url") or (call_args.args[2] if len(call_args.args) > 2 else None)
            assert url is not None, "URL must be provided"
            assert "/jobs/" in url

    def test_url_encodes_dedup_key(self):
        """notify_high_score URL-encodes special characters in dedup_key."""
        from job_finder.web.notifier import notify_high_score

        config = {"notifications": {"high_score": True}}
        # dedup_key with pipe characters (common in job dedup keys)
        dedup_key = "acme corp|data scientist|san francisco"

        with patch("job_finder.web.notifier.send_notification") as mock_send:
            notify_high_score("Data Scientist", "Acme Corp", 82.0, dedup_key, config)
            call_args = mock_send.call_args
            url = call_args.kwargs.get("url") or (call_args.args[2] if len(call_args.args) > 2 else None)
            assert url is not None
            # Pipe characters should be encoded
            assert "|" not in url.split("/jobs/")[1]

    def test_body_includes_score_and_company(self):
        """notify_high_score body includes score and company name."""
        from job_finder.web.notifier import notify_high_score

        config = {"notifications": {"high_score": True}}

        with patch("job_finder.web.notifier.send_notification") as mock_send:
            notify_high_score("Data Scientist", "Acme Corp", 82.0, "acme|ds|sf", config)
            call_args = mock_send.call_args
            body = call_args.args[1] if len(call_args.args) > 1 else call_args.kwargs.get("body", "")
            assert "Acme Corp" in body
            assert "82" in body  # score should appear


class TestBudgetAlertURL(unittest.TestCase):
    """Test URL construction for budget alert notifications."""

    def setUp(self):
        """Reset cooldown state before each test."""
        from job_finder.web import notifier
        notifier._NOTIFY_SEEN.clear()

    def test_url_points_to_settings(self):
        """notify_budget_alert constructs URL pointing to /settings page."""
        from job_finder.web.notifier import notify_budget_alert

        config = {"notifications": {"budget_alert": True}}

        with patch("job_finder.web.notifier.send_notification") as mock_send:
            notify_budget_alert(85.0, config)
            call_args = mock_send.call_args
            url = call_args.kwargs.get("url") or (call_args.args[2] if len(call_args.args) > 2 else None)
            assert url is not None, "URL must be provided"
            assert "/settings" in url

    def test_body_includes_percent(self):
        """notify_budget_alert body includes the percentage."""
        from job_finder.web.notifier import notify_budget_alert

        config = {"notifications": {"budget_alert": True}}

        with patch("job_finder.web.notifier.send_notification") as mock_send:
            notify_budget_alert(85.0, config)
            call_args = mock_send.call_args
            body = call_args.args[1] if len(call_args.args) > 1 else call_args.kwargs.get("body", "")
            assert "85" in body

    def test_body_distinguishes_80_and_100_percent(self):
        """notify_budget_alert body text differs for 80% vs 100% threshold."""
        from job_finder.web.notifier import notify_budget_alert

        config = {"notifications": {"budget_alert": True}}

        bodies = {}
        for pct in [80.0, 100.0]:
            with patch("job_finder.web.notifier.send_notification") as mock_send:
                notify_budget_alert(pct, config)
                call_args = mock_send.call_args
                bodies[pct] = call_args.args[1] if len(call_args.args) > 1 else call_args.kwargs.get("body", "")

        assert "80" in bodies[80.0]
        assert "100" in bodies[100.0]
        assert bodies[80.0] != bodies[100.0], "80% and 100% bodies must differ"


class TestPipelineChangeLabels(unittest.TestCase):
    """Test that detection_type maps to human-readable labels."""

    def setUp(self):
        """Reset cooldown state before each test."""
        from job_finder.web import notifier
        notifier._NOTIFY_SEEN.clear()

    def test_rejection_label(self):
        """rejection detection_type produces human-readable title."""
        from job_finder.web.notifier import notify_pipeline_change

        config = {"notifications": {"pipeline_change": True}}

        with patch("job_finder.web.notifier.send_notification") as mock_send:
            notify_pipeline_change("rejection", "Data Scientist", "Acme Corp", "acme|ds|sf", config)
            call_args = mock_send.call_args
            title = call_args.args[0] if call_args.args else call_args.kwargs.get("title", "")
            assert "Rejection" in title or "rejection" in title.lower()

    def test_interview_invite_label(self):
        """interview_invite detection_type produces human-readable title."""
        from job_finder.web.notifier import notify_pipeline_change

        config = {"notifications": {"pipeline_change": True}}

        with patch("job_finder.web.notifier.send_notification") as mock_send:
            notify_pipeline_change("interview_invite", "Data Scientist", "Acme Corp", "acme|ds|sf", config)
            call_args = mock_send.call_args
            title = call_args.args[0] if call_args.args else call_args.kwargs.get("title", "")
            assert "Interview" in title or "interview" in title.lower()

    def test_confirmation_label(self):
        """application_confirmation detection_type produces human-readable title."""
        from job_finder.web.notifier import notify_pipeline_change

        config = {"notifications": {"pipeline_change": True}}

        with patch("job_finder.web.notifier.send_notification") as mock_send:
            notify_pipeline_change("application_confirmation", "Data Scientist", "Acme Corp", "acme|ds|sf", config)
            call_args = mock_send.call_args
            title = call_args.args[0] if call_args.args else call_args.kwargs.get("title", "")
            assert "Application" in title or "Confirm" in title or "confirm" in title.lower()

    def test_offer_label(self):
        """offer detection_type produces human-readable title."""
        from job_finder.web.notifier import notify_pipeline_change

        config = {"notifications": {"pipeline_change": True}}

        with patch("job_finder.web.notifier.send_notification") as mock_send:
            notify_pipeline_change("offer", "Data Scientist", "Acme Corp", "acme|ds|sf", config)
            call_args = mock_send.call_args
            title = call_args.args[0] if call_args.args else call_args.kwargs.get("title", "")
            assert "Offer" in title or "offer" in title.lower()

    def test_unknown_type_uses_fallback(self):
        """Unknown detection_type falls back to title-cased version."""
        from job_finder.web.notifier import notify_pipeline_change

        config = {"notifications": {"pipeline_change": True}}

        with patch("job_finder.web.notifier.send_notification") as mock_send:
            notify_pipeline_change("custom_event", "Data Scientist", "Acme Corp", "acme|ds|sf", config)
            call_args = mock_send.call_args
            title = call_args.args[0] if call_args.args else call_args.kwargs.get("title", "")
            # Should not crash; title should contain something readable
            assert len(title) > 0

    def test_body_includes_job_and_company(self):
        """Pipeline change notification body includes job title and company."""
        from job_finder.web.notifier import notify_pipeline_change

        config = {"notifications": {"pipeline_change": True}}

        with patch("job_finder.web.notifier.send_notification") as mock_send:
            notify_pipeline_change("rejection", "Data Scientist", "Acme Corp", "acme|ds|sf", config)
            call_args = mock_send.call_args
            body = call_args.args[1] if len(call_args.args) > 1 else call_args.kwargs.get("body", "")
            assert "Acme Corp" in body
            assert "Data Scientist" in body

    def test_pipeline_url_points_to_job_detail(self):
        """Pipeline change notification URL points to job detail page."""
        from job_finder.web.notifier import notify_pipeline_change

        config = {"notifications": {"pipeline_change": True}}

        with patch("job_finder.web.notifier.send_notification") as mock_send:
            notify_pipeline_change("rejection", "Data Scientist", "Acme Corp", "acme|ds|sf", config)
            call_args = mock_send.call_args
            url = call_args.kwargs.get("url") or (call_args.args[2] if len(call_args.args) > 2 else None)
            assert url is not None
            assert "/jobs/" in url


class TestNotifierDedup:
    """Tests for 24-hour notification cooldown guard."""

    def setup_method(self):
        """Reset the module-level dedup state before each test."""
        from job_finder.web import notifier
        notifier._NOTIFY_SEEN.clear()

    def teardown_method(self):
        """Reset the module-level dedup state after each test to prevent leakage."""
        from job_finder.web import notifier
        notifier._NOTIFY_SEEN.clear()

    def test_can_notify_first_call_returns_true(self):
        """_can_notify returns True on the first call for a (key, type) pair."""
        from job_finder.web.notifier import _can_notify
        assert _can_notify("key1", "high_score") is True

    def test_can_notify_second_call_within_24h_returns_false(self):
        """_can_notify returns False on immediate second call (within 24h)."""
        from job_finder.web.notifier import _can_notify
        _can_notify("key1", "high_score")  # first call
        assert _can_notify("key1", "high_score") is False

    def test_can_notify_different_type_same_key_returns_true(self):
        """Different notification_type with same dedup_key is independent."""
        from job_finder.web.notifier import _can_notify
        _can_notify("key1", "high_score")  # fires for high_score
        # pipeline_change for same key should still be allowed
        assert _can_notify("key1", "pipeline_change") is True

    def test_can_notify_different_key_same_type_returns_true(self):
        """Different dedup_key with same notification_type is independent."""
        from job_finder.web.notifier import _can_notify
        _can_notify("key1", "high_score")  # fires for key1
        # key2 for same type should still be allowed
        assert _can_notify("key2", "high_score") is True

    def test_can_notify_after_24h_returns_true(self):
        """After 25 hours elapse, _can_notify returns True again."""
        from job_finder.web import notifier
        from job_finder.web.notifier import _can_notify

        # First call sets the timestamp
        _can_notify("key1", "high_score")

        # Manually set the timestamp to 25 hours ago
        cache_key = ("key1", "high_score")
        notifier._NOTIFY_SEEN[cache_key] = datetime.now() - timedelta(hours=25)

        assert _can_notify("key1", "high_score") is True

    def test_notify_high_score_dedup_blocks_second_send(self):
        """notify_high_score does NOT call send_notification on second call within 24h."""
        from job_finder.web.notifier import notify_high_score

        config = {"notifications": {"high_score": True}}

        with patch("job_finder.web.notifier.send_notification") as mock_send:
            notify_high_score("Data Scientist", "Acme", 82.0, "acme|ds|sf", config)
            notify_high_score("Data Scientist", "Acme", 82.0, "acme|ds|sf", config)
            # Second call should be blocked by cooldown
            assert mock_send.call_count == 1

    def test_notify_pipeline_change_dedup_blocks_second_send(self):
        """notify_pipeline_change does NOT call send_notification on second call within 24h."""
        from job_finder.web.notifier import notify_pipeline_change

        config = {"notifications": {"pipeline_change": True}}

        with patch("job_finder.web.notifier.send_notification") as mock_send:
            notify_pipeline_change("rejection", "Data Scientist", "Acme", "acme|ds|sf", config)
            notify_pipeline_change("rejection", "Data Scientist", "Acme", "acme|ds|sf", config)
            # Second call should be blocked by cooldown
            assert mock_send.call_count == 1

    def test_notify_budget_alert_not_affected_by_dedup(self):
        """notify_budget_alert is NOT affected by the cooldown guard."""
        from job_finder.web.notifier import notify_budget_alert

        config = {"notifications": {"budget_alert": True}}

        with patch("job_finder.web.notifier.send_notification") as mock_send:
            notify_budget_alert(85.0, config)
            notify_budget_alert(100.0, config)
            # Budget alert has no dedup guard — both calls should send
            assert mock_send.call_count == 2


class TestNotifierIntegration(unittest.TestCase):
    """Integration tests for thread daemon behavior."""

    def test_thread_daemon_flag_set(self):
        """Verify daemon=True is explicitly set on the thread."""
        from job_finder.web.notifier import send_notification

        created_daemon_values = []
        original_thread_init = threading.Thread.__init__

        def patched_init(self, *args, **kwargs):
            original_thread_init(self, *args, **kwargs)

        with patch("threading.Thread") as mock_thread:
            t_mock = MagicMock()
            mock_thread.return_value = t_mock
            send_notification("Title", "Body")
            call_kwargs = mock_thread.call_args.kwargs
            assert "daemon" in call_kwargs, "daemon kwarg must be passed to Thread"
            assert call_kwargs["daemon"] is True, "daemon must be True"
            t_mock.start.assert_called_once()
