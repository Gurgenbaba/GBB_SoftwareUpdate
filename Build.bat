@echo off
setlocal
title Compexx-InstallTool - Build
cd /d "%~dp0"

echo ============================================
echo   Compexx-InstallTool wird gebaut ...
echo ============================================
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\build_onedir.ps1"
set BUILD_RESULT=%ERRORLEVEL%

echo.
if %BUILD_RESULT% NEQ 0 (
    echo ============================================
    echo   BUILD FEHLGESCHLAGEN ^(Code %BUILD_RESULT%^)
    echo   Bitte die Meldungen oben pruefen.
    echo ============================================
) else (
    echo ============================================
    echo   BUILD ERFOLGREICH
    echo   Programm: dist\Compexx-InstallTool\Compexx-InstallTool.exe
    echo   Bitte den GESAMTEN Ordner "dist\Compexx-InstallTool" weitergeben,
    echo   nicht nur die EXE-Datei.
    echo ============================================
)

echo.
pause
endlocal
