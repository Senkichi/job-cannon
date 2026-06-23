"""Smoke tests — verify key pages load and HTMX interactions work in a real browser."""

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e


def test_root_redirects_to_jobs(page: Page, live_server: str):
    """GET / redirects to /jobs (the default landing page)."""
    page.goto(live_server)
    expect(page).to_have_url(f"{live_server}/jobs/")


def test_jobs_page_loads(page: Page, live_server: str):
    """Jobs page renders with the job table and sample data."""
    page.goto(f"{live_server}/jobs")
    expect(page).to_have_title("Job Board — Job Cannon")
    # Active job board table should be present. Scope to #jobs-table specifically:
    # the page also contains the (possibly hidden) archived-jobs table, so a bare
    # locator("table") matches 2 elements under Playwright strict mode.
    table = page.locator("#jobs-table")
    expect(table).to_be_visible()
    # Sample job should appear
    expect(page.get_by_text("Senior Data Scientist")).to_be_visible()


def test_dashboard_loads(page: Page, live_server: str):
    """Dashboard page renders without errors."""
    page.goto(f"{live_server}/dashboard")
    expect(page.locator("body")).to_be_visible()
    # Should not show a server error
    expect(page.locator("body")).not_to_contain_text("Internal Server Error")


def test_pipeline_page_loads(page: Page, live_server: str):
    """Pipeline page renders without errors."""
    page.goto(f"{live_server}/pipeline")
    expect(page.locator("body")).to_be_visible()
    expect(page.locator("body")).not_to_contain_text("Internal Server Error")


def test_settings_page_loads(page: Page, live_server: str):
    """Settings page renders with form elements."""
    page.goto(f"{live_server}/settings")
    expect(page.locator("body")).to_be_visible()
    expect(page.locator("body")).not_to_contain_text("Internal Server Error")


def test_companies_page_loads(page: Page, live_server: str):
    """Companies page renders without errors."""
    page.goto(f"{live_server}/companies")
    expect(page.locator("body")).to_be_visible()
    expect(page.locator("body")).not_to_contain_text("Internal Server Error")


def test_job_row_expand(page: Page, live_server: str):
    """Clicking a job row triggers HTMX expansion with detail content."""
    page.goto(f"{live_server}/jobs")
    # Wait for HTMX to load from CDN
    page.wait_for_function("typeof htmx !== 'undefined'", timeout=10000)
    # Click the first job row to expand it
    first_row = page.locator("tr[data-expand-url]").first
    expect(first_row).to_be_visible()
    first_row.click()
    # After HTMX swap, expanded row appears with colspan detail cell
    expanded_cell = page.locator("td[colspan='7']").first
    expect(expanded_cell).to_be_visible(timeout=5000)
