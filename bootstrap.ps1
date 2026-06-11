# bootstrap.ps1 -- end-user one-liner installer for Job Cannon (WP7).
#
#   irm https://raw.githubusercontent.com/Senkichi/job-cannon/main/bootstrap.ps1 | iex
#
# What it does: find Python 3.12+ (offer winget install if missing) -> ensure
# pipx (user-space pip install) -> pipx install/upgrade job-cannon -> launch.
# Every step is idempotent; re-running upgrades instead of erroring.
#
# This is NOT the contributor installer -- that's install.ps1, which syncs a
# git checkout with uv. This script is pipe-safe by construction:
#   - no param() block (parameters don't survive `iex`); config via env vars:
#       JC_BOOTSTRAP_YES=1        non-interactive (assume yes on prompts)
#       JC_BOOTSTRAP_NO_LAUNCH=1  install only, don't start the app
#   - whole body in a function invoked on the last line, so a partially
#     downloaded script is a no-op instead of executing half the steps.
#   - pipx is always invoked as `<python> -m pipx` -- `ensurepath` only edits
#     FUTURE sessions, so the bare `pipx` command may not exist in this one.

[Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSAvoidUsingWriteHost', '', Justification = 'interactive installer; user-facing progress output is the product')]
param()

function Invoke-JobCannonBootstrap {
    $ErrorActionPreference = 'Stop'
    $assumeYes = $env:JC_BOOTSTRAP_YES -eq '1'

    function Confirm-Step([string]$Question) {
        if ($assumeYes) { return $true }
        $answer = Read-Host "$Question [Y/n]"
        return ($answer -eq '' -or $answer -match '^[Yy]')
    }

    # --- Step 1: find Python 3.12+ -------------------------------------------
    Write-Host '==> Looking for Python 3.12+...'
    $py = $null
    $candidates = @(
        @{ Cmd = 'py'; Args = @('-3.13') },
        @{ Cmd = 'py'; Args = @('-3.12') },
        @{ Cmd = 'py'; Args = @('-3') },
        @{ Cmd = 'python'; Args = @() },
        @{ Cmd = 'python3'; Args = @() }
    )
    foreach ($c in $candidates) {
        if (-not (Get-Command $c.Cmd -ErrorAction SilentlyContinue)) { continue }
        try {
            $ver = & $c.Cmd @($c.Args) --version 2>$null
        } catch { continue }
        if ($ver -match 'Python (\d+)\.(\d+)') {
            $major = [int]$Matches[1]; $minor = [int]$Matches[2]
            if ($major -gt 3 -or ($major -eq 3 -and $minor -ge 12)) {
                $py = $c
                Write-Host "    found $ver ($($c.Cmd) $($c.Args -join ' '))"
                break
            }
        }
    }

    if ($null -eq $py) {
        Write-Host '    Python 3.12+ not found.'
        if (Get-Command winget -ErrorAction SilentlyContinue) {
            if (Confirm-Step '    Install Python 3.12 via winget?') {
                winget install --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
                if ($LASTEXITCODE -ne 0) {
                    Write-Host 'winget install failed. Install Python from https://www.python.org/downloads/ then re-run this script.'
                    return
                }
                # winget edits PATH for future sessions; `py` registers immediately.
                $py = @{ Cmd = 'py'; Args = @('-3.12') }
            } else {
                Write-Host 'Skipped. Install Python 3.12+ from https://www.python.org/downloads/ then re-run this script.'
                return
            }
        } else {
            Write-Host 'winget is not available. Install Python 3.12+ from https://www.python.org/downloads/ then re-run this script.'
            return
        }
    }

    function Invoke-Py { & $py.Cmd @($py.Args) @args }

    # --- Step 2: ensure pipx -------------------------------------------------
    Write-Host '==> Checking for pipx...'
    $pipxOk = $false
    try {
        Invoke-Py -m pipx --version *> $null
        $pipxOk = ($LASTEXITCODE -eq 0)
    } catch { $pipxOk = $false }

    if (-not $pipxOk) {
        Write-Host '    pipx not found -- installing into your user account (no admin needed)...'
        Invoke-Py -m pip install --user pipx
        if ($LASTEXITCODE -ne 0) {
            Write-Host 'pip install pipx failed -- see the output above.'
            return
        }
        Invoke-Py -m pipx ensurepath *> $null
        Write-Host '    pipx installed. (New terminals will have `pipx` on PATH.)'
    } else {
        Write-Host '    pipx is already installed.'
    }

    # --- Step 3: install or upgrade job-cannon ------------------------------
    $installed = ''
    try { $installed = Invoke-Py -m pipx list --short 2>$null | Out-String } catch { $installed = '' }
    if ($installed -match 'job-cannon') {
        Write-Host '==> job-cannon is already installed -- upgrading...'
        Invoke-Py -m pipx upgrade job-cannon
    } else {
        Write-Host '==> Installing job-cannon...'
        Invoke-Py -m pipx install job-cannon
    }
    if ($LASTEXITCODE -ne 0) {
        Write-Host 'pipx install failed -- see the output above.'
        return
    }

    # --- Step 4: launch ------------------------------------------------------
    if ($env:JC_BOOTSTRAP_NO_LAUNCH -eq '1') {
        Write-Host '==> Done. Start it any time with: job-cannon  (new terminal), and visit http://localhost:5000'
        return
    }
    $binDir = (Invoke-Py -m pipx environment --value PIPX_BIN_DIR 2>$null | Out-String).Trim()
    $exe = Join-Path $binDir 'job-cannon.exe'
    if (-not (Test-Path $exe)) {
        Write-Host "==> Installed, but couldn't resolve the launcher at $exe."
        Write-Host '    Open a NEW terminal and run: job-cannon'
        return
    }
    Write-Host '==> Launching Job Cannon (http://localhost:5000)...'
    & $exe
}

Invoke-JobCannonBootstrap
