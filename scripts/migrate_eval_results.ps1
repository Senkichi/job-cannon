# scripts/migrate_eval_results.ps1
#
# One-time migration helper for Reconciliation R7.1 (2026-05-06).
#
# Background: through Phases 4 and 5 the eval harness wrote markdown
# reports to ``.planning/eval_results/`` by default. R7.1 moved the
# default to ``eval_results/`` at repo root so production code no
# longer references a ``.planning/`` path. Existing reports remain
# at the old location (still tracked via the ``.planning/eval_results/``
# gitignore exception); this script copies them into the new
# location so a re-run of the harness sees historical context.
#
# Idempotent: safe to run repeatedly. Only copies files that are
# absent or out-of-date at the destination. Does NOT delete from the
# source — the .planning/eval_results/ tree remains the canonical
# version-controlled portfolio archive.
#
# Usage (from the repo root, in PowerShell):
#   .\scripts\migrate_eval_results.ps1

[CmdletBinding()]
param(
    [string]$Source = ".planning/eval_results",
    [string]$Destination = "eval_results"
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path $Source)) {
    Write-Host "Source '$Source' does not exist — nothing to migrate."
    exit 0
}

$srcItems = Get-ChildItem -Path $Source -File
if ($srcItems.Count -eq 0) {
    Write-Host "Source '$Source' is empty — nothing to migrate."
    exit 0
}

if (-not (Test-Path $Destination)) {
    New-Item -ItemType Directory -Path $Destination | Out-Null
    Write-Host "Created '$Destination/'"
}

$copied = 0
$skipped = 0
foreach ($item in $srcItems) {
    $destPath = Join-Path $Destination $item.Name
    if (Test-Path $destPath) {
        $destFile = Get-Item $destPath
        if ($destFile.Length -eq $item.Length -and $destFile.LastWriteTime -ge $item.LastWriteTime) {
            $skipped++
            continue
        }
    }
    Copy-Item -Path $item.FullName -Destination $destPath -Force
    $copied++
}

Write-Host ""
Write-Host "Migration summary:"
Write-Host "  Source:      $Source"
Write-Host "  Destination: $Destination"
Write-Host "  Copied:      $copied"
Write-Host "  Skipped (already up to date): $skipped"
Write-Host ""
Write-Host "Source directory left intact — '.planning/eval_results/' remains the version-controlled archive."
