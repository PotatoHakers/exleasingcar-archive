param(
    [int]$Port = 8765
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

$python = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    throw "Virtual environment not found. Follow docs/FIRST_GLOBAL_RUN.md first."
}

& $python -m exleasingcar_archive.dashboard --database data/exleasingcar_archive.sqlite3 --host 127.0.0.1 --port $Port
exit $LASTEXITCODE
