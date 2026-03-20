"""Tests for persistent file logging (DEBT-07)."""
import logging
from logging.handlers import RotatingFileHandler

import pytest


class TestFileLogging:
    """Verify RotatingFileHandler setup and idempotency guard."""

    def test_create_app_attaches_file_handler(self, app):
        """After create_app(), root logger has a RotatingFileHandler."""
        root = logging.getLogger()
        has_rfh = any(isinstance(h, RotatingFileHandler) for h in root.handlers)
        assert has_rfh, "RotatingFileHandler not found on root logger"

    def test_idempotency_guard_prevents_duplicate_handlers(self, app):
        """Calling _setup_file_logging() again does not add a second handler."""
        from job_finder.web import _setup_file_logging
        root = logging.getLogger()
        count_before = sum(1 for h in root.handlers if isinstance(h, RotatingFileHandler))
        _setup_file_logging()
        count_after = sum(1 for h in root.handlers if isinstance(h, RotatingFileHandler))
        assert count_after == count_before
