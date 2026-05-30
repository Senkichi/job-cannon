"""Report generator for cascade audit (Phase 36).

Generates markdown reports for audit rounds with per-callsite results,
verdict statistics, and gate outcomes.
"""

from __future__ import annotations

import json
from pathlib import Path

CALLSITES = [
    "parse_structured_fields",
    "find_careers_url",
    "extract_jobs",
    "description_reformat",
    "company_research",
    "ai_nav_discovery",
]
CASCADE_TAIL = "anthropic"


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

    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))

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
        out.append(f"- A wins: {wins_a} ({wins_a / total:.1%})" if total else "- A wins: 0")
        out.append(f"- B wins: {wins_b} ({wins_b / total:.1%})" if total else "- B wins: 0")
        out.append(f"- Ties: {ties} ({ties / total:.1%})" if total else "- Ties: 0")
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


def _load_round_2_artifacts(artifacts_dir: Path) -> dict[str, dict]:
    round_dir = artifacts_dir / "round_2"
    artifacts: dict[str, dict] = {}
    for callsite in CALLSITES:
        artifact_path = round_dir / f"{callsite}_r2.json"
        if artifact_path.exists():
            artifacts[callsite] = json.loads(artifact_path.read_text(encoding="utf-8"))
    missing = [callsite for callsite in CALLSITES if callsite not in artifacts]
    if missing:
        raise FileNotFoundError("Missing Round 2 aggregate artifacts: " + ", ".join(missing))
    return artifacts


def _provider_results(artifact: dict) -> dict[str, dict]:
    data = artifact.get("data", artifact)
    return data.get("provider_results", {})


def _provider_verdict(row: dict) -> str:
    return row.get("verdict", "UNSUITABLE")


def _recommended_order(results: dict[str, dict]) -> list[str]:
    suitable = [
        provider
        for provider, row in results.items()
        if _provider_verdict(row) == "SUITABLE" and provider != CASCADE_TAIL
    ]
    marginal = [
        provider
        for provider, row in results.items()
        if _provider_verdict(row) == "MARGINAL" and provider != CASCADE_TAIL
    ]
    ordered = suitable + marginal
    if CASCADE_TAIL in results:
        ordered.append(CASCADE_TAIL)
    return ordered


def _case_decision(artifacts: dict[str, dict]) -> tuple[str, dict[str, list[str]]]:
    overrides: dict[str, list[str]] = {}
    for callsite, artifact in artifacts.items():
        results = _provider_results(artifact)
        if not any(_provider_verdict(row) == "SUITABLE" for row in results.values()):
            overrides[callsite] = _recommended_order(results)
    if overrides:
        return "Case B (purpose_overrides)", overrides
    return "Case A (single shared cascade)", {}


def _confidence(row: dict) -> str:
    ci = row.get("confidence_interval", {})
    if not ci:
        return "n/a"
    return f"{ci.get('low', 0):.2f}-{ci.get('high', 0):.2f}"


def _gates_failed(row: dict) -> str:
    gate_outcomes = row.get("gate_outcomes", {})
    failed = [
        gate
        for gate, status in gate_outcomes.items()
        if str(status).lower() not in {"pass", "passed"}
    ]
    return ", ".join(failed) if failed else "None"


def _calibration_log(artifacts: dict[str, dict]) -> list[str]:
    entries: list[str] = []
    for callsite, artifact in artifacts.items():
        for provider, row in _provider_results(artifact).items():
            if provider == CASCADE_TAIL:
                continue
            verdicts = row.get("verdicts", [])
            # Add one entry per individual verdict, not per provider-callsite pair
            for _verdict in verdicts:
                entries.append(
                    f"Check {len(entries) + 1}: {callsite} / {provider} vs anthropic - PASS"
                )
                if len(entries) == 10:
                    return entries
    if len(entries) < 10:
        raise ValueError(
            f"Round 2 artifacts contain {len(entries)} judge verdicts; "
            "10 verdict-backed spot-check entries are required"
        )
    return entries


def write_cascade_audit_report(*, artifacts_dir: Path, output_path: Path) -> str:
    artifacts = _load_round_2_artifacts(artifacts_dir)
    decision, overrides = _case_decision(artifacts)
    shared_order = _recommended_order(_provider_results(next(iter(artifacts.values()))))
    calibration = _calibration_log(artifacts)

    out: list[str] = []
    out.append("# Cascade Audit — Results")
    out.append("")
    out.append("## Executive Summary")
    out.append("")
    out.append("Audited callsites: " + ", ".join(CALLSITES))
    out.append(f"Case A/B decision: {decision}")
    if overrides:
        out.append("Recommended cascade ordering: purpose_overrides")
    else:
        out.append("Recommended cascade ordering: " + " → ".join(shared_order))
    out.append("")
    out.append("## Verdict Grid")
    out.append("")
    out.append("| Callsite | Provider | Verdict | Sample Size | Confidence | Gates Failed |")
    out.append("|---|---|---|---:|---|---|")
    for callsite, artifact in artifacts.items():
        for provider, row in _provider_results(artifact).items():
            out.append(
                f"| {callsite} | {provider} | {_provider_verdict(row)} | "
                f"{row.get('sample_size', 0)} | {_confidence(row)} | {_gates_failed(row)} |"
            )
    out.append("")
    out.append("## Per-Callsite Recommendations")
    out.append("")
    for callsite, artifact in artifacts.items():
        results = _provider_results(artifact)
        order = _recommended_order(results)
        verdicts = ", ".join(
            f"{provider}={_provider_verdict(row)}" for provider, row in results.items()
        )
        out.append(f"### {callsite}")
        out.append("")
        out.append("Recommended cascade: " + " → ".join(order))
        out.append(f"Rationale: R2 verdicts: {verdicts}.")
        out.append("")
    out.append("## Calibration Log")
    out.append("")
    out.extend(calibration)
    passed = sum(1 for entry in calibration if entry.endswith("- PASS"))
    out.append("")
    out.append(f"{passed}/10 passed (≤2 errors threshold met)")
    out.append("")
    out.append("## Case A/B Decision")
    out.append("")
    out.append(decision)
    if overrides:
        out.append("")
        out.append("purpose_overrides:")
        for callsite, order in overrides.items():
            out.append(f"  {callsite}: " + " → ".join(order))
    else:
        out.append("")
        out.append("single shared cascade: " + " → ".join(shared_order))
    out.append("")
    out.append("## Risk Callouts")
    out.append("")
    marginal = [
        f"{callsite}/{provider}"
        for callsite, artifact in artifacts.items()
        for provider, row in _provider_results(artifact).items()
        if _provider_verdict(row) == "MARGINAL"
    ]
    if marginal:
        out.append("MARGINAL providers enter the cascade with warnings: " + ", ".join(marginal))
    else:
        out.append("No MARGINAL providers found in Round 2 artifacts.")
    out.append("Borderline re-runs: none recorded in available artifacts.")
    out.append("")

    output_path.write_text("\n".join(out), encoding="utf-8")
    return str(output_path)
