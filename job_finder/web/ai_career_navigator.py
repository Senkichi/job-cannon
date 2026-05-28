"""AI-navigated careers page crawler — discover once, replay forever.

Two-phase architecture:
- Phase A (Discovery): the quick-tier model interprets a page's
  accessibility snapshot and produces a navigation recipe — an ordered
  list of Playwright actions that lead to job listings. One-time cost
  per company (typically $0 — Ollama and the Anthropic CLI both bill
  $0 per call).
- Phase B (Replay): Execute the cached recipe mechanically via Playwright
  locators. Zero AI cost.

The recipe is cached as JSON on the companies table (careers_nav_recipe column).
If replay fails (stale layout), the recipe is re-discovered automatically.
"""

import json
import logging
from datetime import datetime

from job_finder.web.claude_client import call_claude
from job_finder.web.db_helpers import standalone_connection
from job_finder.web.model_provider import ProviderCascadeExhaustedError, call_model

logger = logging.getLogger(__name__)


# Stop words excluded when deriving a search term from target titles
_TITLE_STOP_WORDS = frozenset(
    {
        "lead",
        "senior",
        "staff",
        "principal",
        "head",
        "director",
        "manager",
        "junior",
        "associate",
        "intern",
        "of",
        "the",
        "and",
        "for",
        "in",
        "at",
        "i",
        "ii",
        "iii",
        "iv",
        "v",
    }
)


def _derive_search_term(target_titles: list[str]) -> str:
    """Extract a broad single-word search term from target titles.

    Career page search engines return more results with simple terms like
    "analyst" or "scientist" than with full titles like "Lead Product Analyst".

    Returns the most frequently occurring non-stop-word across all titles.
    Falls back to "data analyst" if nothing useful can be extracted.
    """
    from collections import Counter

    word_counts: Counter = Counter()
    for title in target_titles:
        words = title.lower().split()
        for word in words:
            if word not in _TITLE_STOP_WORDS and len(word) > 2:
                word_counts[word] += 1

    if word_counts:
        return word_counts.most_common(1)[0][0]
    return "data analyst"


_MAX_RECIPE_STEPS = 8
_STEP_TIMEOUT_MS = 5000
_POST_ACTION_WAIT_MS = 1500

# Step fields that can carry a {keyword} placeholder for runtime substitution.
# url: lets goto / goto_with_query templatize the destination (path-segment
# search like /search-jobs/{keyword} as well as the URL-param case).
# value: form-fill text on type / goto_with_query.
_KEYWORD_PLACEHOLDER_FIELDS = ("value", "url")


def _substitute_keyword(step: dict, keyword: str) -> dict:
    """Return a copy of ``step`` with ``{keyword}`` replaced in templated fields.

    Centralizes the placeholder rule so discovery's validation and replay's
    execution both agree on which fields can carry ``{keyword}`` — avoids
    the round-14 class of bug where the two paths used different
    substitution rules.
    """
    out = step
    for field in _KEYWORD_PLACEHOLDER_FIELDS:
        val = step.get(field)
        if isinstance(val, str) and "{keyword}" in val:
            out = {**out, field: val.replace("{keyword}", keyword)}
    return out


class RecipeStaleError(Exception):
    """Raised when a cached navigation recipe can no longer be replayed."""


# ---------------------------------------------------------------------------
# Snapshot helpers
# ---------------------------------------------------------------------------


def _take_snapshot(page) -> str:
    """Take an accessibility snapshot augmented with page links.

    Combines the accessibility tree (for interactive elements like search
    boxes and buttons) with a curated list of links with their URLs (so
    the discovery model can produce "goto" steps to navigate to job
    search pages).

    Truncates to ~4000 chars to keep the prompt cheap on Ollama / quick-tier.

    Args:
        page: Playwright Page instance (already navigated).

    Returns:
        Combined text: accessibility tree + links with hrefs.
    """
    # Part 1: Accessibility tree
    a11y_text = ""
    try:
        tree = page.accessibility.snapshot()
        if tree:
            lines: list[str] = []
            _flatten_a11y_node(tree, lines, depth=0)
            a11y_text = "\n".join(lines)
    except Exception:
        pass

    if not a11y_text:
        a11y_text = page.evaluate("() => document.body.innerText.substring(0, 2000)")

    # Part 2: Extract links with hrefs (critical for goto discovery)
    try:
        links = page.evaluate("""() => {
            const links = document.querySelectorAll('a[href]');
            // Priority keywords (job search portals)
            const highPri = ['job search', 'search jobs', 'view jobs', 'view all jobs',
                'open positions', 'open roles', 'browse jobs', 'find jobs',
                'see all jobs', 'all openings', 'job openings', 'browse openings'];
            const keywords = ['job', 'career', 'search', 'opening', 'position',
                'vacancy', 'apply', 'hiring', 'opportunity', 'workday'];
            const all = Array.from(links)
                .filter(a => {
                    const combined = (a.innerText + ' ' + a.href).toLowerCase();
                    return keywords.some(k => combined.includes(k))
                        && a.innerText.trim().length > 2
                        && a.innerText.trim().length < 80
                        && !a.href.includes('#');
                })
                .map(a => ({
                    text: a.innerText.trim(),
                    href: a.href,
                    priority: highPri.some(k => a.innerText.trim().toLowerCase().includes(k)) ? 0 : 1
                }));
            // Sort high-priority first, then alphabetical
            all.sort((a, b) => a.priority - b.priority || a.text.localeCompare(b.text));
            return all.slice(0, 20).map(l => `${l.text} -> ${l.href}`);
        }""")
        if links:
            links_section = "\n\nLinks on this page:\n" + "\n".join(links)
        else:
            links_section = ""
    except Exception:
        links_section = ""

    combined = a11y_text[:2500] + links_section
    return combined[:4000]


def _flatten_a11y_node(node: dict, lines: list, depth: int) -> None:
    """Recursively flatten an accessibility tree node into readable lines."""
    if depth > 6:
        return

    role = node.get("role", "")
    name = node.get("name", "")
    value = node.get("value", "")

    # Skip generic/noise nodes
    if role in ("generic", "none", "presentation") and not name:
        for child in node.get("children", []):
            _flatten_a11y_node(child, lines, depth)
        return

    indent = "  " * depth
    parts = [role]
    if name:
        parts.append(f'"{name}"')
    if value:
        parts.append(f"value={value}")
    # Include URL for links so the discovery model can produce goto steps
    url = node.get("url", "")
    if url and role == "link":
        parts.append(f"href={url}")

    lines.append(f"{indent}{' '.join(parts)}")

    for child in node.get("children", []):
        _flatten_a11y_node(child, lines, depth + 1)


# ---------------------------------------------------------------------------
# Pre-discovery wait
# ---------------------------------------------------------------------------


def wait_for_snapshot_ready(
    page,
    *,
    timeout_ms: int = 8000,
    poll_ms: int = 500,
    min_chars: int = 50,
) -> int:
    """Poll the accessibility snapshot until it grows past ``min_chars``.

    SPA careers pages (e.g. AMD's careers-home/jobs) frequently render
    nothing useful within the first 2s after ``domcontentloaded``, which
    historically caused ``discover_navigation_recipe`` to bail at its
    50-char snapshot guard. This helper replaces the fixed-duration wait
    with a polling loop so slow SPAs get a chance to settle while fast
    pages still proceed immediately.

    Args:
        page: Playwright Page instance.
        timeout_ms: Total budget for polling (default 8s).
        poll_ms: Interval between checks (default 500ms).
        min_chars: Snapshot length considered "ready" (default 50, the
            same threshold ``discover_navigation_recipe`` uses).

    Returns:
        The final snapshot length observed. Caller can decide whether to
        proceed (length >= min_chars) or bail.
    """
    elapsed = 0
    snap_len = 0
    while elapsed < timeout_ms:
        try:
            snap = _take_snapshot(page)
            snap_len = len(snap or "")
        except Exception:
            snap_len = 0
        if snap_len >= min_chars:
            return snap_len
        page.wait_for_timeout(poll_ms)
        elapsed += poll_ms
    return snap_len


# ---------------------------------------------------------------------------
# Recipe execution
# ---------------------------------------------------------------------------


def _execute_step(page, step: dict) -> bool:
    """Execute a single navigation recipe step via Playwright.

    Args:
        page: Playwright Page instance.
        step: Recipe step dict with 'action' key and action-specific params.

    Returns:
        True if the step executed successfully, False on failure.
    """
    action = step.get("action", "")

    try:
        if action == "goto":
            url = step.get("url", "")
            if url:
                # ``domcontentloaded`` over ``networkidle`` so analytics-heavy
                # SPAs (jobvite's TalentNetwork iframe, embedded chat widgets,
                # session-keepalive XHRs) don't blow the 15s deadline before
                # the DOM is ready to extract from. The fixed 2s settle wait
                # below covers any client-side render that would otherwise
                # benefit from a true idle gate.
                page.goto(url, timeout=15000, wait_until="domcontentloaded")
                page.wait_for_timeout(2000)
            else:
                return False

        elif action == "goto_with_query":
            # URL-param search: navigate to a base URL with a query-string
            # parameter set to the search keyword. Used when a careers site
            # exposes its search via URL (e.g. /search?q=analyst) rather
            # than a form input. The {keyword} placeholder in `value` is
            # substituted upstream in replay/discovery — by the time we
            # see it here, it's already a literal search term.
            url = step.get("url", "")
            query_param = step.get("query_param", "")
            value = step.get("value", "")
            if not (url and query_param):
                return False
            from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

            parsed = urlparse(url)
            params = dict(parse_qsl(parsed.query, keep_blank_values=True))
            params[query_param] = value
            new_url = urlunparse(parsed._replace(query=urlencode(params)))
            # See goto rationale above — same fix for the same SPA-polling
            # failure mode (jobvite hosted-careers tenants in particular).
            page.goto(new_url, timeout=15000, wait_until="domcontentloaded")
            page.wait_for_timeout(2000)

        elif action == "click":
            role = step.get("role", "button")
            name = step.get("name", "")
            locator = page.get_by_role(role, name=name)
            locator.first.click(timeout=_STEP_TIMEOUT_MS)

        elif action == "type":
            role = step.get("role", "textbox")
            name = step.get("name", "")
            value = step.get("value", "")
            locator = page.get_by_role(role, name=name)
            locator.first.fill(value, timeout=_STEP_TIMEOUT_MS)

        elif action == "wait":
            seconds = step.get("seconds", 1)
            page.wait_for_timeout(int(seconds * 1000))

        elif action == "press":
            key = step.get("key", "Enter")
            page.keyboard.press(key)

        else:
            logger.debug("Unknown recipe action: %s", action)
            return False

        return True

    except Exception as e:
        logger.debug("Recipe step failed: %s — %s", step, e)
        return False


def _extract_with_recipe(
    page,
    extraction: dict,
    target_titles: list[str],
    exclusions: list[str],
) -> list[dict]:
    """Extract job listings from the current page state using recipe instructions.

    Uses the full _extract_jobs_from_soup function from careers_crawler (handles
    JSON-LD structured data and link text matching) rather than simple <a> tag
    scanning.

    Args:
        page: Playwright Page instance (in post-navigation state).
        extraction: Recipe extraction config dict.
        target_titles: Target title keywords for filtering.
        exclusions: Exclusion keywords.

    Returns:
        List of job dicts with 'title', 'url', 'description' keys.
    """
    from bs4 import BeautifulSoup

    from job_finder.web.careers_crawler import _extract_jobs_from_soup

    html = page.content()
    soup = BeautifulSoup(html, "html.parser")
    return _extract_jobs_from_soup(soup, page.url, target_titles, exclusions)


# ---------------------------------------------------------------------------
# Discovery (quick-tier model, first visit only)
# ---------------------------------------------------------------------------


_DISCOVERY_SYSTEM = """You are a web navigation expert. Given an accessibility snapshot of a careers page, produce a JSON navigation recipe that leads to job listings.

The recipe is a JSON object with:
- "steps": array of action objects to execute in order
- "extraction": object describing how to find job links after navigation

Action types:
- {"action": "goto", "url": "<full URL>"} — navigate to a different page (e.g. the "Job Search" link). The URL may contain a {keyword} placeholder for sites that put the search term in the URL PATH (e.g. /search-jobs/{keyword}, /SearchJobs/{keyword}).
- {"action": "goto_with_query", "url": "<base URL>", "query_param": "<param name>", "value": "{keyword}"}
  Navigate to a URL with a search query string appended. Use when the site exposes job search via URL params (e.g. /search?q=analyst, /jobs?keyword=analyst).
  Examples:
    {"action": "goto_with_query", "url": "https://jobs.example.com/search", "query_param": "q", "value": "{keyword}"}
    {"action": "goto_with_query", "url": "https://example.com/careers/search-jobs", "query_param": "search", "value": "{keyword}"}
- {"action": "type", "role": "textbox", "name": "<accessible name>", "value": "{keyword}"}
  The {keyword} placeholder will be replaced with actual search terms at runtime.
- {"action": "click", "role": "<role>", "name": "<accessible name>"}
- {"action": "press", "key": "Enter"}
- {"action": "wait", "seconds": 2}

Extraction:
- {"method": "links_in_page"} — extract all matching links from the final page state

Rules:
- Use EXACT role and name values from the accessibility snapshot
- Keep recipes short (1-6 steps)
- IMPORTANT: Many career pages are LANDING pages, not job search pages. Look for a "Job Search", "View Jobs", "Open Positions", "Search Jobs", or similar link. If you find one, use a "goto" step to navigate there first, then search/extract on that page.
- PREFER goto_with_query when a search-by-URL pattern is obvious (e.g. the landing page has a "Search jobs" link/button that points to a /search or /jobs endpoint). URL-param search is more reliable than form-filling because no accessible-name guessing is needed. Common query param names: q, keyword, keywords, search, query, search_keywords.
- If the page already shows individual job listings, return empty steps
- If you cannot determine how to navigate to job listings, return null"""


def discover_navigation_recipe(
    page,
    careers_url: str,
    target_titles: list[str],
    config: dict,
    max_steps: int = _MAX_RECIPE_STEPS,
) -> dict | None:
    """Use the quick-tier model to discover a navigation recipe for a careers page.

    Takes an accessibility snapshot of the loaded page, sends it to the
    quick-tier model with instructions to produce a navigation recipe,
    then validates the recipe by executing it and checking for results.

    Args:
        page: Playwright Page instance (already navigated to careers_url).
        careers_url: The careers page URL.
        target_titles: Target title keywords (for search form filling).
        config: Application config dict.
        max_steps: Maximum allowed steps in the recipe.

    Returns:
        Validated recipe dict, or None if discovery failed.
    """
    # Pre-check: if the page already has extractable jobs, skip the discovery call entirely
    pre_jobs = _extract_with_recipe(
        page,
        {"method": "links_in_page"},
        target_titles,
        [],
    )
    if pre_jobs:
        logger.info(
            "ai_nav: page already has %d jobs for %s — empty recipe (no AI needed)",
            len(pre_jobs),
            careers_url,
        )
        return {
            "version": 1,
            "discovered_at": datetime.now().isoformat(),
            "steps": [],
            "extraction": {"method": "links_in_page"},
        }

    snapshot_text = _take_snapshot(page)
    if not snapshot_text or len(snapshot_text) < 50:
        logger.debug("ai_nav: snapshot too short for %s", careers_url)
        return None

    # Build the prompt with a broad search term for the {keyword} placeholder
    search_term = _derive_search_term(target_titles)
    user_message = (
        f"Here is the accessibility snapshot of {careers_url}:\n\n"
        f"{snapshot_text}\n\n"
        f"I want to find job listings. The search term to use is: {search_term}\n"
        f"If there's a search box, use {{keyword}} as the placeholder (it will be "
        f'replaced with "{search_term}" at runtime).\n'
        f"Produce a navigation recipe JSON to find job listings on this page."
    )

    # Dispatch through call_model when providers.low is configured; fall
    # back to direct call_claude otherwise or when the cascade is exhausted.
    try:
        db_path = config.get("db_path", "jobs.db")
        with standalone_connection(db_path) as conn:
            recipe_schema = {
                "type": "object",
                "properties": {
                    "steps": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "action": {"type": "string"},
                                "url": {"type": "string"},
                                "role": {"type": "string"},
                                "name": {"type": "string"},
                                "value": {"type": "string"},
                                "key": {"type": "string"},
                                "seconds": {"type": "number"},
                                "query_param": {"type": "string"},
                            },
                            "required": ["action"],
                        },
                    },
                    "extraction": {
                        "type": "object",
                        "properties": {
                            "method": {"type": "string"},
                        },
                    },
                },
                "required": ["steps", "extraction"],
            }

            use_dispatcher = True  # Always use dispatcher with new cascade

            if use_dispatcher:
                try:
                    model_result = call_model(
                        tier="quick",
                        system=_DISCOVERY_SYSTEM,
                        messages=[{"role": "user", "content": user_message}],
                        conn=conn,
                        config=config,
                        output_schema=recipe_schema,
                        job_id=None,
                        purpose="ai_nav_discovery",
                        max_tokens=1024,
                    )
                    result = model_result.data
                except ProviderCascadeExhaustedError:
                    logger.warning(
                        "ai_nav: cascade exhausted for %s, retrying via CLI",
                        careers_url,
                    )
                    result, _cost, _schema_valid = call_claude(
                        model="claude-haiku-4-5",
                        system=_DISCOVERY_SYSTEM,
                        messages=[{"role": "user", "content": user_message}],
                        output_schema=recipe_schema,
                        conn=conn,
                        purpose="ai_nav_discovery",
                        config=config,
                        max_tokens=1024,
                    )
            else:
                result, _cost, _schema_valid = call_claude(
                    model="claude-haiku-4-5",
                    system=_DISCOVERY_SYSTEM,
                    messages=[{"role": "user", "content": user_message}],
                    output_schema=recipe_schema,
                    conn=conn,
                    purpose="ai_nav_discovery",
                    config=config,
                    max_tokens=1024,
                )

    except Exception as e:
        logger.warning("ai_nav: discovery call failed for %s: %s", careers_url, e)
        return None

    if not result or not isinstance(result, dict):
        return None

    steps = result.get("steps", [])
    if len(steps) > max_steps:
        logger.debug("ai_nav: recipe too long (%d steps) for %s", len(steps), careers_url)
        return None

    # Build the full recipe with metadata
    recipe = {
        "version": 1,
        "discovered_at": datetime.now().isoformat(),
        "steps": steps,
        "extraction": result.get("extraction", {"method": "links_in_page"}),
    }

    # Validate: execute as many steps as possible, then extract.
    # If extraction yields results even after partial execution, the recipe
    # is still valuable. A step failure just means the discovery model
    # guessed an element role/name slightly wrong, but the page may still
    # show jobs.
    try:
        page.goto(careers_url, timeout=15000, wait_until="networkidle")
        page.wait_for_timeout(2000)

        # Execute steps best-effort — don't abort on first failure.
        # Use the same broad keyword strategy here as replay_navigation_recipe
        # uses at runtime — otherwise validation fills the search box with
        # the user's most specific target title (e.g. "Lead Product Analyst"),
        # which routinely returns zero matches on a destination job-search
        # page even when the recipe is correct. The recipe would then be
        # discarded as "0 jobs" despite working at replay time with the
        # broader term ("analyst"). Keep them in lockstep via the same
        # _substitute_keyword helper replay uses.
        kw = _derive_search_term(target_titles)
        steps_executed = 0
        for step in steps:
            resolved_step = _substitute_keyword(step, kw)

            if _execute_step(page, resolved_step):
                steps_executed += 1
                if step.get("action") in ("click", "type", "press"):
                    page.wait_for_timeout(_POST_ACTION_WAIT_MS)
            else:
                logger.debug(
                    "ai_nav: validation step %d failed for %s — continuing",
                    steps_executed + 1,
                    careers_url,
                )
                break  # Stop at first failure but still try extraction

        jobs = _extract_with_recipe(
            page,
            recipe.get("extraction", {"method": "links_in_page"}),
            target_titles,
            [],
        )
        if not jobs:
            logger.debug("ai_nav: recipe produced 0 jobs for %s — discarding", careers_url)
            return None

        # If some steps failed, trim recipe to only the successful steps
        if steps_executed < len(steps):
            recipe["steps"] = steps[:steps_executed]
            logger.info(
                "ai_nav: trimmed recipe for %s — %d/%d steps worked, %d jobs found",
                careers_url,
                steps_executed,
                len(steps),
                len(jobs),
            )
        else:
            logger.info(
                "ai_nav: discovered recipe for %s — %d steps, %d jobs found",
                careers_url,
                len(steps),
                len(jobs),
            )
        return recipe

    except Exception as e:
        logger.debug("ai_nav: recipe validation error for %s: %s", careers_url, e)
        return None


# ---------------------------------------------------------------------------
# Replay (zero AI cost, all subsequent visits)
# ---------------------------------------------------------------------------


def replay_navigation_recipe(
    page,
    recipe: dict,
    target_titles: list[str],
    exclusions: list[str],
) -> list[dict]:
    """Replay a cached navigation recipe mechanically — no AI calls.

    Executes each step in order via Playwright locators, then extracts
    job listings from the final page state.

    Args:
        page: Playwright Page instance (already navigated to the careers page).
        recipe: Cached recipe dict with 'steps' and 'extraction' keys.
        target_titles: Target title keywords for filtering extracted jobs.
        exclusions: Exclusion keywords.

    Returns:
        List of job dicts with 'title', 'url', 'description' keys.

    Raises:
        RecipeStaleError: If a step fails (element not found, page layout changed).
    """
    steps = recipe.get("steps", [])
    extraction = recipe.get("extraction", {"method": "links_in_page"})

    # Substitute {keyword} placeholder with a broad search term.
    # Use the shortest single-word core term from target titles for maximum
    # recall on career page search engines (e.g. "analyst" not "Lead Product Analyst").
    keyword = _derive_search_term(target_titles)

    for step in steps:
        step = _substitute_keyword(step, keyword)

        success = _execute_step(page, step)
        if not success:
            raise RecipeStaleError(
                f"Step failed: {step.get('action')} {step.get('role', '')} "
                f'"{step.get("name", "")}"'
            )

        # Brief wait after interactive steps for page to update
        if step.get("action") in ("click", "type", "press"):
            page.wait_for_timeout(_POST_ACTION_WAIT_MS)

    return _extract_with_recipe(page, extraction, target_titles, exclusions)


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def cache_nav_recipe(db_path: str, company_id: int, recipe: dict) -> None:
    """Store a navigation recipe on the company record."""
    from job_finder.web.db_helpers import standalone_connection

    try:
        with standalone_connection(db_path) as conn:
            conn.execute(
                "UPDATE companies SET careers_nav_recipe = ? WHERE id = ?",
                (json.dumps(recipe), company_id),
            )
            conn.commit()
        logger.info("Cached nav recipe for company %d", company_id)
    except Exception as e:
        logger.debug("Failed to cache nav recipe: %s", e)


def clear_nav_recipe(db_path: str, company_id: int) -> None:
    """Clear a stale navigation recipe."""
    from job_finder.web.db_helpers import standalone_connection

    try:
        with standalone_connection(db_path) as conn:
            conn.execute(
                "UPDATE companies SET careers_nav_recipe = NULL WHERE id = ?",
                (company_id,),
            )
            conn.commit()
    except Exception as e:
        logger.debug("Failed to clear nav recipe: %s", e)
