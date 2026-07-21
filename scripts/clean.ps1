param(
    [switch]$Runtime
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

$targets = @(
    (Join-Path $projectRoot "build"),
    (Join-Path $projectRoot "dist"),
    (Join-Path $projectRoot "__pycache__")
)

foreach ($target in $targets) {
    if (Test-Path $target) {
        Remove-Item -Recurse -Force $target
        Write-Host "Entfernt: $target"
    }
}

Get-ChildItem -Path $projectRoot -Recurse -Directory -Filter "__pycache__" -ErrorAction SilentlyContinue | Where-Object {
    $_.FullName -notlike "*\.venv\*"
} | ForEach-Object {
    Remove-Item -Recurse -Force $_.FullName
    Write-Host "Entfernt: $($_.FullName)"
}

if ($Runtime) {
    $runtimeDir = Join-Path $env:LOCALAPPDATA "Compexx-InstallTool"
    if (Test-Path $runtimeDir) {
        Remove-Item -Recurse -Force $runtimeDir
        Write-Host "Runtime-Daten entfernt: $runtimeDir"
    }
}
