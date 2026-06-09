"""Tests for persistent file logging (DEBT-07) and console-encoding fix (issue #234)."""

import io
import logging
import sys
from logging.handlers import RotatingFileHandler
from unittest.mock import MagicMock, patch


class TestFileLogging:
    """Verify RotatingFileHandler setup and idempotency guard."""

    def test_setup_file_logging_attaches_handler(self, tmp_path):
        """_setup_file_logging() directly attaches a RotatingFileHandler when called explicitly."""
        import os

        from job_finder.web import _setup_file_logging

        root = logging.getLogger()

        # Remove any existing RotatingFileHandlers to start clean
        existing = [h for h in root.handlers[:] if isinstance(h, RotatingFileHandler)]
        for h in existing:
            root.removeHandler(h)
            h.close()

        # Redirect the log file to tmp_path to avoid writing to production logs/
        orig_dir = os.getcwd()
        os.chdir(str(tmp_path))
        try:
            _setup_file_logging()
            has_rfh = any(isinstance(h, RotatingFileHandler) for h in root.handlers)
            assert has_rfh, "_setup_file_logging() did not attach a RotatingFileHandler"
        finally:
            # Clean up: remove the handler we just added
            for h in root.handlers[:]:
                if isinstance(h, RotatingFileHandler):
                    root.removeHandler(h)
                    h.close()
            os.chdir(orig_dir)

    def test_idempotency_guard_prevents_duplicate_handlers(self, tmp_path):
        """Calling _setup_file_logging() again does not add a second handler."""
        import os

        from job_finder.web import _setup_file_logging

        root = logging.getLogger()

        # Remove any existing RotatingFileHandlers to start clean
        existing = [h for h in root.handlers[:] if isinstance(h, RotatingFileHandler)]
        for h in existing:
            root.removeHandler(h)
            h.close()

        orig_dir = os.getcwd()
        os.chdir(str(tmp_path))
        try:
            _setup_file_logging()
            count_before = sum(1 for h in root.handlers if isinstance(h, RotatingFileHandler))
            _setup_file_logging()
            count_after = sum(1 for h in root.handlers if isinstance(h, RotatingFileHandler))
            assert count_after == count_before, (
                "Idempotency guard failed: duplicate handlers added"
            )
        finally:
            for h in root.handlers[:]:
                if isinstance(h, RotatingFileHandler):
                    root.removeHandler(h)
                    h.close()
            os.chdir(orig_dir)


class TestNoFileLoggingInTestMode:
    """Verify that create_app() in test mode does NOT attach RotatingFileHandler."""

    def test_no_file_handler_in_test_mode(self, tmp_db_path):
        """create_app() with TESTING=True does not attach RotatingFileHandler to root logger."""
        import logging
        from logging.handlers import RotatingFileHandler

        from job_finder.web import create_app

        root = logging.getLogger()

        # Remove any existing RotatingFileHandlers before the test
        existing = [h for h in root.handlers[:] if isinstance(h, RotatingFileHandler)]
        for h in existing:
            root.removeHandler(h)
            h.close()

        create_app(
            config={
                "db": {"path": tmp_db_path},
                "scoring": {"min_score_threshold": 40, "daily_budget_usd": 25.0},
                "profile": {
                    "target_titles": ["Staff Data Scientist"],
                    "target_locations": ["Remote"],
                    "min_salary": 150000,
                    "industries": [],
                    "exclusions": {"title_keywords": [], "companies": []},
                    "skills": [],
                },
                "sources": {},
                "output": {"default_format": "cli", "max_results": 50},
                "TESTING": True,
            }
        )

        has_rfh = any(isinstance(h, RotatingFileHandler) for h in root.handlers)
        assert not has_rfh, "create_app() in TESTING mode should NOT attach RotatingFileHandler"


class TestConsoleEncoding:
    """Verify the issue #234 console-encoding fix in job_finder.__main__.

    The fix forces sys.stdout / sys.stderr to utf-8 at process start so
    Werkzeug's auto-attached _ColorStreamHandler and logging.lastResort
    (both StreamHandlers over the OS console encoding — cp1252 on Windows)
    no longer raise UnicodeEncodeError on the non-Latin-1 characters our
    INFO logs routinely emit (→, —, ·, …, accented titles).

    Tests are OS-independent: we construct a deliberately cp1252-strict
    TextIOWrapper rather than relying on the host console codec.
    """

    def test_reconfigure_stdio_to_utf8_changes_encoding(self, monkeypatch):
        """_reconfigure_stdio_utf8 flips both std streams to utf-8."""
        from job_finder.__main__ import _reconfigure_stdio_utf8

        fake_stdout = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")
        fake_stderr = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")
        monkeypatch.setattr(sys, "stdout", fake_stdout)
        monkeypatch.setattr(sys, "stderr", fake_stderr)

        _reconfigure_stdio_utf8()

        assert sys.stdout.encoding == "utf-8"
        assert sys.stderr.encoding == "utf-8"

    def test_reconfigure_stdio_handles_unreconfigurable_streams(self, monkeypatch):
        """Streams without a reconfigure method (pipes/redirected files) must
        not raise — the AttributeError is swallowed and the call is a no-op."""
        from job_finder.__main__ import _reconfigure_stdio_utf8

        # io.BytesIO has no reconfigure method.
        fake_stdout = io.BytesIO()
        fake_stderr = io.BytesIO()
        monkeypatch.setattr(sys, "stdout", fake_stdout)
        monkeypatch.setattr(sys, "stderr", fake_stderr)

        # No exception → spec satisfied.
        _reconfigure_stdio_utf8()

    def test_streamhandler_over_reconfigured_stderr_emits_non_cp1252(self, monkeypatch):
        """After reconfigure, a logging.StreamHandler over sys.stderr can emit
        the exact glyphs in the issue body (→, —, ·, …, accented title) with
        no UnicodeEncodeError. This is the load-bearing assertion: the producer
        side of the bug (StreamHandler.emit on non-Latin-1 text) no longer
        crashes once the stdio reconfigure has run."""
        from job_finder.__main__ import _reconfigure_stdio_utf8

        fake_stderr = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")
        monkeypatch.setattr(sys, "stderr", fake_stderr)

        _reconfigure_stdio_utf8()

        handler = logging.StreamHandler(sys.stderr)
        logger = logging.getLogger("test_console_encoding_issue_234")
        # Isolate from any handlers added by other tests in the suite.
        logger.handlers = [handler]
        logger.propagate = False
        logger.setLevel(logging.INFO)
        try:
            # Verbatim glyphs from the issue body (N7).
            logger.info("cascade: ollama → groq — Außendienstmitarbeiter · …")
            handler.flush()
        finally:
            logger.handlers = []

        # Decode what actually landed on the wire to prove the line wasn't dropped.
        sys.stderr.flush()
        raw_bytes = sys.stderr.buffer.getvalue()
        decoded = raw_bytes.decode("utf-8")
        assert "→" in decoded
        assert "—" in decoded
        assert "Außendienstmitarbeiter" in decoded

    def test_strict_cp1252_stream_baseline_raises_on_arrow(self):
        """Sanity: a strict cp1252 TextIOWrapper genuinely raises on U+2192.

        Pins the test premise — if the stdlib ever silently downgraded cp1252
        strict mode, the other tests in this class would pass trivially.
        """
        strict = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")
        import pytest

        with pytest.raises(UnicodeEncodeError):
            strict.write("→")
            strict.flush()

    def test_main_invokes_stdio_reconfigure(self, monkeypatch):
        """main() calls _reconfigure_stdio_utf8 before any Flask import path
        executes. Patch the rest of the startup machinery so the test never
        touches a real socket / lock / app."""
        from job_finder import __main__ as main_mod

        monkeypatch.setenv("JOB_CANNON_NO_BROWSER", "1")

        fake_app = MagicMock()
        with (
            patch("job_finder.__main__._reconfigure_stdio_utf8") as mock_reconf,
            patch("job_finder.config.load_config", return_value={}),
            patch("job_finder.web.create_app", return_value=fake_app),
            patch("job_finder.__main__.sys.argv", ["job-cannon", "--terminal"]),
            patch("job_finder.__main__.probe_existing_jc", return_value=None),
            patch("job_finder.__main__._port_is_listening", return_value=False),
            patch(
                "job_finder.__main__.acquire_pidfile",
                return_value=MagicMock(acquired=True),
            ),
            patch("job_finder.web._process_lifecycle.install_kill_on_exit"),
            patch("job_finder.web._runtime.runtime_shutdown"),
        ):
            main_mod.main()

        mock_reconf.assert_called_once_with()
