"""Tests for RecipeExtractor — HTML recipe interpreter."""

from job_finder.models import Job
from job_finder.web.autoheal.recipe_extractor import RecipeExtractor
from job_finder.web.autoheal.recipe_schema import FieldRule, HtmlRecipe

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

THREE_JOB_HTML = """
<html><body>
  <div class="job-card">
    <h3 class="title">Software Engineer</h3>
    <a class="link" href="https://example.com/jobs/1">Apply</a>
    <span class="company">Acme Corp</span>
    <span class="location">New York, NY</span>
  </div>
  <div class="job-card">
    <h3 class="title">Product Manager</h3>
    <a class="link" href="https://example.com/jobs/2">Apply</a>
    <span class="company">Beta Inc</span>
    <span class="location">San Francisco, CA</span>
  </div>
  <div class="job-card">
    <h3 class="title">Data Scientist</h3>
    <a class="link" href="https://example.com/jobs/3">Apply</a>
    <span class="company">Gamma LLC</span>
    <span class="location">Remote</span>
  </div>
</body></html>
"""

FULL_RECIPE = HtmlRecipe(
    source="linkedin",
    container_selector="div.job-card",
    fields={
        "title": FieldRule(selector="h3.title", attr="text"),
        "url": FieldRule(selector="a.link", attr="href"),
        "company": FieldRule(selector="span.company", attr="text"),
        "location": FieldRule(selector="span.location", attr="text"),
    },
)

MINIMAL_RECIPE = HtmlRecipe(
    source="glassdoor",
    container_selector="div.job-card",
    fields={
        "title": FieldRule(selector="h3.title", attr="text"),
        "url": FieldRule(selector="a.link", attr="href"),
    },
)

REGEX_RECIPE = HtmlRecipe(
    source="trueup",
    container_selector="div.job-card",
    fields={
        "title": FieldRule(selector="h3.title", attr="text", regex=r"^(\w+)", group=1),
        "url": FieldRule(selector="a.link", attr="href"),
        "company": FieldRule(selector="span.company", attr="text"),
    },
)


# ---------------------------------------------------------------------------
# Core extraction tests
# ---------------------------------------------------------------------------


def test_extracts_three_jobs_with_full_recipe():
    extractor = RecipeExtractor(FULL_RECIPE, job_source="email_recipe")
    jobs = extractor(THREE_JOB_HTML)
    assert len(jobs) == 3
    titles = {j.title for j in jobs}
    assert "Software Engineer" in titles
    assert "Product Manager" in titles
    assert "Data Scientist" in titles


def test_extracts_company_and_location():
    extractor = RecipeExtractor(FULL_RECIPE, job_source="email_recipe")
    jobs = extractor(THREE_JOB_HTML)
    job_map = {j.title: j for j in jobs}
    assert job_map["Software Engineer"].company == "Acme Corp"
    assert job_map["Software Engineer"].location == "New York, NY"


def test_extracts_url_as_href():
    extractor = RecipeExtractor(FULL_RECIPE, job_source="email_recipe")
    jobs = extractor(THREE_JOB_HTML)
    urls = {j.source_url for j in jobs}
    assert "https://example.com/jobs/1" in urls


def test_source_set_to_job_source_param():
    extractor = RecipeExtractor(FULL_RECIPE, job_source="email_recipe")
    jobs = extractor(THREE_JOB_HTML)
    assert all(j.source == "email_recipe" for j in jobs)


def test_minimal_recipe_skips_missing_company_block():
    # No company field in recipe → company defaults to "" → Job.__post_init__ raises
    # → block skipped (Job requires non-empty company). With minimal recipe no company
    # selector exists; those blocks are skipped.
    extractor = RecipeExtractor(MINIMAL_RECIPE, job_source="email_recipe")
    # Minimal recipe has no company field, so all blocks will be skipped since
    # company="" fails Job construction.
    jobs = extractor(THREE_JOB_HTML)
    assert isinstance(jobs, list)
    # All 3 blocks fail because company is empty — verify graceful empty return
    assert len(jobs) == 0


def test_minimal_recipe_with_company_in_html():
    # When company IS in HTML but recipe doesn't map it, it defaults to empty → skipped.
    # Build a recipe that DOES map company but no location:
    recipe = HtmlRecipe(
        source="test",
        container_selector="div.job-card",
        fields={
            "title": FieldRule(selector="h3.title", attr="text"),
            "url": FieldRule(selector="a.link", attr="href"),
            "company": FieldRule(selector="span.company", attr="text"),
        },
    )
    extractor = RecipeExtractor(recipe, job_source="email_recipe")
    jobs = extractor(THREE_JOB_HTML)
    assert len(jobs) == 3
    assert all(j.location == "" for j in jobs)


def test_block_missing_title_element_skips_block():
    html = """
    <div class="job-card">
      <!-- no h3.title here -->
      <a class="link" href="https://example.com/jobs/1">Apply</a>
      <span class="company">Acme Corp</span>
    </div>
    <div class="job-card">
      <h3 class="title">Good Job</h3>
      <a class="link" href="https://example.com/jobs/2">Apply</a>
      <span class="company">Good Corp</span>
    </div>
    """
    extractor = RecipeExtractor(FULL_RECIPE, job_source="email_recipe")
    jobs = extractor(html)
    assert len(jobs) == 1
    assert jobs[0].title == "Good Job"


def test_block_missing_url_element_skips_block():
    html = """
    <div class="job-card">
      <h3 class="title">Software Engineer</h3>
      <!-- no a.link here -->
      <span class="company">Acme Corp</span>
    </div>
    <div class="job-card">
      <h3 class="title">Good Job</h3>
      <a class="link" href="https://example.com/jobs/2">Apply</a>
      <span class="company">Good Corp</span>
    </div>
    """
    extractor = RecipeExtractor(FULL_RECIPE, job_source="email_recipe")
    jobs = extractor(html)
    assert len(jobs) == 1
    assert jobs[0].title == "Good Job"


def test_attr_text_uses_get_text_strip():
    html = """
    <div class="job-card">
      <h3 class="title">  Padded Title  </h3>
      <a class="link" href="https://example.com/jobs/1">Apply</a>
      <span class="company">Acme Corp</span>
    </div>
    """
    extractor = RecipeExtractor(FULL_RECIPE, job_source="email_recipe")
    jobs = extractor(html)
    assert jobs[0].title == "Padded Title"


def test_attr_href_extracts_attribute():
    html = """
    <div class="job-card">
      <h3 class="title">Engineer</h3>
      <a class="link" href="https://jobs.example.com/999">Apply</a>
      <span class="company">Acme Corp</span>
    </div>
    """
    extractor = RecipeExtractor(FULL_RECIPE, job_source="email_recipe")
    jobs = extractor(html)
    assert jobs[0].source_url == "https://jobs.example.com/999"


def test_regex_group_post_processes_extracted_string():
    # REGEX_RECIPE extracts first word of the title
    html = """
    <div class="job-card">
      <h3 class="title">Software Engineer</h3>
      <a class="link" href="https://example.com/jobs/1">Apply</a>
      <span class="company">Acme Corp</span>
    </div>
    """
    extractor = RecipeExtractor(REGEX_RECIPE, job_source="email_recipe")
    jobs = extractor(html)
    assert jobs[0].title == "Software"


def test_empty_html_returns_empty_list():
    extractor = RecipeExtractor(FULL_RECIPE, job_source="email_recipe")
    assert extractor("") == []


def test_garbage_html_returns_empty_list():
    extractor = RecipeExtractor(FULL_RECIPE, job_source="email_recipe")
    assert extractor("not html at all!!! @#$%") == []


def test_none_input_returns_empty_list():
    extractor = RecipeExtractor(FULL_RECIPE, job_source="email_recipe")
    assert extractor(None) == []  # type: ignore[arg-type]


def test_returns_job_objects():
    extractor = RecipeExtractor(FULL_RECIPE, job_source="email_recipe")
    jobs = extractor(THREE_JOB_HTML)
    assert all(isinstance(j, Job) for j in jobs)


def test_regex_no_match_skips_block():
    recipe = HtmlRecipe(
        source="test",
        container_selector="div.job-card",
        fields={
            "title": FieldRule(selector="h3.title", attr="text", regex=r"NOMATCH_(\w+)", group=1),
            "url": FieldRule(selector="a.link", attr="href"),
            "company": FieldRule(selector="span.company", attr="text"),
        },
    )
    extractor = RecipeExtractor(recipe, job_source="email_recipe")
    jobs = extractor(THREE_JOB_HTML)
    # regex doesn't match → title becomes "" → skipped
    assert len(jobs) == 0
