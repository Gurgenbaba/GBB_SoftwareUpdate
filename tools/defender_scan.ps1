<#
.SYNOPSIS
    Fuehrt einen lokalen Microsoft Defender Scan auf einer EXE oder einem Ordner durch.
    Kein Internet-Zugang erforderlich. Keine automatische VirusTotal-Abfrage.

.PARAMETER Target
    Pfad zur EXE-Datei oder zum Verzeichnis, das gescannt werden soll.
    Standardmaessig: dist\Compexx-InstallTool\ (Onedir-Build)

.EXAMPLE
    .\tools\defender_scan.ps1
    .\tools\defender_scan.ps1 -Target "dist\Compexx-InstallTool.exe"
    .\tools\defender_scan.ps1 -Target "dist\Compexx-InstallTool"
#>
param(
    [string]$Target = ""
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

# Standard-Target: Onedir-Build (bevorzugt)
if (-not $Target) {
    $onedir = Join-Path $projectRoot "dist\Compexx-InstallTool"
    $onefile = Join-Path $projectRoot "dist\Compexx-InstallTool.exe"
    if (Test-Path -LiteralPath $onedir) {
        $Target = $onedir
    } elseif (Test-Path -LiteralPath $onefile) {
        $Target = $onefile
    } else {
        Write-Error "Kein Build-Artefakt gefunden. Zuerst build.ps1 oder build_onedir.ps1 ausfuehren."
        exit 1
    }
}

$resolvedTarget = Resolve-Path $Target -ErrorAction Stop

# MpCmdRun.exe robustly finden
$mpCmdRunCandidates = @(
    "$env:ProgramFiles\Windows Defender\MpCmdRun.exe",
    "${env:ProgramFiles(x86)}\Windows Defender\MpCmdRun.exe",
    "$env:ProgramData\Microsoft\Windows Defender\Platform\*\MpCmdRun.exe"
)

$mpCmdRun = $null
foreach ($candidate in $mpCmdRunCandidates) {
    $found = Get-Item $candidate -ErrorAction SilentlyContinue | Select-Object -Last 1
    if ($found) {
        $mpCmdRun = $found.FullName
        break
    }
}

if (-not $mpCmdRun) {
    Write-Error "MpCmdRun.exe nicht gefunden. Windows Defender muss installiert sein."
    exit 2
}

Write-Host "Defender: $mpCmdRun"
Write-Host "Target:   $resolvedTarget"
Write-Host ""

# SHA256 vor dem Scan ausgeben (fuer manuelle VT-Abfrage)
if (Test-Path -LiteralPath $resolvedTarget -PathType Leaf) {
    $hash = Get-FileHash -Algorithm SHA256 -LiteralPath $resolvedTarget
    Write-Host "SHA256: $($hash.Hash)"
    Write-Host "VT-Link (manuell oeffnen): https://www.virustotal.com/gui/file/$($hash.Hash.ToLower())"
    Write-Host ""
}

Write-Host "Starte Defender Custom Scan (ScanType 3)..."
$proc = Start-Process -FilePath $mpCmdRun `
    -ArgumentList "-Scan", "-ScanType", "3", "-File", "`"$resolvedTarget`"" `
    -Wait -PassThru -NoNewWindow

if ($proc.ExitCode -eq 0) {
    Write-Host ""
    Write-Host "Defender: Keine Bedrohung erkannt (ExitCode 0)."
    exit 0
} else {
    Write-Host ""
    Write-Warning "Defender: ExitCode $($proc.ExitCode) — moeglicherweise Bedrohung erkannt oder Scan-Fehler."
    Write-Host "Pruefe Windows Defender Verlauf fuer Details."
    exit $proc.ExitCode
}
