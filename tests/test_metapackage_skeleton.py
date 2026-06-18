"""Skeleton checks for the `jobcannon` typo-squat alias metapackage.

The metapackage lives in `packaging/metapackage/` and ships no code of its own
— only metadata that makes `pip install jobcannon` pull the real `job-cannon`
package. These tests parse its `pyproject.toml` and guard the invariants that
keep the alias correct (name, sole dependency, version-in-sync, backend, and a
present README for `twine check --strict`).
"""

import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

_REPO_ROOT = Path(__file__).resolve().parents[1]
_META_DIR = _REPO_ROOT / "packaging" / "metapackage"
_META_PYPROJECT = _META_DIR / "pyproject.toml"
_ROOT_PYPROJECT = _REPO_ROOT / "pyproject.toml"


def _load(path: Path) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def test_metapackage_name_is_jobcannon():
    """The alias distribution is named `jobcannon` (the no-hyphen misspelling)."""
    data = _load(_META_PYPROJECT)
    assert data["project"]["name"] == "jobcannon"


def test_metapackage_sole_dependency_is_job_cannon():
    """`job-cannon` is the one and only dependency (normalized)."""
    data = _load(_META_PYPROJECT)
    deps = data["project"]["dependencies"]
    assert len(deps) == 1, f"expected exactly one dependency, got {deps!r}"
    name = canonicalize_name(Requirement(deps[0]).name)
    assert name == "job-cannon"


def test_metapackage_version_matches_real_package():
    """Alias version is pinned to the real package's version (no silent drift)."""
    meta = _load(_META_PYPROJECT)
    root = _load(_ROOT_PYPROJECT)
    assert meta["project"]["version"] == root["project"]["version"]


def test_metapackage_uses_hatchling_backend():
    """Mirror the host project's build backend choice."""
    data = _load(_META_PYPROJECT)
    assert data["build-system"]["build-backend"] == "hatchling.build"


def test_metapackage_readme_exists():
    """The `readme` referenced in metadata must exist for twine --strict."""
    data = _load(_META_PYPROJECT)
    readme = data["project"]["readme"]
    assert (_META_DIR / readme).is_file()
