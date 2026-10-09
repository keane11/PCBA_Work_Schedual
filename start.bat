@echo off
chcp 65001 >nul
cd /d "%~dp0"
call "%~dp0service.bat" install
if errorlevel 1 (
    echo.
    echo PCBA workbench failed to start. Check logs and the message above.
    pause
)
