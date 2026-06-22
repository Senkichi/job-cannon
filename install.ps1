<#
.SYNOPSIS
    Job Cannon bootstrap (Windows).

.DESCRIPTION
    Walks a fresh-machine user from a clean clone to a running app in their
    browser. Detects required tools (Python 3.13+, Git), installs uv via
    the official Astral installer if missing, runs `uv sync`, optionally
    installs Ollama + the `qwen2.5:14b` model and Node.js + the Claude Code
    CLI, then launches `uv run job-cannon` (which prints a URL banner and
    auto-opens the browser via the F2 entry-point work).

.PARAMETER Yes
    Accept every prompt non-interactively.

.PARAMETER Minimal
    Skip Ollama, Node, and the Claude Code CLI. Only does Python+Git
    detection, uv install, uv sync, and launch. For users on a paid
    Anthropic key who don't want the 9 GB Ollama download.

.PARAMETER NoLaunch
    Stop after install steps; do not run `uv run job-cannon`.

.NOTES
    UAT 2026-05-21 F5. Run from PowerShell 5.1+ or PowerShell 7+.
    If execution policy blocks the script, run once:
        Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
    or invoke with:
        powershell -ExecutionPolicy Bypass -File .\install.ps1
#>
[CmdletBinding()]
param(
    [switch]$Yes,
    [switch]$Minimal,
    [switch]$NoLaunch
)

# Intentionally not setting $ErrorActionPreference = 'Stop' globally — we
# want to surface "next manual command" guidance on failure rather than
# blow up silently. Native commands are checked via $LASTEXITCODE.

$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$PythonMinMajor = 3
$PythonMinMinor = 13
$OllamaModel = 'qwen2.5:14b'
$UvInstallUrl = 'https://astral.sh/uv/install.ps1'

# --- Output helpers ---
function Write-Info  { param([string]$m) Write-Host "==> $m" -ForegroundColor Blue }
function Write-Ok    { param([string]$m) Write-Host "[ok] $m" -ForegroundColor Green }
function Write-Warn2 { param([string]$m) Write-Host "[!]  $m" -ForegroundColor Yellow }
function Write-Fail  { param([string]$m) Write-Host "[x]  $m" -ForegroundColor Red }
function Write-Step  { param([string]$m) Write-Host ""; Write-Host $m -ForegroundColor Cyan -BackgroundColor Black }

function Confirm-Step {
    param([string]$Prompt)
    if ($Yes) { return $true }
    $reply = Read-Host "$Prompt [Y/n]"
    if ([string]::IsNullOrWhiteSpace($reply)) { return $true }
    return ($reply -match '^(y|yes)$')
}

# --- Banner ---
Write-Host ""
Write-Host "Job Cannon bootstrap (Windows)" -ForegroundColor White -BackgroundColor DarkBlue
Write-Host ""
Write-Host "This script will:"
Write-Host "  1. Check for Python $PythonMinMajor.$PythonMinMinor+ and Git (exits with install link if missing)."
Write-Host "  2. Install uv (Astral's Python package manager) if missing."
Write-Host "  3. Run 'uv sync --extra dev --extra eval' to install the project."
if (-not $Minimal) {
    Write-Host "  4. (Optional) Install Ollama + pull $OllamaModel (~9 GB)."
    Write-Host "  5. (Optional) Install Node.js + @anthropic-ai/claude-code CLI."
}
if (-not $NoLaunch) {
    $launchStep = if ($Minimal) { '4' } else { '6' }
    Write-Host "  $launchStep. Launch the app (uv run job-cannon) and open your browser."
}
Write-Host ""
if (-not (Confirm-Step "Continue?")) {
    Write-Info "Aborted."
    exit 0
}

# --- Step 1: Python ---
Write-Step "Step 1 - Checking Python $PythonMinMajor.$PythonMinMinor+"

$pyBin = $null
foreach ($candidate in @('python', 'python3', 'py')) {
    $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
    if (-not $cmd) { continue }
    # `py -3.13 -c "import sys; print(sys.version_info[:2])"` is the right
    # incantation for the Windows launcher. Plain `python -c ...` works for
    # the unprefixed binary.
    $args = if ($candidate -eq 'py') { @("-$PythonMinMajor.$PythonMinMinor", '-c', "import sys; print('%d.%d' % sys.version_info[:2])") }
            else { @('-c', "import sys; print('%d.%d' % sys.version_info[:2])") }
    $versionStr = & $candidate @args 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $versionStr) { continue }
    $parts = $versionStr.Trim().Split('.')
    if ($parts.Length -ne 2) { continue }
    $major = [int]$parts[0]
    $minor = [int]$parts[1]
    if ($major -gt $PythonMinMajor -or ($major -eq $PythonMinMajor -and $minor -ge $PythonMinMinor)) {
        $pyBin = $candidate
        Write-Ok "Found $candidate (Python $versionStr)"
        break
    }
}

if (-not $pyBin) {
    Write-Fail "Python $PythonMinMajor.$PythonMinMinor+ not found on PATH."
    Write-Host ""
    Write-Host "Install Python $PythonMinMajor.$PythonMinMinor from:"
    Write-Host "  - https://www.python.org/downloads/   (tick 'Add to PATH' in the installer)"
    Write-Host "  - or via winget:  winget install Python.Python.$PythonMinMajor.$PythonMinMinor"
    Write-Host ""
    Write-Host "Then re-run this script."
    exit 2
}

# --- Step 2: Git ---
Write-Step "Step 2 - Checking Git"

$git = Get-Command git -ErrorAction SilentlyContinue
if (-not $git) {
    Write-Fail "git not found on PATH."
    Write-Host ""
    Write-Host "Install Git from one of:"
    Write-Host "  - https://git-scm.com/download/win   (official installer)"
    Write-Host "  - winget install Git.Git             (winget)"
    Write-Host ""
    Write-Host "Then re-run this script."
    exit 2
}
$gitVer = (& git --version) -join ''
Write-Ok "Found git ($gitVer)"

# --- Step 3: uv ---
Write-Step "Step 3 - Installing uv (Astral)"

function Update-PathForUv {
    # uv's Windows installer drops the binary under %USERPROFILE%\.local\bin
    # (or %CARGO_HOME%\bin). Add both to PATH for the current process so
    # subsequent `uv` calls find it without requiring a new shell.
    $candidates = @(
        (Join-Path $env:USERPROFILE '.local\bin'),
        (Join-Path $env:USERPROFILE '.cargo\bin')
    )
    foreach ($p in $candidates) {
        if ((Test-Path $p) -and ($env:PATH -notlike "*$p*")) {
            $env:PATH = "$p;$env:PATH"
        }
    }
}

Update-PathForUv
$uv = Get-Command uv -ErrorAction SilentlyContinue
if ($uv) {
    $uvVer = (& uv --version) -join ''
    Write-Ok "Found uv ($uvVer)"
} else {
    Write-Info "uv is not installed."
    Write-Info "Will run: irm $UvInstallUrl | iex"
    if (Confirm-Step "Install uv now?") {
        try {
            Invoke-Expression (Invoke-RestMethod -Uri $UvInstallUrl)
        } catch {
            Write-Fail "uv install failed: $($_.Exception.Message)"
            Write-Host ""
            Write-Host "Try the manual command:"
            Write-Host "    irm $UvInstallUrl | iex"
            Write-Host ""
            Write-Host "Or see https://docs.astral.sh/uv/getting-started/installation/ for alternatives."
            exit 1
        }
        Update-PathForUv
        $uv = Get-Command uv -ErrorAction SilentlyContinue
        if (-not $uv) {
            Write-Fail "uv install completed but 'uv' was not found on PATH."
            Write-Warn2 "Open a new PowerShell window (so PATH refreshes) and re-run this script."
            exit 1
        }
        $uvVer = (& uv --version) -join ''
        Write-Ok "Installed uv ($uvVer)"
    } else {
        Write-Warn2 "Skipped uv install. Cannot continue without uv."
        exit 1
    }
}

# --- Step 4: uv sync ---
Write-Step "Step 4 - Syncing project dependencies (uv sync --extra dev --extra eval)"

Push-Location $RepoRoot
try {
    & uv sync --extra dev --extra eval
    if ($LASTEXITCODE -ne 0) {
        Write-Fail "uv sync failed."
        Write-Host ""
        Write-Host "Try the manual command from the repo root:"
        Write-Host "    uv sync --extra dev --extra eval"
        exit 1
    }
    Write-Ok "Project dependencies installed."
} finally {
    Pop-Location
}

# --- Step 5: Ollama (optional, skipped with -Minimal) ---
if (-not $Minimal) {
    Write-Step "Step 5 - Ollama + $OllamaModel"

    $ollama = Get-Command ollama -ErrorAction SilentlyContinue
    if ($ollama) {
        Write-Ok "Found ollama."
    } else {
        Write-Info "Ollama is not installed. It runs your local LLM (Job Cannon's free `$0 scoring tier)."
        if (Confirm-Step "Install Ollama now?") {
            $installed = $false
            $winget = Get-Command winget -ErrorAction SilentlyContinue
            if ($winget) {
                Write-Info "Using winget: winget install --id Ollama.Ollama -e --accept-source-agreements --accept-package-agreements"
                & winget install --id Ollama.Ollama -e --accept-source-agreements --accept-package-agreements
                if ($LASTEXITCODE -eq 0) { $installed = $true }
            }
            if (-not $installed) {
                Write-Fail "Could not install Ollama automatically."
                Write-Host ""
                Write-Host "Install manually from: https://ollama.com/download/windows"
                Write-Host "Then re-run this script."
                Write-Warn2 "Continuing without Ollama. Cascade fallbacks (Groq/Cerebras/Gemini/Anthropic) will be used."
            } else {
                # winget installs Ollama under %LOCALAPPDATA%\Programs\Ollama —
                # add it to PATH for the current process so the model pull below
                # works without a shell restart.
                $ollamaPath = Join-Path $env:LOCALAPPDATA 'Programs\Ollama'
                if ((Test-Path $ollamaPath) -and ($env:PATH -notlike "*$ollamaPath*")) {
                    $env:PATH = "$ollamaPath;$env:PATH"
                }
                Write-Ok "Installed Ollama."
            }
        } else {
            Write-Warn2 "Skipped Ollama. Cascade fallbacks will handle scoring."
        }
    }

    $ollama = Get-Command ollama -ErrorAction SilentlyContinue
    if ($ollama) {
        # Check if the model is already pulled.
        $modelLines = & ollama list 2>$null
        $hasModel = $false
        if ($modelLines) {
            foreach ($line in $modelLines) {
                if ($line -match "^$([Regex]::Escape($OllamaModel))(\s|:)") { $hasModel = $true; break }
            }
        }
        if ($hasModel) {
            Write-Ok "Model $OllamaModel already pulled."
        } else {
            Write-Warn2 "About to pull $OllamaModel (~9 GB download)."
            if (Confirm-Step "Pull $OllamaModel now?") {
                & ollama pull $OllamaModel
                if ($LASTEXITCODE -ne 0) {
                    Write-Fail "ollama pull $OllamaModel failed."
                    Write-Warn2 "You can retry manually: ollama pull $OllamaModel"
                } else {
                    Write-Ok "Pulled $OllamaModel."
                }
            } else {
                Write-Warn2 "Skipped model pull. Job Cannon will fall through the provider cascade."
            }
        }
    }

    # --- Step 6: Node + Claude Code CLI (optional) ---
    Write-Step "Step 6 - Node.js + Claude Code CLI"

    $node = Get-Command node -ErrorAction SilentlyContinue
    if ($node) {
        $nodeVer = (& node --version) -join ''
        Write-Ok "Found node ($nodeVer)"
    } else {
        Write-Info "Node.js is not installed. Job Cannon uses the Claude Code CLI as a `$0 fallback."
        if (Confirm-Step "Install Node.js now?") {
            $installed = $false
            $winget = Get-Command winget -ErrorAction SilentlyContinue
            if ($winget) {
                Write-Info "Using winget: winget install --id OpenJS.NodeJS.LTS -e --accept-source-agreements --accept-package-agreements"
                & winget install --id OpenJS.NodeJS.LTS -e --accept-source-agreements --accept-package-agreements
                if ($LASTEXITCODE -eq 0) { $installed = $true }
            }
            if (-not $installed) {
                Write-Fail "Could not install Node.js automatically."
                Write-Host ""
                Write-Host "Install manually from: https://nodejs.org/en/download/"
                Write-Host "Then re-run this script."
                Write-Warn2 "Continuing without Node + Claude Code CLI."
            } else {
                Write-Ok "Installed Node.js."
                Write-Warn2 "Open a new PowerShell window for 'npm' to land on PATH if the next step fails."
            }
        } else {
            Write-Warn2 "Skipped Node install. Claude Code CLI fallback will not be available."
        }
    }

    if (Get-Command node -ErrorAction SilentlyContinue) {
        if (Get-Command claude -ErrorAction SilentlyContinue) {
            Write-Ok "Found Claude Code CLI."
        } else {
            Write-Info "Will run: npm install -g @anthropic-ai/claude-code"
            if (Confirm-Step "Install the Claude Code CLI now?") {
                & npm install -g '@anthropic-ai/claude-code'
                if ($LASTEXITCODE -ne 0) {
                    Write-Fail "npm install -g @anthropic-ai/claude-code failed."
                    Write-Warn2 "You can retry manually: npm install -g @anthropic-ai/claude-code"
                } else {
                    Write-Ok "Installed Claude Code CLI."
                    Write-Host ""
                    Write-Host "Claude Code CLI installed. To log in (one-time, opens your browser):" -ForegroundColor White
                    Write-Host "    claude /login" -ForegroundColor Blue
                    Write-Host ""
                    Write-Host "Run that in your own terminal whenever you're ready. The app works without"
                    Write-Host "it; the CLI is one of several fallbacks in the scoring cascade."
                }
            } else {
                Write-Warn2 "Skipped Claude Code CLI."
            }
        }
    }
}

# --- Final step: launch ---
if (-not $NoLaunch) {
    Write-Step "Launching Job Cannon"
    Write-Host ""
    Write-Host "About to run: uv run job-cannon" -ForegroundColor White
    Write-Host ""
    Write-Host "This prints a URL banner and opens your default browser as soon as the server is ready."
    Write-Host "Press Ctrl+C in this terminal to stop the server."
    Write-Host ""
    if (-not (Confirm-Step "Launch now?")) {
        Write-Info "Skipped launch. Run 'uv run job-cannon' whenever you're ready."
        exit 0
    }
    Push-Location $RepoRoot
    try {
        & uv run job-cannon
    } finally {
        Pop-Location
    }
} else {
    Write-Ok "Bootstrap complete. Start the app with: uv run job-cannon"
}
