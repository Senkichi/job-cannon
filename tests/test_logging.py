"""Tests for persistent file logging (DEBT-07)."""
import logging
from logging.handlers import RotatingFileHandler

import pytest


class TestFileLogging:
    """Verify RotatingFileHandler setup and idempotency guard."""

    def test_setup_file_logging_attaches_handler(self, tmp_path):
        """_setup_file_logging() directly attaches a RotatingFileHandler when called explicitly."""
        from job_finder.web import _setup_file_logging
        import os
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
        from job_finder.web import _setup_file_logging
        import os
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
            assert count_after == count_before, "Idempotency guard failed: duplicate handlers added"
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
        from job_finder.web import create_app
        import logging
        from logging.handlers import RotatingFileHandler

        root = logging.getLogger()

        # Remove any existing RotatingFileHandlers before the test
        existing = [h for h in root.handlers[:] if isinstance(h, RotatingFileHandler)]
        for h in existing:
            root.removeHandler(h)
            h.close()

        create_app(config={
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
        })

        has_rfh = any(isinstance(h, RotatingFileHandler) for h in root.handlers)
        assert not has_rfh, "create_app() in TESTING mode should NOT attach RotatingFileHandler"
