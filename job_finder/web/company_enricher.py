"""Company information enrichment via web search.

Provides company metadata enrichment using DuckDuckGo Instant Answer API.
Used for Sonnet-scored jobs to add company context (size, industry).
"""

import logging
import re

from job_finder.web.enrichment_tiers import search_duckduckgo

logger = logging.getLogger(__name__)

# Industry keywords with specificity weights. Higher weight = more distinctive signal.
# Generic words like "technology" or "financial" appear in many company descriptions,
# so they get low weight. Domain-specific terms get high weight.
_INDUSTRY_KEYWORDS: dict[str, list[tuple[str, int]]] = {
    "healthcare": [("healthcare", 3), ("medical", 3), ("pharma", 4), ("biotech", 4),
                   ("therapeutics", 4), ("clinical trials", 4), ("patient care", 4), ("hospital", 3)],
    "insurance": [("insurance", 4), ("underwriting", 4), ("actuarial", 4), ("policyholder", 4)],
    "consulting": [("consulting", 3), ("advisory", 2), ("professional services", 3),
                   ("management consulting", 4)],
    "defense": [("defense contractor", 4), ("defence", 3), ("aerospace", 3), ("military", 3),
                ("government contractor", 3)],
    "cybersecurity": [("cybersecurity", 4), ("infosec", 4), ("threat detection", 4),
                      ("endpoint security", 4), ("vulnerability", 3)],
    "ai & ml": [("artificial intelligence", 4), ("machine learning", 4), ("deep learning", 4),
                ("generative ai", 4), ("llm ", 3), ("neural network", 3)],
    "real estate": [("real estate", 4), ("proptech", 4), ("commercial property", 4),
                    ("brokerage", 2), ("mortgage", 3)],
    "automotive": [("automotive", 4), ("electric vehicle", 4), ("autonomous driving", 4),
                   ("automaker", 4)],
    "gaming": [("game development", 4), ("video game", 4), ("esports", 4), ("game studio", 4)],
    "education": [("edtech", 4), ("education technology", 4), ("university", 2),
                  ("academic", 2), ("online learning", 3)],
    "food & beverage": [("food", 2), ("beverage", 3), ("restaurant", 3), ("grocery", 3),
                        ("agriculture", 3), ("consumer packaged goods", 3), ("cpg", 3)],
    "logistics": [("logistics", 3), ("freight", 3), ("supply chain", 3), ("warehousing", 3),
                  ("last-mile delivery", 4)],
    "energy": [("oil and gas", 4), ("renewable energy", 4), ("solar", 3), ("utilities", 3),
               ("power generation", 3), ("clean energy", 3)],
    "telecommunications": [("telecom", 4), ("telecommunications", 4), ("wireless", 2),
                           ("broadband", 3), ("5g", 3)],
    "manufacturing": [("manufacturing", 3), ("factory", 2), ("industrial", 2)],
    "e-commerce": [("e-commerce", 4), ("ecommerce", 4), ("online retail", 3),
                   ("marketplace", 2), ("shopping", 2)],
    "media": [("media company", 3), ("entertainment", 2), ("streaming", 3),
              ("publishing", 2), ("broadcast", 3)],
    "staffing": [("staffing", 4), ("recruiting", 2), ("talent acquisition", 3),
                 ("employment agency", 4), ("recruitment agency", 4)],
    "finance": [("fintech", 4), ("banking", 3), ("payments", 3), ("lending", 3),
                ("investment management", 3), ("wealth management", 3), ("capital markets", 3)],
    "software": [("saas", 3), ("software company", 3), ("developer tools", 3),
                 ("cloud platform", 3), ("devops", 3)],
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

        # Extract employee count from multiple patterns found in DDG snippets:
        # - "X employees" / "X,000 employees" / "X to Y employees"
        # - "employed X people" / "employs X people"
        # - "X+ employees" (LinkedIn format like "10,001+")
        # - "company size: X" or "company size. X"
        # - "workforce of X" / "team of X"
        employee_patterns = [
            r"(\d[\d,]*)\s*\+?\s*(?:to\s*\d[\d,]*)?\s+employees?",
            r"employ(?:s|ed|ing)\s+(?:around\s+|approximately\s+|about\s+|over\s+|more\s+than\s+)?(\d[\d,]*)\s*(?:people|workers|staff)?",
            r"company\s*size[.:]\s*(\d[\d,]*)",
            r"workforce\s+of\s+(?:around\s+|approximately\s+|about\s+|over\s+)?(\d[\d,]*)",
            r"team\s+of\s+(?:over\s+|more\s+than\s+)?(\d[\d,]*)",
            r"(\d[\d,]*)\s*\+\s*employees?",
        ]
        for pattern in employee_patterns:
            employee_match = re.search(pattern, ddg_text, re.IGNORECASE)
            if employee_match:
                count_str = employee_match.group(1).replace(",", "")
                try:
                    count = int(count_str)
                    if count >= 5:  # Filter noise (1-4 is likely parsing junk)
                        result["company_size"] = _classify_size(count)
                        break
                except ValueError:
                    pass

        text_lower = ddg_text.lower()
        best_industry = None
        best_score = 0
        for industry, keyword_weights in _INDUSTRY_KEYWORDS.items():
            score = sum(w for kw, w in keyword_weights if kw in text_lower)
            if score > best_score:
                best_score = score
                best_industry = industry
        if best_industry and best_score >= 3:
            result["industry"] = best_industry

        return result

    except Exception as e:
        logger.debug("enrich_company_info failed for '%s': %s", company_name, e)
        return {}
