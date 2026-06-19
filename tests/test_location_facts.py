"""Tests for the v3.1 Location facts renderer (P3.3, D-6).

``render_location_facts_line`` is a pure function: structured-location inputs in,
one ``Location facts: …`` line out. The ``candidate-geography-match`` token must
agree with ``compute_location_fit`` (yes for a decisive >=4 verdict, no for a
decisive <=2 verdict, unknown when the facts are undecided).
"""

from __future__ import annotations

from job_finder.web.location_fit import resolve_targets_and_home
from job_finder.web.scoring_prompts.location_facts import render_location_facts_line


def _loc(**kw) -> dict:
    base = {
        "city": None,
        "region": None,
        "region_code": None,
        "country": None,
        "country_code": None,
        "workplace_type": "UNSPECIFIED",
        "raw": "",
        "unresolved": False,
    }
    base.update(kw)
    return base


class TestGeographyMatchToken:
    def test_remote_unrestricted_remote_target_is_yes(self):
        line = render_location_facts_line(
            locations_structured=[_loc(workplace_type="REMOTE")],
            workplace_type="REMOTE",
            primary_country_code=None,
            target_locations=["remote"],
            home_country="US",
        )
        assert "candidate-geography-match=yes" in line
        assert "workplace=REMOTE" in line

    def test_onsite_foreign_country_is_no(self):
        """Hyderabad-class: ONSITE IN, home US, targets Remote-only → row 4 → no."""
        line = render_location_facts_line(
            locations_structured=[
                _loc(city="Hyderabad", country="India", country_code="IN", workplace_type="ONSITE")
            ],
            workplace_type="ONSITE",
            primary_country_code="IN",
            target_locations=["remote"],
            home_country="US",
        )
        assert "candidate-geography-match=no" in line
        assert "cities=[Hyderabad]" in line
        assert "country=India" in line

    def test_onsite_home_city_not_in_targets_is_unknown(self):
        """Onsite in home country but not a named target → compute returns None → unknown."""
        line = render_location_facts_line(
            locations_structured=[
                _loc(
                    city="Austin",
                    country="United States",
                    country_code="US",
                    workplace_type="ONSITE",
                )
            ],
            workplace_type="ONSITE",
            primary_country_code="US",
            target_locations=["remote"],
            home_country="US",
        )
        assert "candidate-geography-match=unknown" in line

    def test_geo_target_match_is_yes(self):
        line = render_location_facts_line(
            locations_structured=[
                _loc(
                    city="San Francisco",
                    country="United States",
                    country_code="US",
                    workplace_type="ONSITE",
                )
            ],
            workplace_type="ONSITE",
            primary_country_code="US",
            target_locations=["San Francisco"],
            home_country="US",
        )
        assert "candidate-geography-match=yes" in line


class TestFieldRendering:
    def test_unresolved_entries_excluded(self):
        line = render_location_facts_line(
            locations_structured=[
                _loc(city="Ghost", country="Nowhere", unresolved=True),
                _loc(city="Berlin", country="Germany", country_code="DE", workplace_type="ONSITE"),
            ],
            workplace_type=None,
            primary_country_code=None,
            target_locations=["remote"],
            home_country="US",
        )
        assert "Ghost" not in line
        assert "cities=[Berlin]" in line

    def test_empty_structured_uses_denorm_fallback(self):
        line = render_location_facts_line(
            locations_structured=[],
            workplace_type="REMOTE",
            primary_country_code="US",
            target_locations=["remote"],
            home_country="US",
        )
        assert "cities=[(none)]" in line
        assert "country=US" in line
        assert "workplace=REMOTE" in line

    def test_multi_city_dedup_and_join(self):
        line = render_location_facts_line(
            locations_structured=[
                _loc(
                    city="New York",
                    country="United States",
                    country_code="US",
                    workplace_type="HYBRID",
                ),
                _loc(
                    city="New York",
                    country="United States",
                    country_code="US",
                    workplace_type="HYBRID",
                ),
                _loc(
                    city="Boston",
                    country="United States",
                    country_code="US",
                    workplace_type="ONSITE",
                ),
            ],
            workplace_type=None,
            primary_country_code=None,
            target_locations=["remote"],
            home_country="US",
        )
        assert "cities=[New York, Boston]" in line
        # Distinct workplace types are joined with '|'.
        assert "workplace=HYBRID|ONSITE" in line

    def test_line_is_single_line(self):
        line = render_location_facts_line(
            locations_structured=[_loc(workplace_type="REMOTE")],
            workplace_type="REMOTE",
            primary_country_code=None,
            target_locations=["remote"],
            home_country=None,
        )
        assert "\n" not in line
        assert line.startswith("Location facts:")


class TestResolveTargetsAndHome:
    def test_remote_work_arrangement_synthesizes_sentinel(self):
        targets, home = resolve_targets_and_home(
            {
                "profile": {
                    "work_arrangement": "remote",
                    "target_locations": ["NYC"],
                    "home_country": "US",
                }
            }
        )
        assert targets[0] == "remote"
        assert "NYC" in targets
        assert home == "US"

    def test_no_synthesis_when_remote_already_present(self):
        targets, _ = resolve_targets_and_home(
            {"profile": {"work_arrangement": "remote", "target_locations": ["Remote", "NYC"]}}
        )
        # No duplicate "remote" prepended.
        assert targets == ["Remote", "NYC"]

    def test_empty_config_returns_empty(self):
        targets, home = resolve_targets_and_home({})
        assert targets == []
        assert home is None
