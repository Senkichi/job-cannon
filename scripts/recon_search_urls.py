"""One-off recon: identify each target's search URL pattern by submitting
a known keyword and capturing the resulting URL.

Used to inform hand-curation of careers_nav_recipe JSON for the 5
residual zero-yield targets (Deloitte, NVIDIA, Kaiser, Oracle, ByteDance)
identified by scripts/probe_ai_nav.py post 1a+1b.

NOT for production. Throwaway recon. After hand-curation completes,
this script can be deleted.

Run:
    .venv/Scripts/python.exe scripts/recon_search_urls.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.sync_api import sync_playwright

TARGETS = [
    ("NVIDIA", "https://www.nvidia.com/en-us/about-nvidia/careers/", "analyst"),
    ("Oracle", "https://www.oracle.com/careers/", "analyst"),
    ("ByteDance", "https://joinbytedance.com/", "analyst"),
]


def recon(browser, name: str, url: str, keyword: str) -> dict:
    """Try a few common selectors / patterns to discover the search URL."""
    result = {"name": name, "start_url": url, "final_url": None, "method": None, "error": None}
    page = browser.new_page()
    try:
        page.goto(url, timeout=20000, wait_until="domcontentloaded")
        page.wait_for_timeout(2500)

        # Strategy 1: try common keyword-input selectors
        candidates = [
            'input[name*="keyword" i]',
            'input[name*="search" i]',
            'input[name="q"]',
            'input[type="search"]',
            'input[placeholder*="search" i]',
            'input[placeholder*="keyword" i]',
            'input[aria-label*="search" i]',
            'input[aria-label*="keyword" i]',
        ]
        for sel in candidates:
            try:
                loc = page.locator(sel).first
                if loc.is_visible(timeout=1000):
                    loc.fill(keyword, timeout=2000)
                    loc.press("Enter")
                    page.wait_for_load_state("networkidle", timeout=15000)
                    result["final_url"] = page.url
                    result["method"] = f"selector: {sel}"
                    return result
            except Exception:
                continue

        # Strategy 2: try clicking a "Search jobs" / "View jobs" link first
        link_text_candidates = [
            "Search jobs",
            "Search Jobs",
            "View jobs",
            "View Jobs",
            "Open Positions",
            "Find a job",
            "Find Jobs",
            "Browse jobs",
            "Job Search",
            "All jobs",
            "All Jobs",
        ]
        for txt in link_text_candidates:
            try:
                link = page.get_by_role("link", name=txt).first
                if link.is_visible(timeout=1000):
                    link.click(timeout=3000)
                    page.wait_for_load_state("networkidle", timeout=15000)
                    # After clicking, try to find a search input on the destination
                    for sel in candidates:
                        try:
                            loc = page.locator(sel).first
                            if loc.is_visible(timeout=1000):
                                loc.fill(keyword, timeout=2000)
                                loc.press("Enter")
                                page.wait_for_load_state("networkidle", timeout=15000)
                                result["final_url"] = page.url
                                result["method"] = f"link={txt!r}, then selector: {sel}"
                                return result
                        except Exception:
                            continue
                    # No search input on destination — just capture the URL
                    result["final_url"] = page.url
                    result["method"] = f"link={txt!r} only (no search input found post-click)"
                    return result
            except Exception:
                continue

        result["error"] = "no selector or link matched"
    except Exception as e:
        result["error"] = str(e)[:200]
    finally:
        page.close()
    return result


def main() -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            for name, url, keyword in TARGETS:
                print(f"\n--- {name}: {url} ---")
                r = recon(browser, name, url, keyword)
                print(f"  final_url: {r['final_url']}")
                print(f"  method:    {r['method']}")
                if r["error"]:
                    print(f"  error:     {r['error']}")
        finally:
            browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
