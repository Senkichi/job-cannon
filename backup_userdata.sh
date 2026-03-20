#!/usr/bin/env bash
# Backs up untracked user-data files to backups/<timestamp>/
# Run manually or schedule as needed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TIMESTAMP="$(date +%Y-%m-%d_%H%M%S)"
BACKUP_DIR="$SCRIPT_DIR/backups/$TIMESTAMP"

FILES=(
    "config.yaml"
    "experience_profile.json"
    "experience_reference.md"
    "resume_style_guide.json"
)

mkdir -p "$BACKUP_DIR"

count=0
for f in "${FILES[@]}"; do
    src="$SCRIPT_DIR/$f"
    if [[ -f "$src" ]]; then
        cp "$src" "$BACKUP_DIR/"
        echo "  backed up: $f"
        count=$((count + 1))
    fi
done

if [[ $count -eq 0 ]]; then
    echo "No user-data files found to back up."
    rmdir "$BACKUP_DIR"
    exit 1
fi

echo ""
echo "Backed up $count file(s) to: backups/$TIMESTAMP/"
