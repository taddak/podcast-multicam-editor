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

if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    Write-Host ""
    Write-Host "NOTE: ffmpeg was not found - the script needs it for audio analysis."
    Write-Host "Install it with: winget install Gyan.FFmpeg  (then restart Resolve)"
}

Write-Host ""
Write-Host "Done! In DaVinci Resolve (Studio) open your synced multicam timeline"
Write-Host "and run: Workspace -> Scripts -> Edit -> Podcast Multicam Editor"
