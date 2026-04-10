"""Remove all `patch("...anthropic...")` context managers from test files.

The conftest.py `mock_run_oneshot` autouse fixture already prevents real CLI calls,
so these patches are no longer needed. Handles:
- Standalone `with patch("...anthropic..."):` blocks -> de-indent body
- Multi-line `with` blocks with anthropic patch lines -> remove just those lines
- `@patch("...anthropic...")` decorators -> remove
- `mock_anthropic.Anthropic.return_value = ...` setup lines -> remove
"""

import re
import os
import sys


def remove_anthropic_patches(filepath):
    with open(filepath, "r", encoding="utf-8") as f:
        lines = f.readlines()

    original = lines[:]
    result = []
    i = 0

    while i < len(lines):
        line = lines[i]
        stripped = line.rstrip()
        lstripped = line.lstrip()

        # Pattern: @patch("...anthropic...") decorator
        if lstripped.startswith("@patch(") and "anthropic" in lstripped:
            i += 1
            continue

        # Pattern: with patch("...anthropic...") as ..., \ (multi-line continuation)
        if ("patch(" in stripped and "anthropic" in stripped and
                stripped.endswith("\\")):
            # This is part of a multi-line with — skip this line
            # Also skip the next line if it's another anthropic patch continuation
            i += 1
            if i < len(lines) and "anthropic" in lines[i] and lines[i].rstrip().endswith("\\"):
                i += 1
            continue

        # Pattern: standalone with patch("...anthropic...") as ...:
        # or with patch("...anthropic..."):
        if (lstripped.startswith("with patch(") and "anthropic" in lstripped
                and stripped.endswith(":")):
            # This is a standalone with block — de-indent the body
            indent = len(line) - len(lstripped)
            i += 1
            # Skip mock_anthropic.Anthropic.return_value setup lines
            while i < len(lines):
                next_line = lines[i]
                next_stripped = next_line.strip()
                if (next_stripped.startswith("mock_anthropic") or
                    next_stripped.startswith("mock_") and "Anthropic" in next_stripped):
                    i += 1
                    continue
                break
            # De-indent remaining body by one level (4 spaces)
            while i < len(lines):
                body_line = lines[i]
                body_stripped = body_line.strip()
                if not body_stripped:
                    result.append("\n")
                    i += 1
                    continue
                body_indent = len(body_line) - len(body_line.lstrip())
                if body_indent <= indent:
                    break  # Left the with block
                # De-indent by 4 spaces
                dedented = body_line[4:] if body_line.startswith("    " * ((indent // 4) + 1)) else body_line
                result.append(dedented)
                i += 1
            continue

        # Pattern: patch("...anthropic...", None): as part of with
        if "patch(" in stripped and "anthropic" in stripped:
            # Just skip this line
            i += 1
            continue

        # Pattern: mock_anthropic.Anthropic.return_value = ...
        if lstripped.startswith("mock_anthropic") and "Anthropic" in lstripped:
            i += 1
            continue
        if lstripped.startswith("mock_") and ".Anthropic.return_value" in lstripped:
            i += 1
            continue

        result.append(line)
        i += 1

    # Clean up excessive blank lines
    content = "".join(result)
    content = re.sub(r"\n{3,}", "\n\n", content)

    if content != "".join(original):
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)
        return True
    return False


if __name__ == "__main__":
    test_dir = "tests"
    changed = []
    for f in sorted(os.listdir(test_dir)):
        if f.endswith(".py"):
            path = os.path.join(test_dir, f)
            if remove_anthropic_patches(path):
                changed.append(path)

    print(f"Modified {len(changed)} files:")
    for p in changed:
        print(f"  {p}")
