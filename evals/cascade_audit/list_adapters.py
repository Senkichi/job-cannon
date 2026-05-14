"""Enumeration command to verify all 6 adapters exist (Phase 36)."""

from pathlib import Path


def main() -> None:
    """List all adapters and verify all 6 required adapters are present."""
    adapters_dir = Path(__file__).parent / "adapters"

    required_adapters = {
        "parse_structured_fields",
        "find_careers_url",
        "extract_jobs",
        "description_reformat",
        "company_research",
        "ai_nav_discovery",
    }

    # Find all adapter files
    found_adapters = set()
    for file in adapters_dir.glob("*_adapter.py"):
        adapter_name = file.stem.replace("_adapter", "")
        found_adapters.add(adapter_name)

    print("Found adapters:")
    for adapter in sorted(found_adapters):
        print(f"  - {adapter}")

    print("\nRequired adapters:")
    for adapter in sorted(required_adapters):
        status = "✓" if adapter in found_adapters else "✗"
        print(f"  {status} {adapter}")

    missing = required_adapters - found_adapters
    if missing:
        print(f"\nERROR: Missing adapters: {', '.join(sorted(missing))}")
        exit(1)
    else:
        print("\nAll 6 required adapters present.")
        exit(0)


if __name__ == "__main__":
    main()
