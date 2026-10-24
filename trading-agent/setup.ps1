# One-time setup on Windows: private virtualenv, hash-verified dependencies, config, tests.
# Run in PowerShell:  powershell -ExecutionPolicy Bypass -File .\setup.ps1
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function Test-Python([string[]]$cmd) {
    try {
        $exe = $cmd[0]
        $rest = @($cmd | Select-Object -Skip 1)
        & $exe @rest -c "import sys; sys.exit(0 if (3, 11) <= sys.version_info[:2] <= (3, 14) else 1)" 2>$null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    }
}

$py = $null
foreach ($candidate in @(@("py", "-3.13"), @("py", "-3.12"), @("py", "-3.14"), @("py", "-3.11"), @("python"))) {
    if (Test-Python $candidate) { $py = $candidate; break }
}
if (-not $py) {
    Write-Host "Python 3.11-3.14 not found. Install Python 3.13 from https://www.python.org/downloads/ and run this again."
    exit 1
}
$pyExe = $py[0]
$pyArgs = @($py | Select-Object -Skip 1)
Write-Host "Using $(& $pyExe @pyArgs --version)"

if (-not (Test-Path .venv)) { & $pyExe @pyArgs -m venv .venv }
$vpy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"

Write-Host "Installing dependencies (every file checked against its SHA-256 hash)..."
& $vpy -m pip install --quiet --disable-pip-version-check --require-hashes --only-binary=:all: -r requirements.lock
if ($LASTEXITCODE -ne 0) {
    Write-Host "No prebuilt package for this platform; building the hash-verified source packages instead."
    & $vpy -m pip install --quiet --disable-pip-version-check --require-hashes -r requirements.lock
    if ($LASTEXITCODE -ne 0) { exit 1 }
}
& $vpy -m pip install --quiet --disable-pip-version-check --require-hashes --only-binary=:all: -r requirements-dev.lock
if ($LASTEXITCODE -ne 0) { exit 1 }

if (-not (Test-Path config.toml)) { Copy-Item config.example.toml config.toml }
if (-not (Test-Path .env)) { New-Item -ItemType File -Path .env | Out-Null }
# Only the current user may read or change the key file.
icacls .env /inheritance:r /grant:r "$($env:USERDOMAIN)\$($env:USERNAME):(R,W)" | Out-Null
if (-not (Test-Path runs)) { New-Item -ItemType Directory -Path runs | Out-Null }

Write-Host "Running tests..."
& $vpy -m pytest -q
if ($LASTEXITCODE -ne 0) { exit 1 }

Write-Host ""
Write-Host "Setup complete. Next steps:"
Write-Host "  .\.venv\Scripts\Activate.ps1"
Write-Host "  python -m trader keys      # enter API keys (hidden input, stored only in .env)"
Write-Host "  python -m trader check     # security check, exchange data, keys"
Write-Host "  python -m trader run --name paper-week     # one week of paper trading"
