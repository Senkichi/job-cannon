"""Report generator for cascade audit (Phase 36).

Generates markdown reports for audit rounds with per-callsite results,
verdict statistics, and gate outcomes.
"""

from __future__ import annotations

import json
from pathlib import Path


def write_report(
    *,
    round_num: int,
    callsite: str,
    provider: str,
    artifacts_dir: Path,
    output_path: Path,
) -> str:
    """Generate markdown report for a (callsite, provider) pair.

    Args:
        round_num: Round number (0, 1, or 2).
        callsite: Callsite name.
        provider: Provider name.
        artifacts_dir: Directory containing artifact JSON files.
        output_path: Path where markdown report should be written.

    Returns:
        Absolute path to the written report file.
    """
    # Load artifact JSON
    artifact_path = artifacts_dir / f"round_{round_num}" / f"{callsite}_{provider}.json"
    if not artifact_path.exists():
        raise FileNotFoundError(f"Artifact not found: {artifact_path}")

    artifact = json.loads(artifact_path.read_text())

    # Extract provenance and results
    provenance = artifact.get("provenance", {})
    data = artifact.get("data", {})
    metrics = data.get("metrics", artifact.get("metrics", {}))
    verdicts = data.get("verdicts", artifact.get("verdicts", []))

    # Generate markdown
    out: list[str] = []
    out.append(f"# Cascade Audit Report — Round {round_num}")
    out.append("")
    out.append(f"**Callsite:** {callsite}")
    out.append(f"**Provider:** {provider}")
    out.append("")
    out.append("## Provenance")
    out.append(f"- Harness commit SHA: {provenance.get('harness_commit_sha', 'unknown')}")
    out.append(f"- Sample seed: {provenance.get('sample_seed', 'unknown')}")
    out.append(f"- Scheduler pause status: {provenance.get('scheduler_pause_status', 'unknown')}")
    out.append("")

    out.append("## Metrics")
    out.append("```json")
    out.append(json.dumps(metrics, indent=2))
    out.append("```")
    out.append("")

    out.append("## Verdict Statistics")
    if verdicts:
        total = len(verdicts)
        wins_a = sum(1 for v in verdicts if v.get("winner") == "A")
        wins_b = sum(1 for v in verdicts if v.get("winner") == "B")
        ties = sum(1 for v in verdicts if v.get("winner") == "tie")
        avg_confidence = sum(v.get("confidence", 0) for v in verdicts) / total if total else 0

        out.append(f"- Total comparisons: {total}")
        out.append(f"- A wins: {wins_a} ({wins_a/total:.1%})" if total else "- A wins: 0")
        out.append(f"- B wins: {wins_b} ({wins_b/total:.1%})" if total else "- B wins: 0")
        out.append(f"- Ties: {ties} ({ties/total:.1%})" if total else "- Ties: 0")
        out.append(f"- Average confidence: {avg_confidence:.2f}")
    else:
        out.append("_(no verdicts recorded)")
    out.append("")

    out.append("## Gate Outcomes")
    gate_outcomes = data.get("gate_outcomes", artifact.get("gate_outcomes", {}))
    if gate_outcomes:
        for gate, outcome in gate_outcomes.items():
            out.append(f"- **{gate}**: {outcome}")
    else:
        out.append("_(no gate outcomes recorded)")
    out.append("")

    # Write report
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(out), encoding="utf-8")

    return str(output_path)
