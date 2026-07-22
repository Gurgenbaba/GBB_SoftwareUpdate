param()

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $projectRoot

# Onedir-Build: niedrigere AV-False-Positive-Rate als Onefile.
# Verteile den gesamten Ordner dist\Compexx-InstallTool\, nicht nur die EXE.

$distDir = Join-Path $projectRoot "dist\Compexx-InstallTool"
$distExe = Join-Path $distDir "Compexx-InstallTool.exe"
# PyInstaller legt bei jedem Onedir-Build zusaetzlich eine verwaiste EXE direkt unter
# dist\ an (ohne _internal-Ordner). Die startet nicht eigenstaendig und sorgt nur fuer
# Verwirrung -> nach dem Build immer entfernen.
$strayExe = Join-Path $projectRoot "dist\Compexx-InstallTool.exe"
$buildDir = Join-Path $projectRoot "build\Compexx-InstallTool_onedir"

function Remove-DirWithRetry {
    param([string]$Path, [int]$Retries = 5)
    for ($i = 0; $i -lt $Retries; $i++) {
        try {
            Remove-Item -LiteralPath $Path -Recurse -Force -ErrorAction Stop
            return
        } catch {
            if ($i -eq ($Retries - 1)) { throw }
            Start-Sleep -Milliseconds 500
        }
    }
}

Get-Process -Name "Compexx-InstallTool" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Milliseconds 800

if (Test-Path -LiteralPath $distDir) {
    Remove-DirWithRetry -Path $distDir
}
if (Test-Path -LiteralPath $buildDir) {
    Remove-DirWithRetry -Path $buildDir
}
if (Test-Path -LiteralPath $strayExe) {
    Remove-Item -LiteralPath $strayExe -Force -ErrorAction SilentlyContinue
}

$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    throw "Projekt-venv fehlt: $venvPython"
}

& $venvPython -m compileall .

$venvPyInstaller = Join-Path $projectRoot ".venv\Scripts\pyinstaller.exe"
if (Test-Path $venvPyInstaller) {
    & $venvPyInstaller -y --clean Compexx-InstallTool_onedir.spec
} else {
    Write-Host "PyInstaller nicht in Projekt-venv gefunden, nutze PATH-Binary."
    pyinstaller -y --clean Compexx-InstallTool_onedir.spec
}
if ($LASTEXITCODE -ne 0) {
    throw "Build fehlgeschlagen: PyInstaller ExitCode $LASTEXITCODE"
}

if (-not (Test-Path -LiteralPath $distExe)) {
    throw "Build fehlgeschlagen: $distExe nicht gefunden."
}

if (Test-Path -LiteralPath $strayExe) {
    Remove-Item -LiteralPath $strayExe -Force -ErrorAction SilentlyContinue
}

$hash = Get-FileHash -Algorithm SHA256 -LiteralPath $distExe
Write-Host ""
Write-Host "Onedir-Build erfolgreich: $distDir"
Write-Host "EXE: $distExe"
Write-Host "SHA256 (EXE): $($hash.Hash)"
Write-Host ""
Write-Host "HINWEIS: Den gesamten Ordner '$distDir' verteilen, nicht nur die EXE."
