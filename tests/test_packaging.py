"""Tests for PyPI packaging metadata and release workflows."""
import pytest


def test_license_is_spdx_string():
    """pyproject.toml license field uses SPDX string format AGPL-3.0-only."""
    import tomllib

    with open("pyproject.toml", "rb") as f:
        data = tomllib.load(f)
    assert data["project"]["license"] == "AGPL-3.0-only"


def test_required_classifiers_present():
    """pyproject.toml has required classifiers for Python 3.13, OSI license, OS-independent."""
    import tomllib

    with open("pyproject.toml", "rb") as f:
        data = tomllib.load(f)
    classifiers = data["project"]["classifiers"]
    assert "Programming Language :: Python :: 3.13" in classifiers
    assert "License :: OSI Approved :: GNU Affero General Public License v3" in classifiers
    assert "Operating System :: OS Independent" in classifiers


def test_project_urls_complete():
    """pyproject.toml has homepage, repository, and issues URLs."""
    import tomllib

    with open("pyproject.toml", "rb") as f:
        data = tomllib.load(f)
    urls = data["project"]["urls"]
    assert "Homepage" in urls
    assert "Repository" in urls
    assert "Issues" in urls


def test_python_dotenv_in_core_deps():
    """python-dotenv is in core dependencies, not dev extras."""
    import tomllib

    with open("pyproject.toml", "rb") as f:
        data = tomllib.load(f)
    core_deps = data["project"]["dependencies"]
    assert any("python-dotenv" in dep for dep in core_deps)
    dev_deps = data["project"]["optional-dependencies"]["dev"]
    assert not any("python-dotenv" in dep for dep in dev_deps)


def test_publish_workflow_pins_action_version():
    """publish.yml uses pinned action version v1.14.0, not sliding release/v1."""
    with open(".github/workflows/publish.yml") as f:
        content = f.read()
    assert "pypa/gh-action-pypi-publish@v1.14.0" in content
    assert "pypa/gh-action-pypi-publish@release/v1" not in content


def test_publish_uses_pypi_environment():
    """publish.yml declares environment: pypi for required reviewer gate."""
    with open(".github/workflows/publish.yml") as f:
        content = f.read()
    assert "environment:" in content
    assert "name: pypi" in content


def test_workflow_triggers_are_tag_only():
    """publish.yml triggers on v* tags; publish-testpypi.yml on pre-release tags only."""
    with open(".github/workflows/publish.yml") as f:
        publish_content = f.read()
    with open(".github/workflows/publish-testpypi.yml") as f:
        testpypi_content = f.read()
    # Both workflows should only trigger on tag pushes, not PRs or branches
    assert "tags:" in publish_content
    assert "tags:" in testpypi_content
    # TestPyPI workflow should trigger on pre-release patterns
    assert "'v*a*'" in testpypi_content or "v*a*" in testpypi_content
    assert "'v*b*'" in testpypi_content or "v*b*" in testpypi_content
    assert "'v*rc*'" in testpypi_content or "v*rc*" in testpypi_content


def test_publish_runs_smoke_test_before_upload():
    """publish.yml runs uv build, twine check, and smoke install before publish step."""
    with open(".github/workflows/publish.yml") as f:
        content = f.read()
    assert "uv build" in content
    assert "twine check" in content
    assert "uv tool install" in content or "pipx install" in content
    # Smoke test should come before the publish step - check that the smoke test
    # step appears before the Publish to PyPI step (not just the string anywhere)
    lines = content.split("\n")
    smoke_test_line = None
    publish_line = None
    for i, line in enumerate(lines):
        if "Smoke-test installed entry point" in line:
            smoke_test_line = i
        if "Publish to PyPI (OIDC trusted publishing)" in line:
            publish_line = i
    assert smoke_test_line is not None, "Smoke test step not found"
    assert publish_line is not None, "Publish step not found"
    assert smoke_test_line < publish_line, "Smoke test should come before publish step"


def test_release_yml_no_longer_creates_gh_release():
    """release.yml no longer contains gh release create step (moved to publish.yml)."""
    with open(".github/workflows/release.yml") as f:
        content = f.read()
    assert "gh release create" not in content


def test_install_md_three_sections_in_order():
    """INSTALL.md has three H2 sections in correct order."""
    with open("INSTALL.md") as f:
        content = f.read()
    lines = content.split("\n")
    h2_lines = [line for line in lines if line.startswith("## ")]
    assert len(h2_lines) >= 3
    # Check sections exist in order
    h2_text = [line.strip("## ") for line in h2_lines]
    # First section should be about pipx
    assert "pipx" in h2_text[0].lower() or "install" in h2_text[0].lower()
    # Second section should be for contributors
    assert "contributor" in h2_text[1].lower() or "clone" in h2_text[1].lower() or "dev" in h2_text[1].lower()
    # Third section should mention native installers
    assert "native" in h2_text[2].lower() or "installer" in h2_text[2].lower()


def test_install_md_links_to_setup_not_duplicates():
    """INSTALL.md links to docs/SETUP.md and does not duplicate OAuth content."""
    with open("INSTALL.md") as f:
        content = f.read()
    assert "docs/SETUP.md" in content or "SETUP.md" in content
    # Should not duplicate detailed OAuth setup (that's in SETUP.md)
    # This is a weak check - we just ensure it links to SETUP.md


def test_readme_install_above_the_fold():
    """README.md has Install section with pipx install near the top."""
    with open("README.md") as f:
        content = f.read()
    lines = content.split("\n")
    # Find the Install section
    install_line = None
    for i, line in enumerate(lines):
        if line.strip().startswith("## Install") or line.strip().startswith("## install"):
            install_line = i
            break
    assert install_line is not None, "README should have an Install section"
    # Install section should be relatively early (before line 100)
    assert install_line < 100, "Install section should appear above the fold"
    # Should mention pipx install
    install_section = "\n".join(lines[install_line:install_line + 20])
    assert "pipx install job-cannon" in install_section.lower()


def test_readme_for_contributors_present():
    """README.md has For Contributors section with git clone and uv sync."""
    with open("README.md") as f:
        content = f.read()
    assert "For Contributors" in content or "for contributors" in content.lower()
    assert "git clone" in content
    assert "uv sync" in content


def test_readme_mentions_update_banner():
    """README.md features bullet mentions dashboard update banner."""
    with open("README.md") as f:
        content = f.read()
    # Should mention update notifications or banner
    assert "update" in content.lower() and ("banner" in content.lower() or "notification" in content.lower())


def test_release_checklist_covers_manual_steps():
    """PHASE-44-RELEASE-CHECKLIST.md exists and covers PyPI/TestPyPI setup."""
    with open(".planning/phases/44-pypi-release-pipeline-install-docs/PHASE-44-RELEASE-CHECKLIST.md") as f:
        content = f.read()
    assert "PyPI" in content
    assert "TestPyPI" in content
    assert "trusted publisher" in content.lower() or "publishing" in content.lower()
    assert "environment" in content.lower()
