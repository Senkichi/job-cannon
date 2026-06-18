# Runbook — #306 git history purge

One-time, **human-executed** rewrite that strips internal planning/eval
artifacts from public git history before launch (v5 audit B9 / issue #306).

The automated prep for this runbook is `scripts/prep_history_purge_306.py`
(issue #462): it verifies preconditions, inventories the paths to strip, and
prints the exact `git filter-repo` invocation. **The prep script is
read-only — it never rewrites history, force-pushes, or toggles branch
protection.** Those steps are the human leaf of #306 and live here.

> ⚠️ **STOP — the destructive force-push to protected `main` is a HUMAN step.**
> No script in this repo performs it. Disable branch protection, force-push the
> rewritten refs + tags, then re-enable protection, by hand.
>
> ⚠️ **This does not un-leak anything already cloned.** Anyone who cloned the
> repo before the rewrite still holds the purged data locally and on any forks.
> The purge only prevents *future* clones from receiving it.

## What gets stripped

| Path | Why | Approx. count |
|------|-----|---------------|
| `.planning/` | Internal planning + working notes (already gitignored) | ~182 |
| `PLAN.md`, `FOLLOWUPS.md`, `JD-LAYER2-PLAN.md` | Root planning docs (already untracked) | 3 |
| `evals/cascade_audit/artifacts/round_0/jd/` | Captured job-description eval inputs | ~24 |

**Retained on purpose — do NOT strip:**
`evals/cascade_audit/artifacts/round_0/dedup_keys.json`
(re-included via a `.gitignore` negation). The `--path` argument is
`evals/cascade_audit/artifacts/round_0/jd/` **exactly** — never the bare
`evals/cascade_audit/artifacts/round_0/`, which would nuke `dedup_keys.json`.

## Precondition checklist

Run the prep script — every check must print `PASS` and the inventory floors
must all be `OK` (it exits non-zero otherwise):

```powershell
uv run python scripts/prep_history_purge_306.py
```

It verifies:

1. `git filter-repo` is installed (binary on PATH / clean `--version` exit).
2. `.planning/` contents are gitignored.
3. The three root planning docs are no longer tracked.

If any check fails, fix that first — do **not** proceed to the rewrite.

## Procedure

### 1. Clone a throwaway working copy

`git filter-repo` rewrites every commit SHA, so never run it against your
primary clone or any worktree.

```powershell
git clone --no-local <origin-url> jc-history-purge
cd jc-history-purge
git filter-repo --analyze   # optional — inspect .git/filter-repo/analysis/
```

### 2. Run the exact rewrite invocation

```bash
git filter-repo --invert-paths \
  --path .planning/ \
  --path PLAN.md \
  --path FOLLOWUPS.md \
  --path JD-LAYER2-PLAN.md \
  --path evals/cascade_audit/artifacts/round_0/jd/
```

(The prep script prints this same command — copy it from there to avoid typos.)

### 3. Verify the purge

This grep **must return nothing**:

```bash
git log --all --name-only | grep -E "^\.planning/|^PLAN\.md|^FOLLOWUPS\.md|^JD-LAYER2-PLAN\.md|round_0/jd/"
```

Confirm the intentionally-retained file **survives**:

```bash
git log --all --name-only | grep -F "evals/cascade_audit/artifacts/round_0/dedup_keys.json"
```

### 4. Force-push (HUMAN step — destructive, irreversible on the remote)

1. Disable branch protection on `main` (GitHub → Settings → Branches).
2. Force-push the rewritten history and tags:
   ```bash
   git push --force --all
   git push --force --tags
   ```
3. Re-enable branch protection on `main` with the original settings.

## Aftermath checklist (#306)

- [ ] Re-clone any worktrees / working copies from the rewritten remote
      (old clones carry the pre-rewrite SHAs and will diverge).
- [ ] Rebase or recreate any open PR branches onto the new history.
- [ ] Verify all tags resolve to the rewritten commits.
- [ ] Confirm CI is green on the rewritten `main`.
- [ ] Re-enable branch protection (if not already done in step 4).
- [ ] Acknowledge that prior clones/forks retain the purged data — rotate any
      secrets that may have been committed historically if applicable.
