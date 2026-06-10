"""E2E: Jobs page HTMX interactions update DOM without page refresh."""

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e

BASE_TIMEOUT = 5000  # 5s for HTMX swaps


# ---------------------------------------------------------------------------
# Accordion expand / collapse
# ---------------------------------------------------------------------------


class TestAccordionExpandCollapse:
    def test_expand_shows_detail_inline(self, page: Page, live_server: str):
        """Clicking a compact row loads detail inline without page reload."""
        page.goto(f"{live_server}/jobs")
        page.wait_for_function("typeof htmx !== 'undefined'", timeout=10000)

        first_row = page.locator("tr[data-expand-url]").first
        expect(first_row).to_be_visible()

        # Track navigation — clicking expand must NOT navigate away
        navigated = []
        page.on("framenavigated", lambda frame: navigated.append(frame.url))

        first_row.click()
        expanded_cell = page.locator("td[colspan='7']").first
        expect(expanded_cell).to_be_visible(timeout=BASE_TIMEOUT)

        assert not navigated, "Row expand should not cause page navigation"

    def test_collapse_restores_placeholder(self, page: Page, live_server: str):
        """Clicking collapse hides the detail row (no stale content remains)."""
        page.goto(f"{live_server}/jobs")
        page.wait_for_function("typeof htmx !== 'undefined'", timeout=10000)

        first_row = page.locator("tr[data-expand-url]").first
        first_row.click()
        # Wait for expand
        expanded_cell = page.locator("td[colspan='7']").first
        expect(expanded_cell).to_be_visible(timeout=BASE_TIMEOUT)

        # Find and click collapse button
        collapse_btn = page.locator("button[hx-get*='/expand']").first
        if not collapse_btn.is_visible():
            collapse_btn = page.get_by_text("Collapse").first
        collapse_btn.click()

        # Detail cell must disappear
        expect(expanded_cell).not_to_be_visible(timeout=BASE_TIMEOUT)


# ---------------------------------------------------------------------------
# Filter bar interactions
# ---------------------------------------------------------------------------


class TestFilterBar:
    def test_status_pills_filter_table(self, page: Page, live_server: str):
        """Clicking a status pill filters the table to that status only."""
        page.goto(f"{live_server}/jobs")
        page.wait_for_function("typeof htmx !== 'undefined'", timeout=10000)
        page.wait_for_load_state("networkidle")

        # Redesign: status pills live inside the Status dropdown popover — open it first.
        page.locator("#status-dd > button").click()

        # Click the visible span label (the hidden checkbox drives the filter via JS)
        reviewing_span = page.locator("span.status-pill-label").filter(has_text="Reviewing")
        reviewing_span.click()

        # Wait for HTMX swap
        page.wait_for_load_state("networkidle")

        table_body = page.locator("#job-table-body")
        content = table_body.inner_text()

        # Stripe has status "reviewing" — should be present
        assert "Stripe" in content, "Reviewing job should be in filtered results"

    def test_freshness_toggle_filters_table(self, page: Page, live_server: str):
        """Clicking 'Last 3 Biz Days' freshness button triggers HTMX filter."""
        page.goto(f"{live_server}/jobs")
        page.wait_for_function("typeof htmx !== 'undefined'", timeout=10000)
        page.wait_for_load_state("networkidle")

        # Redesign: freshness toggles live inside the Filters popover — open it first.
        page.locator("#filters-dd > button").click()

        biz3_btn = page.locator("#filter-biz-3")
        expect(biz3_btn).to_be_visible()

        biz3_btn.click()
        page.wait_for_load_state("networkidle")

        # FreshCo was posted today — should survive the biz3 cutoff
        # (All other jobs are a week old — whether they survive depends on the biz day cutoff)
        table_body = page.locator("#job-table-body")
        expect(table_body).to_be_visible()

        # Button should appear active (indigo background)
        btn_classes = biz3_btn.get_attribute("class")
        assert "bg-indigo-600" in btn_classes, (
            "Active freshness button should have indigo background"
        )

    def test_freshness_toggle_deactivates_on_second_click(self, page: Page, live_server: str):
        """Clicking the active freshness button again clears the filter."""
        page.goto(f"{live_server}/jobs")
        page.wait_for_function("typeof htmx !== 'undefined'", timeout=10000)

        # Redesign: freshness toggles live inside the Filters popover — open it first.
        page.locator("#filters-dd > button").click()

        biz3_btn = page.locator("#filter-biz-3")
        biz3_btn.click()
        page.wait_for_load_state("networkidle")

        # Second click should deactivate
        biz3_btn.click()
        page.wait_for_load_state("networkidle")

        btn_classes = biz3_btn.get_attribute("class")
        assert "bg-indigo-600" not in btn_classes, (
            "Deactivated button should not have indigo background"
        )

    def test_freshness_and_posted_within_are_mutually_exclusive(
        self, page: Page, live_server: str
    ):
        """Selecting 'posted within' while freshness is active clears freshness."""
        page.goto(f"{live_server}/jobs")
        page.wait_for_function("typeof htmx !== 'undefined'", timeout=10000)

        # Redesign: freshness toggles live inside the Filters popover — open it first.
        page.locator("#filters-dd > button").click()

        biz3_btn = page.locator("#filter-biz-3")
        biz3_btn.click()
        page.wait_for_load_state("networkidle")

        # Now select 'today' from posted_within dropdown
        page.select_option("#filter-posted-within", "today")
        page.wait_for_load_state("networkidle")

        # Freshness button should be deactivated
        btn_classes = biz3_btn.get_attribute("class")
        assert "bg-indigo-600" not in btn_classes, (
            "Freshness toggle should clear when posted_within changes"
        )

        # Freshness hidden input should be empty
        freshness_val = page.locator("#filter-freshness").input_value()
        assert freshness_val == "", "Freshness hidden input should be cleared"

    def test_show_hidden_reveals_dismissed_jobs(self, page: Page, live_server: str):
        """Checking 'Show hidden' reveals dismissed and rejected jobs."""
        # Clear any filter state from prior tests in this browser context
        page.goto(f"{live_server}/jobs")
        page.evaluate("localStorage.clear()")
        page.reload()
        page.wait_for_function("typeof htmx !== 'undefined'", timeout=10000)
        page.wait_for_load_state("networkidle")

        # Dismissed/rejected jobs should NOT appear by default
        table_body = page.locator("#job-table-body")
        default_content = table_body.inner_text()
        assert "Data Science Intern" not in default_content, (
            "Dismissed intern job should not appear in default view"
        )
        assert "OldCo" not in default_content, "Rejected job should not appear in default view"

        # Enable show_hidden and wait for the HTMX /jobs/table response.
        # Redesign: show_hidden lives inside the Filters popover — open it first.
        page.locator("#filters-dd > button").click()
        show_hidden_cb = page.locator("input[name='show_hidden']")
        with page.expect_response(lambda r: "/jobs/table" in r.url):
            show_hidden_cb.click()
            page.evaluate(
                "document.querySelector('input[name=\"show_hidden\"]')"
                ".dispatchEvent(new Event('change', {bubbles: true}))"
            )

        # Dismissed job (Data Science Intern) should now appear
        all_content = table_body.inner_text()
        assert "Intern" in all_content or "OldCo" in all_content, (
            "Hidden jobs should be visible after enabling show_hidden"
        )


# ---------------------------------------------------------------------------
# DOM integrity
# ---------------------------------------------------------------------------


class TestDOMIntegrity:
    def test_no_duplicate_rows_after_expand_collapse(self, page: Page, live_server: str):
        """Expand then collapse leaves no stale expanded cells in the DOM."""
        page.goto(f"{live_server}/jobs")
        page.wait_for_function("typeof htmx !== 'undefined'", timeout=10000)

        first_row = page.locator("tr[data-expand-url]").first
        first_row.click()
        expanded_cell = page.locator("td[colspan='7']").first
        expect(expanded_cell).to_be_visible(timeout=BASE_TIMEOUT)

        # Collapse
        collapse_btn = page.locator("button[hx-get*='/expand']").first
        if not collapse_btn.is_visible():
            collapse_btn = page.get_by_text("Collapse").first
        collapse_btn.click()
        page.wait_for_load_state("networkidle")

        # After collapse, no expanded cells should remain visible
        expect(expanded_cell).not_to_be_visible(timeout=BASE_TIMEOUT)
        # And no orphaned detail cells should exist
        assert page.locator("td[colspan='7']:visible").count() == 0, (
            "No expanded detail cells should remain after collapse"
        )

    def test_filter_form_present_and_interactive(self, page: Page, live_server: str):
        """Filter form is present with expected new controls."""
        page.goto(f"{live_server}/jobs")

        expect(page.locator("#filter-form")).to_be_visible()
        # Tier-1 controls are always visible.
        expect(page.locator("#filter-posted-within")).to_be_visible()
        # Redesign: advanced controls live inside the Filters popover — open it.
        page.locator("#filters-dd > button").click()
        expect(page.locator("#filter-biz-1")).to_be_visible()
        expect(page.locator("#filter-biz-3")).to_be_visible()
        expect(page.locator("input[name='show_hidden']")).to_be_visible()
        expect(page.locator("#filter-freshness")).to_be_attached()

    def test_filter_state_persists_via_localstorage(self, page: Page, live_server: str):
        """Filter state saved to localStorage is restored on page reload."""
        page.goto(f"{live_server}/jobs")
        page.wait_for_function("typeof htmx !== 'undefined'", timeout=10000)
        page.wait_for_load_state("networkidle")

        # Change sort to 'Date'
        page.select_option("#filter-sort-by", "first_seen")
        page.wait_for_load_state("networkidle")

        # Reload the page
        page.reload()
        page.wait_for_function("typeof htmx !== 'undefined'", timeout=10000)
        page.wait_for_load_state("networkidle")

        # Sort selection should be restored from localStorage
        sort_val = page.locator("#filter-sort-by").input_value()
        assert sort_val == "first_seen", (
            f"Sort filter should be restored from localStorage, got: {sort_val!r}"
        )

    def test_posted_within_restore_actually_refreshes_table(self, page: Page, live_server: str):
        """posted_within=today restored from localStorage must drive an HTMX fetch
        so the table reflects the filter, not just the dropdown UI.

        Regression: dispatching `change` on the form did not match the form's
        `hx-trigger="change from:select"` filter, so the dropdown displayed
        'Today' while the table still showed all jobs.
        """
        page.goto(f"{live_server}/jobs")
        page.evaluate("localStorage.clear()")
        page.reload()
        page.wait_for_function("typeof htmx !== 'undefined'", timeout=10000)
        page.wait_for_load_state("networkidle")

        # Instrument htmx:afterSettle so we can wait for the swap to fully
        # apply (HTMX strips the transient `htmx-added` / `htmx-swapping`
        # classes during settle, so we must read AFTER settle to get a
        # stable inner_html). networkidle alone fires before the swap settles.
        page.evaluate(
            """
            window._tableSettles = 0;
            document.body.addEventListener('htmx:afterSettle', function(evt) {
              if (evt.detail && evt.detail.target && evt.detail.target.id === 'job-table-body') {
                window._tableSettles += 1;
              }
            });
            """
        )

        # Apply 'today' filter and wait for the HTMX swap to settle.
        page.select_option("#filter-posted-within", "today")
        page.wait_for_function("window._tableSettles >= 1", timeout=10000)
        filtered_html = page.locator("#job-table-body").inner_html()

        # Reload — localStorage should reapply 'today' and fire an HTMX request
        # that refreshes #job-table-body to the same filtered contents.
        page.reload()
        page.wait_for_function("typeof htmx !== 'undefined'", timeout=10000)
        page.wait_for_load_state("networkidle")
        # Re-instrument after reload (window was reset by navigation) and wait
        # for the restoreFilters-driven swap to settle. The form's checkbox-on
        # request and the form-level request both target #job-table-body, so
        # we wait until at least one has settled before reading.
        page.evaluate(
            """
            window._tableSettles = 0;
            document.body.addEventListener('htmx:afterSettle', function(evt) {
              if (evt.detail && evt.detail.target && evt.detail.target.id === 'job-table-body') {
                window._tableSettles += 1;
              }
            });
            """
        )
        # The restore triggers `change` on hide_stale (firstRestored), which
        # propagates to both the checkbox's own hx-get and the form's hx-trigger.
        # Wait for at least one settle; if no settle fires (a regression of
        # the actual bug), let the inner_html comparison below surface it.
        try:
            page.wait_for_function("window._tableSettles >= 1", timeout=5000)
        except Exception:
            pass
        # Two requests fire on restore (form + checkbox); give the second
        # settle a moment to land so we don't capture a state between them.
        page.wait_for_load_state("networkidle")

        # Dropdown should show 'today'
        assert page.locator("#filter-posted-within").input_value() == "today"

        # AND the table body must match the filtered state — not the all-jobs
        # initial render. We compare to the locally-filtered HTML rather than
        # asserting a row count, because fixture data may produce 0 today-rows.
        restored_html = page.locator("#job-table-body").inner_html()
        assert restored_html == filtered_html, (
            "Table body did not refresh to match restored posted_within=today; "
            "dropdown shows 'today' but listing reflects the unfiltered initial render."
        )
