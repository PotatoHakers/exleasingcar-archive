param(
    [switch]$PlanOnly,
    [switch]$ForceDiscovery,
    [int]$MaxRunSeconds = 900
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

$python = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    throw "Virtual environment not found. Follow docs/FIRST_GLOBAL_RUN.md first."
}

$database = "data/exleasingcar_archive.sqlite3"
$profile = "data/browser_profile"
$raw = "data/raw/collector"

$collectorArgs = @(
    "-m", "exleasingcar_archive.collector",
    "--database", $database,
    "--profile-dir", $profile,
    "--raw-dir", $raw,
    "--max-auctions", "0",
    "--max-run-seconds", "$MaxRunSeconds"
)

if ($PlanOnly) {
    $collectorArgs += "--plan-only"
}
if ($ForceDiscovery) {
    $collectorArgs += "--force-auction-discovery"
}

& $python @collectorArgs
exit $LASTEXITCODE
