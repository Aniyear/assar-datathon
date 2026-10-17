# Run from any folder. No installation, downloads, secrets or GPU work in this wrapper.
$ErrorActionPreference = 'Stop'
$repo = Split-Path $PSScriptRoot -Parent
$candidates = @((Join-Path $repo '.venv/Scripts/python.exe'),
    (Join-Path $repo '../../.venv/Scripts/python.exe'))
$python = $candidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $python) {
    Write-Error 'Python environment missing. Create .venv and install requirements.lock; see docs/SERVICE_OPERATIONS.md.'
    exit 2
}
Push-Location $repo
try { & $python -X utf8 -m scripts.service @args; exit $LASTEXITCODE }
finally { Pop-Location }
