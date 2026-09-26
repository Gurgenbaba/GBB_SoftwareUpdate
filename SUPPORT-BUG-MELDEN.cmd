@echo off
setlocal
set "SUPPORT_URL=https://mail-hub-production-f6d2.up.railway.app/support/updater"
echo Opening GBB SoftwareUpdate Support / Bugtracker...
start "" "%SUPPORT_URL%"
endlocal
