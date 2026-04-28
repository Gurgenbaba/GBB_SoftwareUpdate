$ErrorActionPreference = "Stop"
$scriptPath = Join-Path $PSScriptRoot "scripts\build.ps1"
if (-not (Test-Path $scriptPath)) {
    throw "scripts/build.ps1 nicht gefunden: $scriptPath"
}
& $scriptPath
