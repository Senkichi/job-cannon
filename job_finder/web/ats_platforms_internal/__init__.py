"""ATS platform scanner registry (added in polish-review F1).

This package is the new home for per-platform scanner code. During F1 it
coexists with the flat ``job_finder/web/ats_platforms.py`` module, which
delegates to ``run_platform_scan`` for the 12 known ATS platforms.

The "internal" suffix is temporary — it avoids the file/package name clash
with ``ats_platforms.py`` during the two-commit F1 transition. A later
optional commit may rename to ``ats_platforms/`` (package-promote).
"""

from job_finder.web.ats_platforms_internal._platforms_ashby import SCANNER as _ASHBY
from job_finder.web.ats_platforms_internal._platforms_bamboohr import SCANNER as _BAMBOOHR
from job_finder.web.ats_platforms_internal._platforms_breezy import SCANNER as _BREEZY
from job_finder.web.ats_platforms_internal._platforms_greenhouse import SCANNER as _GREENHOUSE
from job_finder.web.ats_platforms_internal._platforms_jazzhr import SCANNER as _JAZZHR
from job_finder.web.ats_platforms_internal._platforms_jobvite import SCANNER as _JOBVITE
from job_finder.web.ats_platforms_internal._platforms_lever import SCANNER as _LEVER
from job_finder.web.ats_platforms_internal._platforms_paylocity import SCANNER as _PAYLOCITY
from job_finder.web.ats_platforms_internal._platforms_personio import SCANNER as _PERSONIO
from job_finder.web.ats_platforms_internal._platforms_pinpoint import SCANNER as _PINPOINT
from job_finder.web.ats_platforms_internal._platforms_recruitee import SCANNER as _RECRUITEE
from job_finder.web.ats_platforms_internal._platforms_rippling import SCANNER as _RIPPLING
from job_finder.web.ats_platforms_internal._platforms_smartrecruiters import (
    SCANNER as _SMARTRECRUITERS,
)
from job_finder.web.ats_platforms_internal._platforms_teamtailor import SCANNER as _TEAMTAILOR
from job_finder.web.ats_platforms_internal._platforms_workable import SCANNER as _WORKABLE
from job_finder.web.ats_platforms_internal._platforms_workday import SCANNER as _WORKDAY
from job_finder.web.ats_platforms_internal._registry import PlatformScanner

SCANNERS_BY_NAME: dict[str, PlatformScanner] = {
    s.name: s
    for s in (
        _ASHBY,
        _BAMBOOHR,
        _BREEZY,
        _GREENHOUSE,
        _JAZZHR,
        _JOBVITE,
        _LEVER,
        _PAYLOCITY,
        _PERSONIO,
        _PINPOINT,
        _RECRUITEE,
        _RIPPLING,
        _SMARTRECRUITERS,
        _TEAMTAILOR,
        _WORKABLE,
        _WORKDAY,
    )
}

__all__ = ["SCANNERS_BY_NAME", "PlatformScanner"]
