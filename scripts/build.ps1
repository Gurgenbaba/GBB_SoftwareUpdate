param()

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $projectRoot

$distExe = Join-Path $projectRoot "dist\GBB Updater.exe"
Get-Process -Name "GBB Updater" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Milliseconds 800
if (Test-Path -LiteralPath $distExe) {
    try {
        Remove-Item -LiteralPath $distExe -Force
    } catch {
        $bak = "$distExe.bak-$(Get-Date -Format 'yyyyMMddHHmmss')"
        Move-Item -LiteralPath $distExe -Destination $bak -Force
    }
}

$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    throw "Projekt-venv fehlt: $venvPython"
}

& $venvPython -m compileall .
$venvPyInstaller = Join-Path $projectRoot ".venv\Scripts\pyinstaller.exe"
if (Test-Path $venvPyInstaller) {
    & $venvPyInstaller -y --clean GBB_SoftwareUpdater.spec
} else {
    Write-Host "PyInstaller nicht in Projekt-venv gefunden, nutze PATH-Binary."
    pyinstaller -y --clean GBB_SoftwareUpdater.spec
}
if ($LASTEXITCODE -ne 0) {
    throw "Build fehlgeschlagen: PyInstaller ExitCode $LASTEXITCODE"
}

if (-not (Test-Path -LiteralPath $distExe)) {
    throw "Build fehlgeschlagen: $distExe nicht gefunden."
}

$hash = Get-FileHash -Algorithm SHA256 -LiteralPath $distExe
Write-Host "Build erfolgreich: $distExe"
Write-Host "SHA256: $($hash.Hash)"
