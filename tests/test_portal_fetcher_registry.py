"""Guard: every portal _fetch_* is wired into the _PORTAL_FETCHERS roster.

fetch_all_portals used to hand-dispatch each portal fetcher inline; a new
_fetch_* that someone forgot to add would silently never run ("dark fetcher")
— the same failure class this whole effort targets. The roster is now the
single source of truth and this test fails if a defined portal fetcher is
missing from it.
"""

from __future__ import annotations

import inspect

from job_finder.sources import portal_search_source as ps


def test_no_dark_portal_fetcher():
    """Every module-level _fetch_* portal fetcher appears in the roster."""
    registered = {pf.fetch for pf in ps._PORTAL_FETCHERS}
    defined = {
        obj
        for name, obj in vars(ps).items()
        if name.startswith("_fetch_") and inspect.isfunction(obj) and obj.__module__ == ps.__name__
    }
    unwired = defined - registered
    assert not unwired, (
        "portal _fetch_* functions defined but not wired into _PORTAL_FETCHERS "
        f"(they would never run): {sorted(f.__name__ for f in unwired)}"
    )


def test_roster_entries_point_at_real_fetchers():
    """Reverse direction: every roster entry points at a real module fetcher."""
    for pf in ps._PORTAL_FETCHERS:
        assert inspect.isfunction(pf.fetch)
        assert pf.fetch.__name__.startswith("_fetch_")


def test_always_on_portals_are_keyless():
    """Tier-1a always-on portals (config_key None) must take no extra kwargs;
    a keyed fetcher mis-marked always-on would call its API unconfigured every
    run."""
    for pf in ps._PORTAL_FETCHERS:
        if pf.config_key is None:
            assert pf.kwargs_from_cfg is None, (
                f"{pf.fetch.__name__} is always-on but declares kwargs_from_cfg"
            )
