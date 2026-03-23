"""Job Finder - main pipeline orchestrator.

Usage:
    python -m job_finder.main                    # full pipeline
    python -m job_finder.main --source gmail     # gmail only
    python -m job_finder.main --source serpapi   # serpapi only
    python -m job_finder.main --output markdown  # markdown report
    python -m job_finder.main --interactive      # review jobs interactively
    python -m job_finder.main --stats            # show database stats
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.table import Table
from rich.prompt import Prompt

from job_finder.config import (
    DEFAULT_DB_PATH,
    DEFAULT_LOOKBACK_DAYS,
    DEFAULT_MAX_RESULTS,
    DEFAULT_MIN_SCORE_THRESHOLD,
    load_config,
)
from job_finder.db import JobDB
from job_finder.models import Job
from job_finder.scoring.scorer import JobScorer
from job_finder.json_utils import safe_json_load

console = Console()


def fetch_gmail_jobs(config: dict) -> list[Job]:
    """Fetch jobs from Gmail alert emails."""
    gmail_config = config.get("sources", {}).get("gmail", {})
    if not gmail_config.get("enabled", False):
        console.print("[dim]Gmail source disabled in config[/dim]")
        return []

    try:
        from job_finder.sources.gmail_source import GmailSource

        console.print("[bold blue]Fetching from Gmail...[/bold blue]")
        source = GmailSource()
        lookback = gmail_config.get("lookback_days", DEFAULT_LOOKBACK_DAYS)
        jobs = source.fetch_jobs(lookback_days=lookback)
        console.print(f"  Found {len(jobs)} jobs from email alerts")
        return jobs
    except FileNotFoundError:
        console.print(
            "[yellow]Gmail not configured. Run: python -m job_finder.gmail_auth[/yellow]"
        )
        return []
    except Exception as e:
        console.print(f"[red]Gmail error: {e}[/red]")
        return []


def fetch_serpapi_jobs(config: dict) -> list[Job]:
    """Fetch jobs from SerpAPI Google Jobs."""
    serpapi_config = config.get("sources", {}).get("serpapi", {})
    if not serpapi_config.get("enabled", False):
        console.print("[dim]SerpAPI source disabled in config[/dim]")
        return []

    api_key = serpapi_config.get("api_key", "")
    if not api_key:
        console.print("[yellow]SerpAPI key not set in config[/yellow]")
        return []

    try:
        from job_finder.sources.serpapi_source import SerpAPISource

        console.print("[bold blue]Fetching from SerpAPI (Google Jobs)...[/bold blue]")
        source = SerpAPISource(api_key)
        queries = serpapi_config.get("queries", [])
        jobs = source.fetch_jobs(queries)
        console.print(f"  Found {len(jobs)} jobs from Google Jobs")
        return jobs
    except Exception as e:
        console.print(f"[red]SerpAPI error: {e}[/red]")
        return []


def display_jobs_table(jobs: list[dict], title: str = "Top Jobs"):
    """Display jobs in a rich terminal table."""
    table = Table(title=title, show_lines=True)

    table.add_column("Score", style="bold", width=6, justify="right")
    table.add_column("Title", style="cyan", max_width=40)
    table.add_column("Company", style="green", max_width=25)
    table.add_column("Location", max_width=20)
    table.add_column("Salary", max_width=18)
    table.add_column("Sources", max_width=15)
    table.add_column("Link", max_width=50)
    table.add_column("Status", max_width=10)

    for job in jobs:
        score = job.get("score", 0)
        score_style = "bold green" if score >= 70 else "yellow" if score >= 50 else "red"

        salary = ""
        if job.get("salary_min") and job.get("salary_max"):
            salary = f"${job['salary_min']//1000}K-${job['salary_max']//1000}K"
        elif job.get("salary_max"):
            salary = f"Up to ${job['salary_max']//1000}K"

        sources = ", ".join(safe_json_load(job.get("sources"), default=[]))
        urls = safe_json_load(job.get("source_urls"), default=[])
        link = urls[0] if urls else ""

        table.add_row(
            f"[{score_style}]{score:.0f}[/{score_style}]",
            job.get("title", ""),
            job.get("company", ""),
            job.get("location", ""),
            salary,
            sources,
            f"[link={link}]{link[:45]}...[/link]" if link else "",
            job.get("user_interest", ""),
        )

    console.print(table)


def interactive_review(db: JobDB):
    """Interactively review unreviewed jobs."""
    jobs = db.get_top_jobs(limit=100, interest_filter="unreviewed")

    if not jobs:
        console.print("[green]No unreviewed jobs![/green]")
        return

    console.print(f"\n[bold]{len(jobs)} unreviewed jobs to review[/bold]\n")

    for i, job in enumerate(jobs, 1):
        console.print(f"\n[bold cyan]--- Job {i}/{len(jobs)} ---[/bold cyan]")
        console.print(f"[bold]{job['title']}[/bold]")
        console.print(f"Company: {job['company']}")
        console.print(f"Location: {job['location']}")

        if job.get("salary_min") and job.get("salary_max"):
            console.print(
                f"Salary: ${job['salary_min']//1000}K - ${job['salary_max']//1000}K"
            )

        urls = safe_json_load(job.get("source_urls"), default=[])
        for url in urls[:2]:
            console.print(f"Link: {url}")

        console.print(f"Score: {job['score']:.0f}")

        choice = Prompt.ask(
            "\n[i]nterested / [s]kip / [a]pplied / [q]uit",
            choices=["i", "s", "a", "q"],
            default="s",
        )

        if choice == "q":
            break
        elif choice == "i":
            db.mark_interest(job["dedup_key"], "interested")
            console.print("[green]Marked as interested[/green]")
        elif choice == "a":
            db.mark_interest(job["dedup_key"], "applied")
            console.print("[blue]Marked as applied[/blue]")
        else:
            db.mark_interest(job["dedup_key"], "skipped")


def generate_markdown_report(jobs: list[dict], output_dir: str = "reports/"):
    """Generate a markdown report of top jobs."""
    Path(output_dir).mkdir(exist_ok=True)
    date_str = datetime.now().strftime("%Y-%m-%d")
    filepath = Path(output_dir) / f"job-report-{date_str}.md"

    lines = [f"# Job Report - {date_str}\n"]
    lines.append(f"**{len(jobs)} jobs scored above threshold**\n\n")

    for job in jobs:
        score = job.get("score", 0)
        emoji = "🟢" if score >= 70 else "🟡" if score >= 50 else "🔴"
        salary = ""
        if job.get("salary_min") and job.get("salary_max"):
            salary = f" | ${job['salary_min']//1000}K-${job['salary_max']//1000}K"

        urls = safe_json_load(job.get("source_urls"), default=[])
        link = f"[Apply]({urls[0]})" if urls else ""

        lines.append(f"### {emoji} {job['title']} ({score:.0f})")
        lines.append(f"**{job['company']}** — {job['location']}{salary}")
        if link:
            lines.append(link)
        lines.append("")

    filepath.write_text("\n".join(lines))
    console.print(f"[green]Report saved to {filepath}[/green]")


def main():
    parser = argparse.ArgumentParser(description="Job Finder Pipeline")
    parser.add_argument(
        "--source",
        choices=["gmail", "serpapi", "all"],
        default="all",
        help="Which source to fetch from",
    )
    parser.add_argument(
        "--output",
        choices=["cli", "markdown", "json"],
        default=None,
        help="Output format (overrides config)",
    )
    parser.add_argument("--interactive", action="store_true", help="Review jobs interactively")
    parser.add_argument("--stats", action="store_true", help="Show database stats")
    parser.add_argument("--config", default="config.yaml", help="Config file path")

    args = parser.parse_args()

    # Load config
    try:
        config = load_config(args.config)
    except FileNotFoundError as e:
        console.print(f"[red]{e}[/red]")
        sys.exit(1)

    # Initialize DB
    db_path = config.get("db", {}).get("path", DEFAULT_DB_PATH)
    db = JobDB(db_path)

    # Stats mode
    if args.stats:
        stats = db.stats()
        console.print(f"\n[bold]Database Stats[/bold]")
        console.print(f"Total jobs: {stats['total_jobs']}")
        console.print(f"By status: {stats['by_interest']}")
        for run in stats["recent_runs"]:
            console.print(
                f"  Run: {run['timestamp']} | {run['source']} | "
                f"{run['jobs_fetched']} fetched, {run['jobs_new']} new"
            )
        return

    # Interactive mode
    if args.interactive:
        interactive_review(db)
        return

    # --- FETCH ---
    all_jobs = []

    if args.source in ("gmail", "all"):
        all_jobs.extend(fetch_gmail_jobs(config))

    if args.source in ("serpapi", "all"):
        all_jobs.extend(fetch_serpapi_jobs(config))

    if not all_jobs:
        console.print("[yellow]No jobs fetched from any source.[/yellow]")
        return

    console.print(f"\n[bold]Total fetched: {len(all_jobs)} jobs[/bold]")

    # --- SCORE ---
    scorer = JobScorer(config)
    scored_jobs = scorer.score_jobs(all_jobs)
    console.print(f"[bold]Above threshold: {len(scored_jobs)} jobs[/bold]")

    # --- DEDUPLICATE & PERSIST ---
    new_count = 0
    for job in scored_jobs:
        is_new = db.upsert_job(job)
        if is_new:
            new_count += 1

    console.print(f"[bold green]New jobs: {new_count}[/bold green]")
    db.log_run(args.source, len(all_jobs), new_count, len(scored_jobs))

    # --- OUTPUT ---
    output_format = args.output or config.get("output", {}).get("default_format", "cli")
    max_results = config.get("output", {}).get("max_results", DEFAULT_MAX_RESULTS)
    threshold = config.get("scoring", {}).get("min_score_threshold", DEFAULT_MIN_SCORE_THRESHOLD)

    top_jobs = db.get_top_jobs(limit=max_results, min_score=threshold)

    if output_format == "markdown":
        generate_markdown_report(top_jobs)
    elif output_format == "json":
        print(json.dumps(top_jobs, indent=2, default=str))
    else:
        display_jobs_table(top_jobs)


if __name__ == "__main__":
    main()
