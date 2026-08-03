# Installer for Podcast Multicam Editor (Windows).
# Copies the script into DaVinci Resolve's user scripts folder.

$RawUrl = "https://raw.githubusercontent.com/taddak/podcast-multicam-editor/main/Podcast%20Multicam%20Editor.py"
$Name = "Podcast Multicam Editor.py"
$TargetDir = Join-Path $env:APPDATA "Blackmagic Design\DaVinci Resolve\Support\Fusion\Scripts\Edit"

New-Item -ItemType Directory -Force -Path $TargetDir | Out-Null
$Target = Join-Path $TargetDir $Name

# prefer a local copy (running from a cloned repo / unpacked zip)
$Local = Join-Path $PSScriptRoot $Name
if ($PSScriptRoot -and (Test-Path $Local)) {
    Copy-Item $Local $Target -Force
    Write-Host "Installed from local copy."
} else {
    Invoke-WebRequest -Uri $RawUrl -OutFile $Target -UseBasicParsing
    Write-Host "Downloaded the latest version."
}

Write-Host "Installed to: $Target"

$Missing = $false

if (-not (Get-Command python -ErrorAction SilentlyContinue) -and
    -not (Get-Command python3 -ErrorAction SilentlyContinue)) {
    $Missing = $true
    Write-Host ""
    Write-Host "MISSING: Python 3 - without it Resolve hides .py scripts from the menu."
    Write-Host "  Install it with: winget install Python.Python.3.12"
    Write-Host "  (or download it from https://www.python.org/downloads/)"
}

if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    $Missing = $true
    Write-Host ""
    Write-Host "MISSING: ffmpeg - the script needs it for the audio analysis."
    Write-Host "  Install it with: winget install Gyan.FFmpeg"
}

Write-Host ""
if ($Missing) {
    Write-Host "Script installed. Install the missing pieces above, then restart"
    Write-Host "DaVinci Resolve (Studio) and run:"
} else {
    Write-Host "Done! In DaVinci Resolve (Studio) open your synced multicam timeline"
    Write-Host "and run:"
}
Write-Host "  Workspace -> Scripts -> Edit -> Podcast Multicam Editor"
