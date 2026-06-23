"""Tests for the custom-miss batch reprobe (PR-A3, job_finder.web.ats_reprobe).

Statically re-fetches frozen (scan_enabled=0) custom-miss careers pages and
promotes any that embed a supported ATS board — re-enabling scan atomically on a
live-verified embed.
"""

import sqlite3
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from job_finder.web.ats_reprobe import reprobe_custom_miss_cohort
from job_finder.web.db_migrate import run_migrations

_GH_HTML = '<html><body><a href="https://boards.greenhouse.io/{slug}">Open roles</a></body></html>'
_NO_ATS_HTML = '<html><body><a href="https://acme.com/about">About us</a></body></html>'
_JOBVITE_ONLY_HTML = (
    '<html><body><a href="https://jobs.jobvite.com/acme/job/x">Jobs</a></body></html>'
)
# A bespoke server-rendered job list (no ATS embed): the generic static
# extractor should pull the Data Scientist link, matching target_titles.
_CUSTOM_LIST_HTML = (
    "<html><body><ul>"
    '<li><a href="/careers/data-scientist-ii">Data Scientist II</a></li>'
    '<li><a href="/careers/warehouse-associate">Warehouse Associate</a></li>'
    "</ul></body></html>"
)
# A marketing shell with listings but none matching target_titles.
_CUSTOM_OFFTARGET_HTML = (
    "<html><body><ul>"
    '<li><a href="/careers/line-cook">Line Cook</a></li>'
    '<li><a href="/careers/dishwasher">Dishwasher</a></li>'
    "</ul></body></html>"
)
# A marketing/landing shell that lists NO jobs itself but links to the real
# openings page (the "wrong careers_url" case the rediscovery pass recovers).
_MARKETING_WITH_OPENINGS = (
    "<html><body><p>Join our mission to build great things.</p>"
    '<a href="/careers/openings">View Open Positions</a></body></html>'
)
# The deeper openings page embeds a Greenhouse board.
_DEEPER_GH_EMBED = (
    "<html><body><h1>Open roles</h1>"
    '<a href="https://boards.greenhouse.io/deepco">See all jobs on Greenhouse</a>'
    "</body></html>"
)

# identity_reconcile must be enabled for the promotion writer to fire; TESTING
# skips the polite sleep so the suite stays fast.
_CONFIG = {"TESTING": True, "ats": {"identity_reconcile": {"enabled": True, "shadow": False}}}
# Config carrying a profile title filter, enabling the generic-extraction pass.
_CONFIG_WITH_PROFILE = {
    "TESTING": True,
    "ats": {"identity_reconcile": {"enabled": True, "shadow": False}},
    "profile": {"target_titles": ["Data Scientist"], "exclusions": {"title_keywords": []}},
}


@pytest.fixture()
def db(tmp_path):
    path = str(tmp_path / "jobs.db")
    run_migrations(path)
    return path


def _seed(
    db_path: str,
    name: str,
    careers_url: str | None,
    *,
    scan_enabled: int = 0,
    ats_probe_status: str = "miss",
    ats_platform: str | None = None,
    ats_slug: str | None = None,
) -> int:
    now = datetime.now().isoformat()
    conn = sqlite3.connect(db_path)
    conn.execute(
        """INSERT INTO companies
              (name, name_raw, careers_url, ats_platform, ats_slug, ats_probe_status,
               miss_reason, scan_enabled, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, 'speculative_exhausted', ?, ?, ?)""",
        (
            name.lower(),
            name,
            careers_url,
            ats_platform,
            ats_slug,
            ats_probe_status,
            scan_enabled,
            now,
            now,
        ),
    )
    cid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    conn.close()
    return int(cid)


def _fake_get(url_to_html: dict):
    """requests.get side_effect: 200+HTML for known URLs, 404 otherwise."""

    def _get(url, **kwargs):
        resp = MagicMock()
        html = url_to_html.get(url)
        if html is None:
            resp.status_code = 404
            resp.text = ""
        else:
            resp.status_code = 200
            resp.text = html
        return resp

    return _get


def _row(db_path: str, cid: int) -> dict:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = dict(conn.execute("SELECT * FROM companies WHERE id = ?", (cid,)).fetchone())
    conn.close()
    return row


@patch("job_finder.web.ats_identity_reconcile._verify_live", return_value=True)
@patch("job_finder.web.ats_reprobe.requests.get")
def test_promotes_and_reenables_frozen_company(mock_get, _verify, db):
    cid = _seed(db, "FrozenCo", "https://frozenco.com/careers", scan_enabled=0)
    mock_get.side_effect = _fake_get(
        {"https://frozenco.com/careers": _GH_HTML.format(slug="frozenco")}
    )

    summary = reprobe_custom_miss_cohort(db, _CONFIG)

    assert summary["embeds_found"] == 1
    assert summary["promoted"] == 1
    row = _row(db, cid)
    assert row["ats_probe_status"] == "hit"
    assert row["ats_platform"] == "greenhouse"
    assert row["ats_slug"] == "frozenco"
    assert row["scan_enabled"] == 1  # frozen company re-enabled on the verified embed


@patch("job_finder.web.ats_identity_reconcile._verify_live", return_value=True)
@patch("job_finder.web.ats_reprobe.requests.get")
def test_no_embed_leaves_company_frozen(mock_get, _verify, db):
    cid = _seed(db, "PlainCo", "https://plainco.com/careers", scan_enabled=0)
    mock_get.side_effect = _fake_get({"https://plainco.com/careers": _NO_ATS_HTML})

    summary = reprobe_custom_miss_cohort(db, _CONFIG)

    assert summary["no_candidate"] == 1
    assert summary["promoted"] == 0
    row = _row(db, cid)
    assert row["ats_probe_status"] == "miss"
    assert row["scan_enabled"] == 0


@patch("job_finder.web.ats_identity_reconcile._verify_live", return_value=True)
@patch("job_finder.web.ats_reprobe.requests.get")
def test_non_scannable_embed_not_promoted(mock_get, _verify, db):
    # A page that only links to a jobvite (non-scannable stub) board → no
    # candidate, never promoted.
    cid = _seed(db, "StubCo", "https://stubco.com/careers", scan_enabled=0)
    mock_get.side_effect = _fake_get({"https://stubco.com/careers": _JOBVITE_ONLY_HTML})

    summary = reprobe_custom_miss_cohort(db, _CONFIG)

    assert summary["no_candidate"] == 1
    assert summary["promoted"] == 0
    assert _row(db, cid)["ats_probe_status"] == "miss"


@patch("job_finder.web.ats_identity_reconcile._verify_live", return_value=True)
@patch("job_finder.web.ats_reprobe.requests.get")
def test_fetch_error_counted_not_fatal(mock_get, _verify, db):
    cid = _seed(db, "DeadCo", "https://deadco.com/careers", scan_enabled=0)
    mock_get.side_effect = _fake_get({})  # every URL 404s

    summary = reprobe_custom_miss_cohort(db, _CONFIG)

    assert summary["checked"] == 1
    assert summary["fetch_errors"] == 1
    assert summary["promoted"] == 0
    assert _row(db, cid)["ats_probe_status"] == "miss"


@patch("job_finder.web.ats_identity_reconcile._verify_live", return_value=True)
@patch("job_finder.web.ats_reprobe.requests.get")
def test_respects_limit(mock_get, _verify, db):
    for i in range(3):
        _seed(db, f"Co{i}", f"https://co{i}.com/careers", scan_enabled=0)
    mock_get.side_effect = _fake_get({})

    summary = reprobe_custom_miss_cohort(db, _CONFIG, limit=1)

    assert summary["checked"] == 1


@patch("job_finder.web.ats_reprobe.requests.get")
def test_disabled_via_config_does_no_fetches(mock_get, db):
    _seed(db, "FrozenCo", "https://frozenco.com/careers", scan_enabled=0)

    summary = reprobe_custom_miss_cohort(db, {"ats": {"reprobe": {"enabled": False}}})

    assert summary["disabled"] == 1
    assert summary["checked"] == 0
    mock_get.assert_not_called()


@patch("job_finder.web.ats_identity_reconcile._verify_live", return_value=True)
@patch("job_finder.web.ats_reprobe.requests.get")
def test_only_eligible_custom_miss_cohort_selected(mock_get, _verify, db):
    # Eligible: ats_platform IS NULL, miss, careers_url present.
    eligible = _seed(db, "Eligible", "https://eligible.com/careers", scan_enabled=0)
    # Excluded: already a hit.
    _seed(
        db,
        "AlreadyHit",
        "https://hit.com/careers",
        scan_enabled=1,
        ats_probe_status="hit",
        ats_platform="lever",
        ats_slug="hitco",
    )
    # Excluded: already has a platform (not custom).
    _seed(db, "HasPlatform", "https://hp.com/careers", ats_platform="workday", ats_slug="hp.wd1/x")
    # Excluded: miss but no careers_url.
    _seed(db, "NoUrl", None, scan_enabled=0)

    mock_get.side_effect = _fake_get(
        {"https://eligible.com/careers": _GH_HTML.format(slug="eligible")}
    )

    summary = reprobe_custom_miss_cohort(db, _CONFIG)

    assert summary["checked"] == 1  # only the eligible company
    assert summary["promoted"] == 1
    assert _row(db, eligible)["ats_probe_status"] == "hit"


@patch("job_finder.web.ats_reprobe.requests.get")
def test_custom_extractable_reenables_scan(mock_get, db):
    # No ATS embed, but the bespoke page server-renders a target-matching job.
    # The generic static extractor pulls it -> re-enable scan (hand to crawl),
    # WITHOUT claiming an ATS platform.
    cid = _seed(db, "CustomCo", "https://customco.com/careers", scan_enabled=0)
    mock_get.side_effect = _fake_get({"https://customco.com/careers": _CUSTOM_LIST_HTML})

    summary = reprobe_custom_miss_cohort(db, _CONFIG_WITH_PROFILE)

    assert summary["custom_extractable"] == 1
    assert summary["promoted"] == 0
    assert summary["no_candidate"] == 0
    row = _row(db, cid)
    assert row["scan_enabled"] == 1  # re-enabled for the daily careers crawl
    assert row["ats_platform"] is None  # no platform claim — not an ATS hit
    assert row["ats_probe_status"] == "miss"


@patch("job_finder.web.ats_reprobe.requests.get")
def test_offtarget_listing_stays_frozen(mock_get, db):
    # Page lists jobs, but none match target_titles -> not a viable source for
    # us; leave it frozen.
    cid = _seed(db, "OffTargetCo", "https://offtarget.com/careers", scan_enabled=0)
    mock_get.side_effect = _fake_get({"https://offtarget.com/careers": _CUSTOM_OFFTARGET_HTML})

    summary = reprobe_custom_miss_cohort(db, _CONFIG_WITH_PROFILE)

    assert summary["custom_extractable"] == 0
    assert summary["no_candidate"] == 1
    row = _row(db, cid)
    assert row["scan_enabled"] == 0
    assert row["ats_probe_status"] == "miss"


@patch("job_finder.web.ats_reprobe.requests.get")
def test_no_profile_disables_static_pass(mock_get, db):
    # Same extractable page, but config carries no profile/target_titles -> the
    # generic-extraction pass is disabled; company stays frozen (no_candidate).
    cid = _seed(db, "CustomCo", "https://customco.com/careers", scan_enabled=0)
    mock_get.side_effect = _fake_get({"https://customco.com/careers": _CUSTOM_LIST_HTML})

    summary = reprobe_custom_miss_cohort(db, _CONFIG)  # no profile key

    assert summary["custom_extractable"] == 0
    assert summary["no_candidate"] == 1
    assert _row(db, cid)["scan_enabled"] == 0


@patch("job_finder.web.ats_identity_reconcile._verify_live", return_value=True)
@patch("job_finder.web.ats_reprobe.requests.get")
def test_rediscovers_ats_via_openings_link(mock_get, _verify, db):
    # careers_url is a marketing shell with no embed; its "View Open Positions"
    # link leads to a page that embeds Greenhouse. Reprobe follows the link,
    # promotes on the deeper page, and REPOINTS careers_url to it.
    cid = _seed(db, "MarketingCo", "https://marketingco.com/careers", scan_enabled=0)
    mock_get.side_effect = _fake_get(
        {
            "https://marketingco.com/careers": _MARKETING_WITH_OPENINGS,
            "https://marketingco.com/careers/openings": _DEEPER_GH_EMBED,
        }
    )

    summary = reprobe_custom_miss_cohort(db, _CONFIG)

    assert summary["rediscovered_ats"] == 1
    assert summary["promoted"] == 0  # counted under rediscovered_ats
    assert summary["no_candidate"] == 0
    row = _row(db, cid)
    assert row["ats_platform"] == "greenhouse"
    assert row["ats_slug"] == "deepco"
    assert row["ats_probe_status"] == "hit"
    assert row["scan_enabled"] == 1
    # careers_url repointed from the marketing shell to the real openings page.
    assert row["careers_url"] == "https://marketingco.com/careers/openings"


@patch("job_finder.web.ats_reprobe.requests.get")
def test_rediscovers_custom_via_openings_link(mock_get, db):
    # The openings link leads to a bespoke server-rendered list with a
    # target-matching role. Reprobe re-enables scan and repoints careers_url —
    # no ATS platform claimed.
    cid = _seed(db, "BespokeCo", "https://bespokeco.com/careers", scan_enabled=0)
    mock_get.side_effect = _fake_get(
        {
            "https://bespokeco.com/careers": _MARKETING_WITH_OPENINGS,
            "https://bespokeco.com/careers/openings": _CUSTOM_LIST_HTML,
        }
    )

    summary = reprobe_custom_miss_cohort(db, _CONFIG_WITH_PROFILE)

    assert summary["rediscovered_custom"] == 1
    assert summary["no_candidate"] == 0
    row = _row(db, cid)
    assert row["scan_enabled"] == 1
    assert row["ats_platform"] is None  # no platform claim
    assert row["ats_probe_status"] == "miss"
    assert row["careers_url"] == "https://bespokeco.com/careers/openings"  # repointed


@patch("job_finder.web.ats_reprobe.requests.get")
def test_rediscovery_no_openings_link_stays_frozen(mock_get, db):
    # Marketing shell with no job-listings link anywhere -> nothing to follow.
    cid = _seed(db, "DeadShell", "https://deadshell.com/careers", scan_enabled=0)
    mock_get.side_effect = _fake_get({"https://deadshell.com/careers": _NO_ATS_HTML})

    summary = reprobe_custom_miss_cohort(db, _CONFIG_WITH_PROFILE)

    assert summary["no_candidate"] == 1
    assert summary["rediscovered_ats"] == 0
    assert summary["rediscovered_custom"] == 0
    row = _row(db, cid)
    assert row["scan_enabled"] == 0
    assert row["careers_url"] == "https://deadshell.com/careers"  # unchanged


@patch("job_finder.web.ats_reprobe.requests.get")
def test_rediscovery_deeper_offtarget_stays_frozen(mock_get, db):
    # The openings link leads to a real list, but none match target_titles ->
    # not a viable source; leave frozen and DON'T repoint careers_url.
    cid = _seed(db, "OffDeep", "https://offdeep.com/careers", scan_enabled=0)
    mock_get.side_effect = _fake_get(
        {
            "https://offdeep.com/careers": _MARKETING_WITH_OPENINGS,
            "https://offdeep.com/careers/openings": _CUSTOM_OFFTARGET_HTML,
        }
    )

    summary = reprobe_custom_miss_cohort(db, _CONFIG_WITH_PROFILE)

    assert summary["no_candidate"] == 1
    assert summary["rediscovered_custom"] == 0
    row = _row(db, cid)
    assert row["scan_enabled"] == 0
    assert row["careers_url"] == "https://offdeep.com/careers"  # unchanged
