#!/usr/bin/env bash
# Audit: find call_claude() bypasses of the provider cascade.
#
# Every new feature's AI calls should dispatch through call_model() so Ollama
# routing is universally available (see
# .planning/OLLAMA_CASCADE_MIGRATION_PLAN.md). The only files permitted to
# import call_claude directly are:
#
#  1. Infrastructure:     claude_client.py (defines the function),
#                         model_provider.py + providers/anthropic_provider.py
#                         (cascade implementation)
#  2. Migrated call sites: import call_claude only for the
#                          ProviderCascadeExhaustedError fallback branch,
#                          AND also import call_model from model_provider
#  3. Known-legacy bypasses: resume/interview/rejection flows that predate
#                            the cascade migration. These are intentionally
#                            skipped until a follow-up phase migrates them.
#                            Adding new files to this list is explicitly a
#                            regression and requires documenting why.
#
# The script exits non-zero when a file imports call_claude without also
# importing call_model and is not on the allowlist below — i.e. a genuinely
# new bypass has appeared.
set -euo pipefail

root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

# Cascade infrastructure — owns the function or implements the dispatcher.
INFRA_FILES=(
    "job_finder/web/claude_client.py"
    "job_finder/web/model_provider.py"
    "job_finder/web/providers/anthropic_provider.py"
)

# Pre-cascade features. Not called during the hot ingestion / scoring path
# and therefore out of the Ollama cascade plan's scope. Migrate these in a
# follow-up; do not add new entries without an accompanying plan.
LEGACY_BYPASS_FILES=()

is_exempt() {
    local f="$1"
    for e in "${INFRA_FILES[@]}" "${LEGACY_BYPASS_FILES[@]}"; do
        if [ "$f" = "$e" ]; then
            return 0
        fi
    done
    return 1
}

echo "=== Files importing call_claude ==="
new_bypasses=0
covered_migrations=0
while IFS= read -r file; do
    if printf '%s\n' "${INFRA_FILES[@]}" | grep -qxF "$file"; then
        printf '  INFRA:  %s\n' "$file"
        continue
    fi
    if printf '%s\n' "${LEGACY_BYPASS_FILES[@]}" | grep -qxF "$file"; then
        printf '  LEGACY: %s (allowlisted; migrate in a follow-up)\n' "$file"
        continue
    fi
    if grep -q 'from[[:space:]]\+job_finder\.web\.model_provider[[:space:]]\+import[[:space:]].*call_model' "$file"; then
        printf '  OK:     %s (also imports call_model)\n' "$file"
        covered_migrations=$((covered_migrations + 1))
        continue
    fi
    printf '  BYPASS: %s (imports call_claude, does NOT import call_model)\n' "$file"
    new_bypasses=$((new_bypasses + 1))
done < <(grep -rl --include='*.py' \
    'from[[:space:]]\+job_finder\.web\.claude_client[[:space:]]\+import[[:space:]].*call_claude' \
    job_finder/ 2>/dev/null | sort)

echo ""
echo "Summary: ${covered_migrations} migrated call sites, ${#LEGACY_BYPASS_FILES[@]} legacy-allowlisted, ${new_bypasses} new bypasses."

if [ "$new_bypasses" -ne 0 ]; then
    echo ""
    echo "Cascade audit: FAIL — migrate the new bypass sites or add them to the"
    echo "  LEGACY_BYPASS_FILES allowlist in $(basename "$0") with a justifying comment."
    exit 1
fi

echo "Cascade audit: PASS"
exit 0
