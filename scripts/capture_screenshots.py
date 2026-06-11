"""Capture README screenshots (+ optional demo GIF) against `job-cannon --demo`.

Usage:
    uv run --active python scripts/capture_screenshots.py            # 5 PNGs
    uv run --active python scripts/capture_screenshots.py --record   # + demo.webm -> demo.gif

Outputs to docs/assets/screenshots/ (PNGs) and docs/assets/demo.gif. Outputs
are OVERWRITTEN — the script is idempotent by design.

Refresh policy: re-run before any release that changes UI. The PNGs/GIF are
generated artifacts but ARE committed (they're documentation; GitHub renders
committed assets reliably).

Requirements beyond the dev extras: a Playwright chromium build
(``uv run --active playwright install chromium``) and — for ``--record`` only —
``ffmpeg`` on PATH for the webm→gif conversion. Both are local doc-tooling
dependencies; this script is intentionally NOT wired into CI.

Implementation notes:
- The app is launched as a real subprocess through the ``--demo`` entry point
  (exercises the actual user path: temp DB, seeding, port logic).
- Waits are selector-based, never sleeps. ``networkidle`` is unusable here —
  the SSE live-update stream (/events) holds a connection open forever.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parents[1]
SCREENSHOT_DIR = REPO_ROOT / "docs" / "assets" / "screenshots"
GIF_PATH = REPO_ROOT / "docs" / "assets" / "demo.gif"

PORT = 5055
BASE = f"http://127.0.0.1:{PORT}"
VIEWPORT = {"width": 1280, "height": 800}

# Selector contracts (load-bearing template attributes):
#   compact job row:  tr[data-dedup-key]   (jobs/_row.html)
#   expanded triage:  tr[data-row-expanded="triage"]  (jobs/_row_expanded.html)
ROW_SELECTOR = "tr[data-dedup-key]"
EXPANDED_SELECTOR = 'tr[data-row-expanded="triage"]'


def _launch_demo() -> subprocess.Popen:
    env = dict(os.environ, JOB_CANNON_NO_BROWSER="1")
    proc = subprocess.Popen(
        [sys.executable, "-m", "job_finder", "--demo", "--terminal", "--port", str(PORT)],
        env=env,
        cwd=str(REPO_ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            r = requests.get(f"{BASE}/__jc_health", timeout=1)
            if r.status_code == 200 and r.json().get("app") == "job-cannon":
                return proc
        except requests.RequestException:
            pass
        if proc.poll() is not None:
            raise RuntimeError(f"demo process exited early (code {proc.returncode})")
        time.sleep(0.5)
    proc.kill()
    raise RuntimeError("demo instance never became healthy on " + BASE)


def _goto(page, path: str, wait_for: str) -> None:
    # domcontentloaded, NOT networkidle — the SSE /events stream never idles.
    page.goto(f"{BASE}{path}", wait_until="domcontentloaded")
    page.wait_for_selector(wait_for, timeout=15_000)
    # Let HTMX settle + Tailwind CDN paint; fonts/colors land within a beat.
    page.wait_for_timeout(600)


def _capture_screenshots(page) -> None:
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)

    _goto(page, "/jobs/", ROW_SELECTOR)
    page.screenshot(path=str(SCREENSHOT_DIR / "job-board.png"))

    page.click(ROW_SELECTOR)
    page.wait_for_selector(EXPANDED_SELECTOR, timeout=15_000)
    page.wait_for_timeout(400)
    page.screenshot(path=str(SCREENSHOT_DIR / "job-expanded.png"))

    _goto(page, "/dashboard/", "text=Quick Actions")
    page.screenshot(path=str(SCREENSHOT_DIR / "dashboard.png"))

    _goto(page, "/pipeline/", "[data-status]")
    page.screenshot(path=str(SCREENSHOT_DIR / "pipeline-kanban.png"))

    _goto(page, "/companies/", "h1:has-text('Companies')")
    page.screenshot(path=str(SCREENSHOT_DIR / "companies.png"))

    print(f"5 screenshots written to {SCREENSHOT_DIR}")


def _record_tour(browser) -> None:
    """~25s scripted tour recorded as video, converted to GIF via ffmpeg."""
    video_dir = REPO_ROOT / "docs" / "assets" / "_video_tmp"
    video_dir.mkdir(parents=True, exist_ok=True)
    context = browser.new_context(
        viewport=VIEWPORT,
        record_video_dir=str(video_dir),
        record_video_size=VIEWPORT,
    )
    page = context.new_page()

    _goto(page, "/dashboard/", "text=Quick Actions")
    page.wait_for_timeout(2_500)

    _goto(page, "/jobs/", ROW_SELECTOR)
    page.wait_for_timeout(2_000)

    page.click(ROW_SELECTOR)
    page.wait_for_selector(EXPANDED_SELECTOR, timeout=15_000)
    page.wait_for_timeout(3_500)

    # Scroll through a few rows so classification chips are visibly varied.
    page.mouse.wheel(0, 600)
    page.wait_for_timeout(2_000)

    _goto(page, "/pipeline/", "[data-status]")
    page.wait_for_timeout(3_000)

    _goto(page, "/companies/", "h1:has-text('Companies')")
    page.wait_for_timeout(2_500)

    context.close()  # flushes the video file

    webm = next(video_dir.glob("*.webm"))
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(webm),
            "-vf",
            "fps=10,scale=960:-1:flags=lanczos",
            str(GIF_PATH),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    shutil.rmtree(video_dir, ignore_errors=True)
    size_mb = GIF_PATH.stat().st_size / 1_048_576
    print(f"GIF written to {GIF_PATH} ({size_mb:.1f} MB)")
    if size_mb > 8:
        print("WARNING: GIF exceeds the 8 MB target — consider trimming the tour or fps.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--record", action="store_true", help="also record the demo GIF (needs ffmpeg)"
    )
    args = parser.parse_args()

    from playwright.sync_api import sync_playwright

    proc = _launch_demo()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            context = browser.new_context(viewport=VIEWPORT)
            _capture_screenshots(context.new_page())
            context.close()
            if args.record:
                _record_tour(browser)
            browser.close()
    finally:
        proc.kill()
        proc.wait(timeout=10)


if __name__ == "__main__":
    main()
