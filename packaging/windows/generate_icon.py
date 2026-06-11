"""Regenerate packaging/windows/job-cannon.ico from the bundled tray icon.

The .ico is committed (build inputs must not depend on a generation step),
but this script is the reproducible source of truth: run it whenever
job_finder/assets/tray_icon.png changes.

    uv run python packaging/windows/generate_icon.py

The source PNG is 64x64; the 256px frame is a LANCZOS upscale. That is
acceptable for this glyph (flat shapes, no fine detail) and beats shipping
no 256px frame, which makes Explorer's large-icon view render the 48px
frame blurrier than a deliberate upscale.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image

_SIZES = [16, 32, 48, 64, 256]


def main() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    src = repo_root / "job_finder" / "assets" / "tray_icon.png"
    dst = Path(__file__).resolve().parent / "job-cannon.ico"

    base = Image.open(src).convert("RGBA")
    frames = [base.resize((s, s), Image.LANCZOS) for s in _SIZES]
    # Pillow writes a multi-resolution .ico from append_images; the first
    # frame's size list drives which resolutions are embedded.
    frames[0].save(
        dst,
        format="ICO",
        append_images=frames[1:],
        sizes=[(s, s) for s in _SIZES],
    )
    print(f"Wrote {dst} ({dst.stat().st_size} bytes, sizes={_SIZES})")


if __name__ == "__main__":
    main()
