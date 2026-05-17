"""Tests for updates blueprint HTMX dismiss endpoint."""
import pytest


@pytest.fixture
def tmp_path(tmp_path, monkeypatch):
    """Override user data dir for test isolation."""
    monkeypatch.setenv("JOB_CANNON_USER_DATA_DIR", str(tmp_path))
    return tmp_path


def test_htmx_post_dismisses_returns_empty_200(client, tmp_path):
    """Test 1: HTMX POST dismisses + returns empty 200."""
    resp = client.post(
        "/updates/dismiss/v5.0.1", headers={"HX-Request": "true"}
    )
    assert resp.status_code == 200
    assert resp.data == b""


def test_htmx_post_appends_to_update_check_json(client, tmp_path):
    """Test 2: HTMX POST appends to update_check.json."""
    client.post("/updates/dismiss/v5.0.1", headers={"HX-Request": "true"})
    from job_finder.web import update_check

    result = update_check.read_cache()
    assert result is not None
    assert "v5.0.1" in result["dismissed_versions"]


def test_non_htmx_direct_browser_hit_redirects(client):
    """Test 3: non-HTMX direct browser hit redirects."""
    resp = client.post("/updates/dismiss/v5.0.1")
    assert resp.status_code == 302
    assert resp.location.endswith("/dashboard") or resp.location.endswith("/dashboard/")


def test_oversized_version_rejected(client):
    """Test 4: oversized version rejected."""
    long_version = "x" * 100
    resp = client.post(
        f"/updates/dismiss/{long_version}", headers={"HX-Request": "true"}
    )
    assert resp.status_code == 400


def test_idempotent_repeated_dismiss_does_not_duplicate(client, tmp_path):
    """Test 5: idempotent — repeated dismiss does not duplicate."""
    client.post("/updates/dismiss/v5.0.1", headers={"HX-Request": "true"})
    client.post("/updates/dismiss/v5.0.1", headers={"HX-Request": "true"})
    client.post("/updates/dismiss/v5.0.1", headers={"HX-Request": "true"})
    from job_finder.web import update_check

    result = update_check.read_cache()
    assert result["dismissed_versions"] == ["v5.0.1"]


def test_cache_write_failure_still_returns_200(client, tmp_path, monkeypatch):
    """Test 6: cache-write failure still returns 200."""
    def _raising(*args, **kwargs):
        raise OSError("disk full")

    # Patch the blueprint's imported binding, not the source module
    monkeypatch.setattr(
        "job_finder.web.blueprints.updates.append_dismissed_version", _raising
    )
    resp = client.post("/updates/dismiss/v5.0.1", headers={"HX-Request": "true"})
    assert resp.status_code == 200


def test_route_registered_with_strict_slashes_false(client):
    """Test 7: route registered with strict_slashes=False (trailing slash accepted)."""
    resp = client.post("/updates/dismiss/v5.0.1/", headers={"HX-Request": "true"})
    assert resp.status_code == 200
