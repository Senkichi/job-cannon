"""Self-test for _gather_since's rotation-safety.

Constructs a temporary logs/ dir, drops in synthetic rotated files mimicking
the state right after a RotatingFileHandler rotation: a `database is locked`
line in app.log.1, an unrelated old line in app.log.2, and only the new
session's preamble in app.log. Asserts _gather_since picks up the line in
app.log.1 (which the prior byte-offset implementation would have missed).
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

# Make the script's helpers importable without running main().
SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import concurrent_overlap_test as cot  # noqa: E402


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def main() -> int:
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        log_dir = Path(tmp)

        # State of files at "inspection time", after a rotation happened
        # mid-test. The threshold corresponds to ~start of the test window.
        # - app.log.3: very old, fully out-of-window.
        # - app.log.2: contains the BEFORE-threshold tail (must be filtered out).
        # - app.log.1: pre-rotation tail of the test window — contains the
        #              `database is locked` line the old impl missed.
        # - app.log:   post-rotation new file with the trailing test output.
        _write(
            log_dir / "app.log.3",
            "2026-05-18 09:00:00 INFO foo: ancient line\n",
        )
        _write(
            log_dir / "app.log.2",
            "2026-05-19 10:00:00 INFO foo: before threshold\n"
            "2026-05-19 10:30:00 INFO foo: still before threshold\n",
        )
        _write(
            log_dir / "app.log.1",
            "2026-05-19 11:00:05 INFO foo: in-window pre-rotation\n"
            "2026-05-19 11:00:10 ERROR sqlite3.OperationalError: database is locked\n"
            "Traceback (most recent call last):\n"
            '  File "x.py", line 1, in <module>\n'
            "2026-05-19 11:00:11 INFO bar: Orphan cleanup: 0 deleted\n",
        )
        _write(
            log_dir / "app.log",
            "2026-05-19 11:00:30 INFO baz: Registry hygiene: ok\n",
        )

        # Point the module at our temp dir.
        cot.LOG_DIR = log_dir
        cot.LOG_PATH = log_dir / "app.log"

        threshold = "2026-05-19 11:00:00"
        chunk = cot._gather_since(threshold)

        # Assertions.
        if "database is locked" not in chunk:
            failures.append(
                "REGRESSION: missed 'database is locked' line in app.log.1"
            )
        if "ancient line" in chunk:
            failures.append("BUG: included app.log.3 ancient line")
        if "before threshold" in chunk:
            failures.append("BUG: included pre-threshold line from app.log.2")
        if "Orphan cleanup:" not in chunk:
            failures.append("BUG: missed Orphan cleanup completion in app.log.1")
        if "Registry hygiene:" not in chunk:
            failures.append("BUG: missed Registry hygiene completion in app.log")
        # Traceback continuation line should be in-window too (no timestamp,
        # but inherits in-window from prior timestamped line).
        if 'File "x.py"' not in chunk:
            failures.append(
                "BUG: missed traceback continuation line (no inherited window state)"
            )

        # Old-impl simulation: byte-offset at app.log.size_at_start (small),
        # then reading post-rotation file finds only "Registry hygiene" line
        # and crucially MISSES the database-is-locked one. That's the bug we
        # are fixing; our new impl must include it.
        # (No assertion on the OLD impl — just commentary.)

    if failures:
        print("FAIL:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS: _gather_since picks up app.log.1 content the old impl missed")
    print("  - included: database is locked (app.log.1)")
    print("  - included: Orphan cleanup (app.log.1)")
    print("  - included: Registry hygiene (app.log)")
    print("  - included: Traceback continuation (inherited in-window state)")
    print("  - excluded: ancient app.log.3")
    print("  - excluded: pre-threshold lines in app.log.2")
    return 0


if __name__ == "__main__":
    sys.exit(main())
