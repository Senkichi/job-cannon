<!-- .planning/v5.0/HYPER-V-VM-BRINGUP.md -->

# Hyper-V Ubuntu 22.04 LTS Bringup (Phase 45 PYPI-06 hands-on)

**Author-local infrastructure note. NOT distributed to end users.**

Per CONTEXT D-02, the primary Linux validation surface is GitHub Actions `ubuntu-latest`. This file documents the author's secondary hands-on layer: a Hyper-V Ubuntu 22.04 LTS VM standing in for "user running pipx + wizard on a fresh Linux machine."

INSTALL.md does NOT recommend a specific VM solution (Hyper-V is author-local; users may use anything).

## Bringup steps

1. **Create VM**
   - Hyper-V Manager → Quick Create → Ubuntu 22.04 LTS image
   - Allocate ≥ 4 GB RAM, ≥ 30 GB disk, default networking
2. **First boot**
   - Complete Ubuntu installer; create username matching author preference
   - `sudo apt update && sudo apt upgrade -y`
3. **Prereqs for pipx**
   - `sudo apt install -y pipx python3-venv git` (per RESEARCH Pitfall 3: apt-packaged pipx 1.4.3 is fine for our smoke commands; no `pipx upgrade pipx` needed)
   - `pipx ensurepath` then log out / log in (PATH refresh per RESEARCH Pitfall 2)
4. **Validate base install path (Phase 45 SC3)**
   - Download wheel artifact from the release run that this validation is checking: `gh run download <run-id> --name dist`
   - `WHEEL=$(python3 -c "import glob; print(glob.glob('dist/job_cannon-*.whl')[0])")`
   - `pipx install "$WHEEL"`
   - `job-cannon --help` must exit 0
5. **Run wizard end-to-end**
   - `job-cannon` (launches Flask on localhost:5000)
   - Open browser, complete wizard (welcome → provider → IMAP app-password → resume → schedule)
   - Confirm ≥ 1 scored job appears in dashboard
6. **Snapshot**
   - Hyper-V Manager → snapshot named `post-pipx-install` after step 4 succeeds
   - Re-runs revert to this snapshot for cheap re-validation

## Re-validation rubric (after re-running)

Each re-run records a one-line outcome in `.planning/v5.0/PYPI-GATE-attestations.md > Author validation log` table:
- Date (YYYY-MM-DD)
- Outcome: `wizard completed, N scored jobs visible` OR `failure at step X (one-line description)`
