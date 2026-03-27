"""Company information enrichment via web search.

Provides company metadata enrichment using DuckDuckGo Instant Answer API.
Used for Sonnet-scored jobs to add company context (size, industry).
"""

import logging
import re

from job_finder.web.enrichment_tiers import search_duckduckgo

logger = logging.getLogger(__name__)


def enrich_company_info(company_name: str) -> dict:
    """Enrich company info via DuckDuckGo (for Sonnet-scored jobs only).

    Returns dict with optional keys: company_size, industry, funding_stage.
    Best-effort — returns empty dict on failure. DDG reliability is LOW per
    research (sparse company data), so callers should not depend on results.

    Args:
        company_name: The company name to look up.

    Returns:
        Dict with any of: company_size (str), industry (str), funding_stage (str).
        May be empty if no data found.
    """
    try:
        query = f"{company_name} company size employees industry"
        ddg_text = search_duckduckgo(query)
        if not ddg_text:
            return {}

        result = {}

        # Extract employee count pattern: "X employees" or "X,000 employees"
        employee_match = re.search(
            r"(\d[\d,]*)\s*(?:to\s*\d[\d,]*)?\s+employees?", ddg_text, re.IGNORECASE
        )
        if employee_match:
            count_str = employee_match.group(1).replace(",", "")
            try:
                count = int(count_str)
                if count < 50:
                    result["company_size"] = "startup"
                elif count < 500:
                    result["company_size"] = "small"
                elif count < 5000:
                    result["company_size"] = "mid-size"
                else:
                    result["company_size"] = "large"
            except ValueError:
                pass

        # Extract industry keywords
        industry_keywords = {
            "software": ["software", "saas", "tech", "technology", "platform"],
            "finance": ["finance", "fintech", "banking", "financial"],
            "healthcare": ["healthcare", "health", "medical", "pharma"],
            "e-commerce": ["e-commerce", "ecommerce", "retail", "marketplace"],
            "media": ["media", "entertainment", "streaming", "content"],
        }
        text_lower = ddg_text.lower()
        for industry, keywords in industry_keywords.items():
            if any(kw in text_lower for kw in keywords):
                result["industry"] = industry
                break

        return result

    except Exception as e:
        logger.debug("enrich_company_info failed for '%s': %s", company_name, e)
        return {}
