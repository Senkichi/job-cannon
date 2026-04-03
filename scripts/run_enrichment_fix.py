"""Launch the plan-build-review workflow to implement the enrichment pipeline fixes.

Invokes the langgraph-agents plan-build-review graph with the pre-written
ENRICHMENT_FIX_PLAN.md. Since a plan is provided, the workflow skips the
planner and goes straight to plan review → build → code review loop.

Usage:
    uv run --active python scripts/run_enrichment_fix.py
"""

import sys
from pathlib import Path

# Add langgraph-agents to sys.path
LANGGRAPH_AGENTS_DIR = Path.home() / "repos" / "langgraph-agents"
sys.path.insert(0, str(LANGGRAPH_AGENTS_DIR / "src"))

from langgraph_agents.graphs.plan_build_review import plan_build_review_app


def main():
    workspace = Path(__file__).resolve().parent.parent  # job-cannon root
    plan_path = workspace / "ENRICHMENT_FIX_PLAN.md"

    plan_text = plan_path.read_text(encoding="utf-8")

    task = (
        "Implement the enrichment pipeline fixes described in the plan. "
        "The workspace is a Python/Flask project (job-cannon). Key context:\n"
        "- Package manager: uv (use 'uv run pytest' not bare 'pytest')\n"
        "- The plan modifies 3 source files and 3 test files\n"
        "- All existing tests must continue to pass\n"
        "- Follow existing code style (PEP 8, type hints on signatures, logging not print)\n"
        "- The ddgs library is already installed (used by agentic_enricher.py)\n"
        "- Read each file fully before modifying it\n"
        "- Run 'uv run pytest tests/ -x' after all changes to verify no regressions\n"
        "- IMPORTANT: Read CLAUDE.md at the project root for project conventions"
    )

    print(f"Workspace: {workspace}")
    print(f"Plan: {plan_path}")
    print(f"Plan length: {len(plan_text)} chars")
    print("Launching plan-build-review workflow...")
    print("=" * 60)

    result = plan_build_review_app.invoke({
        "task": task,
        "current_plan": plan_text,
        "current_code": "",
        "workspace_path": str(workspace),
    })

    print("\n" + "=" * 60)
    print("WORKFLOW COMPLETE")
    print("=" * 60)

    if result.get("current_plan"):
        print(f"\nFinal plan length: {len(result['current_plan'])} chars")

    if result.get("current_code"):
        print(f"\nCode diff length: {len(result['current_code'])} chars")
        print("\nCode diff (first 2000 chars):")
        print(result["current_code"][:2000])
    else:
        print("\nNo code diff returned.")


if __name__ == "__main__":
    main()
