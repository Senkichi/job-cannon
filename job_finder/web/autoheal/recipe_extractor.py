"""RecipeExtractor — HTML recipe interpreter.

A single-arg callable ``(raw_html) -> list[Job]`` that applies a frozen
``HtmlRecipe`` to an HTML email body.  Never raises — invalid/garbage inputs
return ``[]``.  Used as the email override path in Phase C; the existing
``extract_with_fallback`` two-step runs unchanged when no override is present.
"""

from __future__ import annotations

import logging
import re

from bs4 import BeautifulSoup

from job_finder.models import Job
from job_finder.web.autoheal.recipe_schema import FieldRule, HtmlRecipe

logger = logging.getLogger(__name__)


class RecipeExtractor:
    """Apply a declarative HTML recipe to an email body and return Job objects.

    Args:
        recipe: A validated, frozen ``HtmlRecipe`` from ``validate_recipe()``.
        job_source: The ``source`` field written onto every resulting ``Job``.
    """

    def __init__(self, recipe: HtmlRecipe, *, job_source: str) -> None:
        self._recipe = recipe
        self._job_source = job_source

    def __call__(self, raw: object) -> list[Job]:
        """Extract jobs from *raw* HTML.

        Args:
            raw: Raw HTML string (or any object; non-string / empty returns ``[]``).

        Returns:
            List of ``Job`` objects.  Never raises.
        """
        if not raw or not isinstance(raw, str):
            return []
        try:
            return self._extract(raw)
        except Exception:
            logger.warning("RecipeExtractor: unexpected error; returning []", exc_info=True)
            return []

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _extract(self, html: str) -> list[Job]:
        soup = BeautifulSoup(html, "html.parser")
        blocks = soup.select(self._recipe.container_selector)
        jobs: list[Job] = []
        for block in blocks:
            job = self._parse_block(block)
            if job is not None:
                jobs.append(job)
        return jobs

    def _parse_block(self, block) -> Job | None:
        """Parse one container block into a Job, or return None to skip."""
        extracted: dict[str, str] = {}
        for field_name, rule in self._recipe.fields.items():
            value = self._apply_rule(block, rule)
            extracted[field_name] = value

        title = extracted.get("title", "")
        url = extracted.get("url", "")

        # Skip block if required fields are absent after extraction
        if not title or not url:
            return None

        company = extracted.get("company", "")
        location = extracted.get("location", "")

        try:
            return Job(
                title=title,
                company=company,
                location=location,
                source=self._job_source,
                source_url=url,
            )
        except ValueError:
            # Job.__post_init__ raises on empty title or company
            return None

    def _apply_rule(self, block, rule: FieldRule) -> str:
        """Apply a FieldRule to a BeautifulSoup block element.

        Returns the extracted string, or ``""`` if the element/attribute is absent.
        """
        element = block.select_one(rule.selector)
        if element is None:
            return ""

        if rule.attr == "text":
            value = element.get_text(strip=True)
        else:
            value = element.get(rule.attr, "") or ""

        if rule.regex and value:
            match = re.search(rule.regex, value)
            if match:
                try:
                    value = match.group(rule.group)
                except IndexError:
                    value = ""
            else:
                value = ""

        return value or ""
