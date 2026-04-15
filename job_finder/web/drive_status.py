"""Per-request Google Drive status helper.

Provides a single function get_drive_status() that checks whether Drive
integration is ready for use. Results are cached on Flask g for the duration
of the request.

Usage:
    from job_finder.web.drive_status import get_drive_status

    status = get_drive_status(config)
    if not status["ok"]:
        # show error UI based on status["error_code"]
        pass

Error codes:
    no_token       -- token.json does not exist
    missing_scope  -- token exists but lacks drive.file scope
    refresh_failed -- token refresh raised an exception
    no_folder_id   -- token is valid but Drive folder is not configured
"""

from pathlib import Path

from flask import g

_DRIVE_FILE_SCOPE = "https://www.googleapis.com/auth/drive.file"

def get_drive_status(config: dict, token_path: str = "token.json") -> dict:
    """Return a structured dict describing current Google Drive readiness.

    Caches the result on ``flask.g.drive_status`` for the duration of the
    request so multiple callers in the same request cycle pay the I/O cost
    only once.

    Args:
        config: Application config dict (reads ``config["drive"]["folder_id"]``).
        token_path: Path to the saved OAuth token JSON file.

    Returns:
        dict with keys:
            ok          -- bool, True only when Drive is fully ready
            error       -- human-readable error string (None when ok=True)
            error_code  -- machine-readable code (None when ok=True):
                           "no_token" | "missing_scope" | "refresh_failed" |
                           "no_folder_id"
    """
    if hasattr(g, "drive_status"):
        return g.drive_status

    result = _compute_drive_status(config, token_path)
    g.drive_status = result
    return result

def _compute_drive_status(config: dict, token_path: str) -> dict:
    """Internal: compute Drive status without caching."""
    try:
        # 1-4. Token existence, scope check, and refresh via centralized function
        from job_finder.gmail_auth import get_credentials, AuthenticationError
        try:
            creds = get_credentials(token_path)
        except AuthenticationError as exc:
            error_msg = str(exc)
            if "not found" in error_msg:
                return {"ok": False, "error": error_msg, "error_code": "no_token"}
            elif "refresh failed" in error_msg.lower():
                return {"ok": False, "error": error_msg, "error_code": "refresh_failed"}
            else:
                return {"ok": False, "error": error_msg, "error_code": "refresh_failed"}

        # Check that drive.file scope was actually granted
        if not creds.scopes or _DRIVE_FILE_SCOPE not in creds.scopes:
            return {
                "ok": False,
                "error": "Token lacks drive.file scope. Run: python -m job_finder.gmail_auth",
                "error_code": "missing_scope",
            }

        # 5. Drive folder must be configured
        folder_id = config.get("drive", {}).get("folder_id", "")
        if not folder_id:
            return {
                "ok": False,
                "error": "Drive folder not configured. Run: python -m job_finder.gmail_auth",
                "error_code": "no_folder_id",
            }

        return {"ok": True, "error": None, "error_code": None}

    except Exception as exc:
        # Catch-all: never crash the caller
        return {
            "ok": False,
            "error": f"Unexpected error checking Drive status: {exc}",
            "error_code": "unknown",
        }
