"""Tests for compute_location_fit — P3.1 deterministic location_fit override.

Covers the FULL rule table including all †-rows (requires home_country).

Rule table (first-match-wins):
    Row 1: any REMOTE unrestricted, 'Remote' ∈ targets  → (5, "fully remote, remote targeted")
    Row 2: any REMOTE restricted to home_country †       → (5, "fully remote, remote targeted")
    Row 3: all REMOTE restricted to countries ≠ home †  → (1, "remote but ineligible geography")
    Row 4: all onsite/hybrid/UNSP, countries ≠ home, no geo target match † → (1, "on-site outside candidate geography")
    Row 5: any city/region/country matches non-Remote target → (5, "on-site/hybrid in target geography")
    otherwise → None

Reference: issue #390.
"""

from __future__ import annotations

from job_finder.web.location_fit import compute_location_fit

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _loc(
    workplace_type: str = "UNSPECIFIED",
    country_code: str | None = None,
    city: str | None = None,
    region: str | None = None,
    country: str | None = None,
    unresolved: bool = False,
) -> dict:
    """Build a location dict matching the JobLocation JSON shape."""
    return {
        "workplace_type": workplace_type,
        "country_code": country_code,
        "city": city,
        "region": region,
        "country": country,
        "region_code": None,
        "raw": "",
        "unresolved": unresolved,
    }


def _remote(country_code: str | None = None, **kw) -> dict:
    return _loc(workplace_type="REMOTE", country_code=country_code, **kw)


def _onsite(country_code: str | None = None, **kw) -> dict:
    return _loc(workplace_type="ONSITE", country_code=country_code, **kw)


def _hybrid(country_code: str | None = None, **kw) -> dict:
    return _loc(workplace_type="HYBRID", country_code=country_code, **kw)


# ---------------------------------------------------------------------------
# Row 1: any REMOTE unrestricted, 'Remote' ∈ targets → (5, ...)
# ---------------------------------------------------------------------------


class TestRow1RemoteUnrestricted:
    def test_remote_no_country_code_with_remote_target(self):
        locs = [_remote()]  # no country_code = unrestricted
        score, reason = compute_location_fit(locs, "REMOTE", None, ["Remote"], "US")
        assert score == 5
        assert "remote" in reason.lower()

    def test_remote_no_country_code_multiple_targets(self):
        locs = [_remote()]
        score, _ = compute_location_fit(locs, "REMOTE", None, ["Remote", "San Francisco"], "US")
        assert score == 5

    def test_remote_no_country_code_no_remote_target_skips_row1(self):
        """Without 'Remote' in targets, row 1 must not fire."""
        locs = [_remote()]
        result = compute_location_fit(locs, "REMOTE", None, ["San Francisco"], "US")
        # Row 1 does NOT fire; row 5 checks city match (city is None here → no match)
        assert result is None

    def test_remote_no_country_code_no_home_country(self):
        """Row 1 does not require home_country."""
        locs = [_remote()]
        score, _ = compute_location_fit(locs, "REMOTE", None, ["Remote"], None)
        assert score == 5

    def test_remote_row1_fires_even_with_mixed_locations(self):
        """Best location wins — one unrestricted remote + one onsite fires row 1."""
        locs = [_remote(), _onsite(country_code="IN")]
        score, _ = compute_location_fit(locs, None, None, ["Remote"], "US")
        assert score == 5


# ---------------------------------------------------------------------------
# Row 2: any REMOTE restricted to home_country  † → (5, ...)
# ---------------------------------------------------------------------------


class TestRow2RemoteHomecountry:
    def test_remote_us_home_us_target_remote(self):
        locs = [_remote(country_code="US")]
        score, reason = compute_location_fit(locs, "REMOTE", "US", ["Remote"], "US")
        assert score == 5
        assert "remote" in reason.lower()

    def test_remote_home_country_matches_case_insensitively(self):
        locs = [_remote(country_code="us")]
        score, _ = compute_location_fit(locs, None, None, ["Remote"], "US")
        assert score == 5

    def test_row2_requires_remote_in_targets(self):
        """Row 2 needs 'Remote' ∈ targets to fire."""
        locs = [_remote(country_code="US")]
        # 'Remote' not in targets → row 2 skipped; row 5 checks city (None) → None
        result = compute_location_fit(locs, None, None, ["New York"], "US")
        assert result is None

    def test_row2_skipped_when_no_home_country(self):
        """†-row: without home_country, row 2 cannot fire."""
        locs = [_remote(country_code="US")]
        # home_country=None, 'Remote' in targets, but row 2 skipped
        # Row 1 also skipped (country_code="US" makes it restricted, not unrestricted)
        # Row 5: city=None → None
        result = compute_location_fit(locs, None, None, ["Remote"], None)
        assert result is None

    def test_row2_skipped_when_home_country_doesnt_match(self):
        """Row 2 requires the REMOTE location's country = home_country."""
        locs = [_remote(country_code="GB")]  # GB, not US
        result = compute_location_fit(locs, None, None, ["Remote"], "US")
        # Row 1 skipped (restricted), row 2 skipped (GB ≠ US)
        # Row 3 fires: all remote, restricted to country ≠ home
        score, reason = result
        assert score == 1
        assert "ineligible" in reason.lower()


# ---------------------------------------------------------------------------
# Row 3: all REMOTE restricted to countries ≠ home_country † → (1, ...)
# ---------------------------------------------------------------------------


class TestRow3AllRemoteIneligible:
    def test_remote_in_country_outside_home(self):
        """Single REMOTE restricted to IN ≠ US home."""
        locs = [_remote(country_code="IN")]
        score, reason = compute_location_fit(locs, "REMOTE", "IN", ["Remote"], "US")
        assert score == 1
        assert "ineligible" in reason.lower()

    def test_remote_multiple_all_outside_home(self):
        """All three remotes restricted to non-home countries."""
        locs = [_remote("IN"), _remote("GB"), _remote("DE")]
        score, reason = compute_location_fit(locs, None, None, ["Remote"], "US")
        assert score == 1
        assert "ineligible" in reason.lower()

    def test_row3_skipped_when_one_remote_is_home(self):
        """Mix of home + non-home remotes: row 3 doesn't fire (best-wins)."""
        locs = [_remote("IN"), _remote("US")]
        # Row 2: any remote restricted to home → fires
        score, _ = compute_location_fit(locs, None, None, ["Remote"], "US")
        assert score == 5

    def test_row3_skipped_when_no_home_country(self):
        """† — without home_country, row 3 cannot fire."""
        locs = [_remote("IN")]
        # No home_country: row 1 skipped (restricted), row 2 skipped, row 3 skipped
        # Row 5: city=None → None
        result = compute_location_fit(locs, None, None, ["Remote"], None)
        assert result is None

    def test_row3_skipped_when_remote_has_no_country(self):
        """An unrestricted REMOTE (no country_code) is NOT outside any country."""
        locs = [_remote()]  # no country_code
        # Row 1 fires first (unrestricted + Remote in targets)
        score, _ = compute_location_fit(locs, None, None, ["Remote"], "US")
        assert score == 5

    def test_row3_skipped_when_has_onsite_fallback(self):
        """Row 3 must NOT fire when there is also a non-remote location."""
        locs = [_remote("IN"), _onsite("US")]
        # All-remote check fails → row 3 skipped
        # Row 5: onsite US city/region? city=None → check ...
        result = compute_location_fit(locs, None, None, ["Remote"], "US")
        # Onsite US, no geo target match → row 4 check? onsite in home country → row 4 NOT fire (all outside)
        # Falls to row 5: city=None, country="US" not in [Remote] targets → None
        assert result is None


# ---------------------------------------------------------------------------
# Row 4: all onsite/hybrid/UNSP outside home_country, no geo target match †
# → (1, "on-site outside candidate geography")
# ---------------------------------------------------------------------------


class TestRow4OnsiteOutsideHome:
    def test_onsite_in_india_us_home(self):
        """Classic Hyderabad regression: EY DS job, targets=[Remote], home=US."""
        locs = [_onsite("IN", city="Hyderābād", region="Telangana", country="India")]
        score, reason = compute_location_fit(locs, "ONSITE", "IN", ["Remote"], "US")
        assert score == 1
        assert "on-site outside" in reason.lower()

    def test_hyderabad_regression_end_to_end(self):
        """Issue #390 Hyderabad regression test.

        EY DE-Data-Scientist-VG-W4-CDAO0217 row facts:
          locations_structured: [{city: "Hyderābād", country_code: "IN", workplace_type: "ONSITE"}]
          primary_country_code: "IN"
          targets: ["Remote"], home: "US"
        Expected: (1, "on-site outside candidate geography")
        """
        locs = [_onsite("IN", city="Hyderābād", region="Telangana", country="India")]
        result = compute_location_fit(locs, "ONSITE", "IN", ["Remote"], "US")
        assert result is not None
        score, reason = result
        assert score == 1, f"Expected 1, got {score}"
        assert "on-site outside" in reason.lower()

    def test_onsite_multiple_all_outside_home(self):
        locs = [_onsite("IN"), _onsite("SG")]
        score, reason = compute_location_fit(locs, None, None, ["Remote"], "US")
        assert score == 1
        assert "on-site outside" in reason.lower()

    def test_row4_skipped_when_no_home_country(self):
        """† — without home_country, row 4 cannot fire."""
        locs = [_onsite("IN")]
        result = compute_location_fit(locs, None, None, ["Remote"], None)
        # Row 5: city=None → None
        assert result is None

    def test_row4_skipped_when_in_home_country(self):
        """Onsite in home country — row 4 must NOT fire; falls to row 5/None."""
        locs = [_onsite("US", city="Seattle")]
        result = compute_location_fit(locs, None, None, ["Remote"], "US")
        # Row 4 skipped (in home country); row 5 city "Seattle" vs ["Remote"] → no match
        assert result is None

    def test_row4_skipped_when_geo_target_matches(self):
        """Even if onsite/foreign, a geo target match in the same posting blocks row 4."""
        # This should not happen in practice (location both foreign AND target?),
        # but the rule says "no target_location matches any city/region" — if it does,
        # row 4 is skipped and row 5 fires.
        locs = [_onsite("GB", city="London", country="United Kingdom")]
        score, reason = compute_location_fit(locs, None, None, ["Remote", "London"], "US")
        # Row 4 check: does any target match London? Yes → row 4 skipped.
        # Row 5: city "london" in target "london" → (5, ...)
        assert score == 5

    def test_row4_hybrid_onsite_outside_home(self):
        locs = [_hybrid("CN")]
        score, reason = compute_location_fit(locs, None, None, ["Remote"], "US")
        assert score == 1
        assert "on-site outside" in reason.lower()

    def test_row4_unspecified_outside_home(self):
        locs = [_loc("UNSPECIFIED", country_code="DE")]
        score, reason = compute_location_fit(locs, None, None, ["Remote"], "US")
        assert score == 1
        assert "on-site outside" in reason.lower()

    def test_row4_no_country_code_skips_row4(self):
        """UNSPECIFIED with no country_code — _country_outside_home returns False."""
        locs = [_loc("UNSPECIFIED")]  # no country_code
        result = compute_location_fit(locs, None, None, ["Remote"], "US")
        # Cannot confirm outside → row 4 not fired → None
        assert result is None


# ---------------------------------------------------------------------------
# Row 5: any city/region/country matches a non-Remote target
# → (5, "on-site/hybrid in target geography")
# ---------------------------------------------------------------------------


class TestRow5GeoTargetMatch:
    def test_city_exact_match(self):
        locs = [_onsite("US", city="San Francisco", region="California")]
        score, reason = compute_location_fit(locs, None, None, ["San Francisco", "Remote"], "US")
        assert score == 5
        assert "target geography" in reason.lower()

    def test_region_match(self):
        locs = [_onsite("US", city="San Jose", region="California")]
        score, reason = compute_location_fit(locs, None, None, ["California"], "US")
        assert score == 5

    def test_country_match(self):
        locs = [_onsite("CA", city="Toronto", country="Canada")]
        score, reason = compute_location_fit(locs, None, None, ["Canada"], "US")
        assert score == 5

    def test_remote_target_excluded_from_geo_matching(self):
        """'Remote' as a target_location must NOT trigger row 5."""
        locs = [_onsite("IN", city="Mumbai")]
        result = compute_location_fit(locs, None, None, ["Remote"], "US")
        # Row 4 fires (onsite, IN ≠ US, no geo match)
        assert result == (1, "on-site outside candidate geography")

    def test_case_insensitive_match(self):
        locs = [_onsite("US", city="new york")]
        score, _ = compute_location_fit(locs, None, None, ["New York"], "US")
        assert score == 5

    def test_substring_match_city_in_target(self):
        locs = [_onsite("US", city="New York")]
        score, _ = compute_location_fit(locs, None, None, ["New York, NY"], "US")
        assert score == 5

    def test_multiple_locs_one_matches(self):
        locs = [_onsite("IN"), _onsite("US", city="Seattle")]
        score, _ = compute_location_fit(locs, None, None, ["Seattle"], "US")
        assert score == 5


# ---------------------------------------------------------------------------
# None — LLM judges
# ---------------------------------------------------------------------------


class TestFallsThrough:
    def test_no_structured_data(self):
        result = compute_location_fit([], None, None, ["Remote"], "US")
        assert result is None

    def test_all_unresolved(self):
        locs = [_loc(unresolved=True), _loc(unresolved=True)]
        result = compute_location_fit(locs, None, None, ["Remote"], "US")
        # All unresolved; no denormalized fallback either
        assert result is None

    def test_onsite_in_home_country_no_target_match(self):
        """Onsite in home country, city not in targets — desirability is judgment."""
        locs = [_onsite("US", city="Omaha")]
        result = compute_location_fit(locs, None, None, ["Remote"], "US")
        assert result is None

    def test_empty_targets(self):
        locs = [_remote()]
        result = compute_location_fit(locs, None, None, [], "US")
        # No 'Remote' in targets → row 1 skipped; no geo targets → row 5 skipped
        assert result is None

    def test_no_home_no_match(self):
        locs = [_onsite("IN")]
        result = compute_location_fit(locs, None, None, ["Remote"], None)
        assert result is None


# ---------------------------------------------------------------------------
# Denormalized-column fallback (empty locations_structured)
# ---------------------------------------------------------------------------


class TestDenormalizedFallback:
    def test_fallback_remote_unrestricted(self):
        """When locations_structured is empty, use workplace_type + primary_country_code."""
        result = compute_location_fit(
            [],
            "REMOTE",  # workplace_type
            None,  # primary_country_code = unrestricted
            ["Remote"],
            "US",
        )
        assert result == (5, "fully remote, remote targeted")

    def test_fallback_onsite_outside_home(self):
        result = compute_location_fit(
            [],
            "ONSITE",
            "IN",
            ["Remote"],
            "US",
        )
        assert result == (1, "on-site outside candidate geography")

    def test_fallback_none_when_no_denorm_data(self):
        result = compute_location_fit([], None, None, ["Remote"], "US")
        assert result is None


# ---------------------------------------------------------------------------
# Multi-location best-wins semantics
# ---------------------------------------------------------------------------


class TestMultiLocation:
    def test_best_wins_one_remote_one_onsite_outside(self):
        """Job offerable in Remote OR Hyderabad — best for candidate is Remote."""
        locs = [_remote(), _onsite("IN")]
        score, _ = compute_location_fit(locs, None, None, ["Remote"], "US")
        assert score == 5

    def test_best_wins_onsite_us_in_targets(self):
        """NYC + Bangalore: NYC matches target → (5, ...)."""
        locs = [_onsite("IN", city="Bengaluru"), _onsite("US", city="New York")]
        score, _ = compute_location_fit(locs, None, None, ["New York", "Remote"], "US")
        assert score == 5

    def test_all_remote_mixed_home_notHome(self):
        """US-remote + IN-remote: row 2 fires (any REMOTE in home)."""
        locs = [_remote("US"), _remote("IN")]
        score, _ = compute_location_fit(locs, None, None, ["Remote"], "US")
        assert score == 5


# ---------------------------------------------------------------------------
# Unresolved entries contribute nothing
# ---------------------------------------------------------------------------


class TestUnresolvedContributesNothing:
    def test_unresolved_plus_resolved_remote(self):
        locs = [_loc(unresolved=True), _remote()]
        score, _ = compute_location_fit(locs, None, None, ["Remote"], "US")
        assert score == 5

    def test_all_unresolved_with_denorm_fallback(self):
        """When all structured are unresolved, denormalized columns kick in."""
        locs = [_loc(unresolved=True)]
        result = compute_location_fit(locs, "ONSITE", "IN", ["Remote"], "US")
        # Denormalized fallback: ONSITE IN vs US home → row 4
        assert result == (1, "on-site outside candidate geography")


# ---------------------------------------------------------------------------
# Orchestrator integration — _apply_location_fit_override
# ---------------------------------------------------------------------------


class TestOrchestratorOverride:
    """Integration tests for the orchestrator's _apply_location_fit_override helper."""

    def _make_assessment(self, location_fit: int = 5):
        from job_finder.db import JobAssessment

        return JobAssessment(
            sub_scores={
                "title_fit": 4,
                "location_fit": location_fit,
                "comp_fit": 4,
                "domain_match": 4,
                "seniority_match": 4,
                "skills_match": 4,
            },
            classification="",
            rationale={
                "strengths": ["good fit"],
                "gaps": [],
                "talking_points": [],
                "resume_priority_skills": [],
            },
            provider="ollama",
        )

    # A JD string that clears the I-13 content-density floor (min 200 chars,
    # no junk prefix). Used by all _seed_job calls that don't override it.
    _STUB_JD = (
        "We are looking for a Senior Data Scientist to join our team. "
        "You will design, build, and operate data and machine learning systems at scale, "
        "partnering with cross-functional teams to ship reliable features end to end. "
        "Requirements: strong Python and SQL skills, hands-on cloud infrastructure, "
        "testing and production observability experience. "
        "Bonus: experience with MLflow, Spark, or distributed training."
    )

    def _seed_job(
        self,
        conn,
        dedup_key: str,
        *,
        locations_structured: str,
        workplace_type: str = "ONSITE",
        primary_country_code: str | None = None,
        jd_full: str | None = None,
    ):
        """Insert a minimal job row with location columns."""
        if jd_full is None:
            jd_full = self._STUB_JD
        conn.execute(
            """INSERT INTO jobs (dedup_key, title, company, location, sources,
               source_urls, source_id, first_seen, last_seen, score,
               score_breakdown, user_interest, jd_full,
               locations_structured, workplace_type, primary_country_code)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                dedup_key,
                "Data Scientist",
                "EY",
                "",
                '["test"]',
                '["https://example.com"]',
                "src-1",
                "2026-01-01T00:00:00",
                "2026-01-01T00:00:00",
                0.0,
                "{}",
                "unreviewed",
                jd_full,
                locations_structured,
                workplace_type,
                primary_country_code,
            ),
        )
        conn.commit()

    def test_override_fires_hyderabad(self, migrated_db):
        """Hyderabad regression: ONSITE IN, targets=[Remote], home=US → (1, ...)."""
        import json

        from job_finder.web.scoring_orchestrator import _apply_location_fit_override

        _path, conn = migrated_db
        dedup_key = "ey-hyderabad-test"
        locs_json = json.dumps(
            [
                {
                    "city": "Hyderābād",
                    "region": "Telangana",
                    "region_code": "TG",
                    "country": "India",
                    "country_code": "IN",
                    "workplace_type": "ONSITE",
                    "raw": "Hyderabad",
                    "unresolved": False,
                }
            ]
        )
        self._seed_job(
            conn,
            dedup_key,
            locations_structured=locs_json,
            workplace_type="ONSITE",
            primary_country_code="IN",
        )

        assessment = self._make_assessment(location_fit=5)  # LLM hallucinated 5
        config = {
            "profile": {"target_locations": ["Remote"], "home_country": "US"},
            "providers": {"primary": "ollama", "fallback_chain": []},
        }

        result = _apply_location_fit_override(assessment, {"dedup_key": dedup_key}, conn, config)
        assert result.sub_scores["location_fit"] == 1
        assert "override" in result.rationale["gaps"][0].lower()

    def test_override_noop_when_llm_correct(self, migrated_db):
        """When override score == LLM score, assessment is unchanged."""
        import json

        from job_finder.web.scoring_orchestrator import _apply_location_fit_override

        _path, conn = migrated_db
        dedup_key = "remote-correct"
        locs_json = json.dumps(
            [
                {
                    "city": None,
                    "region": None,
                    "region_code": None,
                    "country": None,
                    "country_code": None,
                    "workplace_type": "REMOTE",
                    "raw": "Remote",
                    "unresolved": False,
                }
            ]
        )
        self._seed_job(
            conn,
            dedup_key,
            locations_structured=locs_json,
            workplace_type="REMOTE",
            primary_country_code=None,
        )

        assessment = self._make_assessment(location_fit=5)
        config = {
            "profile": {"target_locations": ["Remote"], "home_country": "US"},
            "providers": {"primary": "ollama", "fallback_chain": []},
        }

        result = _apply_location_fit_override(assessment, {"dedup_key": dedup_key}, conn, config)
        # Same score (5==5) → no change, rationale untouched
        assert result is assessment

    def test_override_noop_when_no_facts(self, migrated_db):
        """No locations_structured + no denorm data → no override."""
        import json

        from job_finder.web.scoring_orchestrator import _apply_location_fit_override

        _path, conn = migrated_db
        dedup_key = "no-location-facts"
        self._seed_job(
            conn,
            dedup_key,
            locations_structured=json.dumps([]),
            workplace_type="UNSPECIFIED",
            primary_country_code=None,
        )

        assessment = self._make_assessment(location_fit=3)
        config = {
            "profile": {"target_locations": ["Remote"], "home_country": "US"},
            "providers": {"primary": "ollama", "fallback_chain": []},
        }

        result = _apply_location_fit_override(assessment, {"dedup_key": dedup_key}, conn, config)
        assert result is assessment

    def test_override_noop_when_no_dedup_key(self, migrated_db):
        """Missing dedup_key → safe no-op."""
        from job_finder.web.scoring_orchestrator import _apply_location_fit_override

        _path, conn = migrated_db
        assessment = self._make_assessment(location_fit=5)
        config = {"profile": {"target_locations": ["Remote"], "home_country": "US"}}

        result = _apply_location_fit_override(assessment, {}, conn, config)
        assert result is assessment

    def test_override_amends_gaps_rationale(self, migrated_db):
        """When override fires, the gaps rationale must be prepended."""
        import json

        from job_finder.web.scoring_orchestrator import _apply_location_fit_override

        _path, conn = migrated_db
        dedup_key = "gaps-prepend-test"
        locs_json = json.dumps(
            [
                {
                    "city": None,
                    "region": None,
                    "region_code": None,
                    "country": None,
                    "country_code": "IN",
                    "workplace_type": "ONSITE",
                    "raw": "India",
                    "unresolved": False,
                }
            ]
        )
        self._seed_job(
            conn,
            dedup_key,
            locations_structured=locs_json,
            workplace_type="ONSITE",
            primary_country_code="IN",
        )

        existing_gap = "Some existing gap"
        assessment = self._make_assessment(location_fit=3)
        assessment = type(assessment)(
            sub_scores=assessment.sub_scores,
            classification=assessment.classification,
            rationale={**assessment.rationale, "gaps": [existing_gap]},
            provider=assessment.provider,
        )
        config = {
            "profile": {"target_locations": ["Remote"], "home_country": "US"},
            "providers": {"primary": "ollama", "fallback_chain": []},
        }

        result = _apply_location_fit_override(assessment, {"dedup_key": dedup_key}, conn, config)
        assert result.sub_scores["location_fit"] == 1
        gaps = result.rationale["gaps"]
        assert gaps[0].startswith("[location_fit override P3.1]")
        assert existing_gap in gaps


# ---------------------------------------------------------------------------
# Hyderabad end-to-end classification regression (issue #390 exit criterion)
# ---------------------------------------------------------------------------


class TestHyderabadClassificationRegression:
    """The issue exit criterion: Hyderabad-class jobs can NEVER classify 'apply'.

    EY row facts: ONSITE IN, no remote, targets=[Remote], home=US.
    With the deterministic override → location_fit=1 → derive_classification
    branch "any axis == 1" → "reject".
    """

    def test_hyderabad_classifies_reject(self):
        """Deterministic 1 on location_fit drives derive_classification to 'reject'."""
        from job_finder.db._classification import derive_classification

        # Simulate what score_and_persist_job would do:
        # 1. LLM returns location_fit=5 (hallucinated, historical Hyderabad bug)
        # 2. P3.1 override replaces it with 1
        # 3. derive_classification on the overridden sub_scores
        sub_scores_after_override = {
            "title_fit": 4,
            "location_fit": 1,  # P3.1 deterministic override
            "comp_fit": 4,
            "domain_match": 4,
            "seniority_match": 4,
            "skills_match": 4,
        }
        classification = derive_classification(sub_scores_after_override, None)
        assert classification == "reject", (
            f"Hyderabad job with location_fit=1 must classify 'reject', got {classification!r}"
        )

    def test_hyderabad_historical_llm5_would_apply(self):
        """Confirm the bug we're fixing: historical LLM-5 was 'apply'."""
        from job_finder.db._classification import derive_classification

        sub_scores_llm_only = {
            "title_fit": 4,
            "location_fit": 5,  # historical LLM hallucination
            "comp_fit": 4,
            "domain_match": 4,
            "seniority_match": 4,
            "skills_match": 4,
        }
        classification = derive_classification(sub_scores_llm_only, None)
        assert classification == "apply", "Confirm the pre-fix bug: should have been 'apply'"
