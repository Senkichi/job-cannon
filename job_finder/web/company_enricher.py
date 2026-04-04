"""Company information enrichment via web search.

Uses DuckDuckGo web search (DDGS.text()) for company metadata.
The legacy search_duckduckgo() (Instant Answer API) returned 0/1054 results.
"""

import logging
import re

from ddgs import DDGS

logger = logging.getLogger(__name__)

_INDUSTRY_KEYWORDS = {
    "finance": ["fintech", "finance", "banking", "financial"],
    "software": ["software", "saas", "technology", "platform"],
    "healthcare": ["healthcare", "health", "medical", "pharma"],
    "e-commerce": ["e-commerce", "ecommerce", "retail", "marketplace"],
    "media": ["media", "entertainment", "streaming", "content"],
}


def _classify_size(count: int) -> str:
    """Classify employee count into size band."""
    if count < 50:
        return "startup"
    if count < 500:
        return "small"
    if count < 5000:
        return "mid-size"
    return "large"


def enrich_company_info(company_name: str) -> dict:
    """Enrich company info via DuckDuckGo web search.

    Searches for employee count and industry keywords in result snippets.
    Returns dict with optional keys: company_size, industry.
    Best-effort — returns empty dict on failure.

    Args:
        company_name: The company name to look up.

    Returns:
        Dict with any of: company_size (str), industry (str).
        May be empty if no data found.
    """
    try:
        query = f'"{company_name}" company employees'
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=3))
        if not results:
            return {}
        snippets = " ".join(r.get("body", "") for r in results)
        if not snippets:
            return {}

        result = {}

        # Extract employee count pattern: "X employees" or "X,000 employees"
        employee_match = re.search(
            r"(\d[\d,]*)\s*(?:to\s*\d[\d,]*)?\s+employees?", snippets, re.IGNORECASE
        )
        if employee_match:
            count_str = employee_match.group(1).replace(",", "")
            try:
                result["company_size"] = _classify_size(int(count_str))
            except ValueError:
                pass

        snippets_lower = snippets.lower()
        for industry, keywords in _INDUSTRY_KEYWORDS.items():
            if any(kw in snippets_lower for kw in keywords):
                result["industry"] = industry
                break

        return result

    except Exception as e:
        logger.debug("enrich_company_info failed for '%s': %s", company_name, e)
        return {}
