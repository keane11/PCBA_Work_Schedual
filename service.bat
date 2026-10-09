@echo off
chcp 65001 >nul
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"
set "PCBA_ROOT=%~dp0"
set "PCBA_PY=%~dp0.venv\Scripts\python.exe"
set "PCBA_PID_FILE=%~dp0data\pcba.pid"
set "PCBA_WATCH_PID_FILE=%~dp0data\pcba-watch.pid"
set "PCBA_STOP_FILE=%~dp0logs\stop.flag"
set "PCBA_STARTUP_NAME=PCBAWorkbench.vbs"
set "PCBA_SERVICE_BAT=%~f0"
set "PCBA_BOOT_TASK_SCRIPT=%~dp0scripts\pcba_boot_task.ps1"
if not exist "logs" mkdir "logs"

if /i "%~1"=="run" goto run
if /i "%~1"=="run-bg" goto run_bg
if /i "%~1"=="watch" goto watch
if /i "%~1"=="install" goto install
if /i "%~1"=="install-boot" goto install_boot
if /i "%~1"=="uninstall-boot" goto uninstall_boot
if /i "%~1"=="uninstall" goto uninstall
if /i "%~1"=="stop" goto stop
if /i "%~1"=="status" goto status
if /i "%~1"=="backup" goto backup
if /i "%~1"=="firewall" goto firewall
if not "%~1"=="" goto usage

:menu
echo ========================================
echo   PCBA Workbench - Windows 11
echo ========================================
echo 1. Start now ^(foreground^)
echo 2. Install login auto-start and start now
echo 3. Show status and LAN URL
echo 4. Make a consistent SQLite backup now
echo 5. Stop background server
echo 6. Remove login auto-start
echo 7. Open Windows Firewall TCP 8001 ^(administrator^)
echo 8. Install boot auto-start ^(administrator, optional^)
echo 9. Remove boot auto-start ^(administrator^)
echo 0. Exit
set /p "PCBA_CHOICE=Choose: "
if "%PCBA_CHOICE%"=="1" goto run
if "%PCBA_CHOICE%"=="2" goto install
if "%PCBA_CHOICE%"=="3" goto status
if "%PCBA_CHOICE%"=="4" goto backup
if "%PCBA_CHOICE%"=="5" goto stop
if "%PCBA_CHOICE%"=="6" goto uninstall
if "%PCBA_CHOICE%"=="7" goto firewall
if "%PCBA_CHOICE%"=="8" goto install_boot
if "%PCBA_CHOICE%"=="9" goto uninstall_boot
if "%PCBA_CHOICE%"=="0" exit /b 0
goto menu

:usage
echo Usage: service.bat [run^|run-bg^|install^|install-boot^|uninstall^|uninstall-boot^|stop^|status^|backup^|firewall]
exit /b 1

:prepare
if not exist "data" mkdir "data"
if not exist "logs" mkdir "logs"
if exist "%PCBA_PY%" goto prepare_deps
where py >nul 2>&1
if not errorlevel 1 (
    py -3 -m venv ".venv"
) else (
    where python >nul 2>&1
    if errorlevel 1 (
        echo ERROR: Python 3 was not found. Install Python 3.11 or newer and retry.
        exit /b 1
    )
    python -m venv ".venv"
)
if errorlevel 1 exit /b 1
:prepare_deps
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$stamp='.venv\.deps-sha256'; $hash=(Get-FileHash -LiteralPath 'requirements.txt' -Algorithm SHA256).Hash; if ((Test-Path $stamp) -and ((Get-Content -LiteralPath $stamp -Raw).Trim() -eq $hash)) { exit 0 }; exit 1" >nul 2>&1
if not errorlevel 1 exit /b 0
echo Installing PCBA workbench dependencies...
"%PCBA_PY%" -m pip install -r "requirements.txt"
if errorlevel 1 exit /b 1
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "(Get-FileHash -LiteralPath 'requirements.txt' -Algorithm SHA256).Hash | Set-Content -LiteralPath '.venv\.deps-sha256' -Encoding ASCII"
if errorlevel 1 exit /b 1
exit /b 0

:backup_internal
if not exist "data\pcba.db" exit /b 0
"%PCBA_PY%" -m server.backup
exit /b %errorlevel%

:run
call :prepare
if errorlevel 1 exit /b 1
powershell -NoProfile -ExecutionPolicy Bypass -Command "$f=$env:PCBA_WATCH_PID_FILE; if (-not (Test-Path $f)) { exit 1 }; $watchPid=0; if (-not [int]::TryParse((Get-Content $f),[ref]$watchPid)) { exit 1 }; $p=Get-Process -Id $watchPid -ErrorAction SilentlyContinue; if ($p -and $p.Path -eq $env:ComSpec) { exit 0 }; exit 1" >nul 2>&1
if not errorlevel 1 (
    echo PCBA background watchdog is already running at http://localhost:8001
    exit /b 0
)
powershell -NoProfile -ExecutionPolicy Bypass -Command "$f=$env:PCBA_PID_FILE; if (-not (Test-Path $f)) { exit 1 }; $serverPid=0; if (-not [int]::TryParse((Get-Content $f),[ref]$serverPid)) { exit 1 }; $p=Get-Process -Id $serverPid -ErrorAction SilentlyContinue; if ($p -and $p.Path -eq $env:PCBA_PY) { exit 0 }; exit 1" >nul 2>&1
if not errorlevel 1 (
    echo PCBA workbench is already running at http://localhost:8001
    exit /b 0
)
call :backup_internal
if errorlevel 1 (
    echo ERROR: Database backup failed; server was not started.
    exit /b 1
)
echo.
echo Local: http://localhost:8001
echo LAN:   http://%COMPUTERNAME%:8001
echo Press Ctrl+C to stop.
"%PCBA_PY%" -m uvicorn server.app:app --host 0.0.0.0 --port 8001
exit /b %errorlevel%

:run_bg
call :prepare >> "logs\install.log" 2>&1
if errorlevel 1 exit /b 1
REM A live watchdog owns the background server. A foreground run must be stopped first.
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$f=$env:PCBA_WATCH_PID_FILE; if (Test-Path $f) { $watchPid=0; if ([int]::TryParse((Get-Content $f),[ref]$watchPid)) { $p=Get-Process -Id $watchPid -ErrorAction SilentlyContinue; if ($p -and $p.Path -eq $env:ComSpec) { exit 0 } } }; try { $h=Invoke-RestMethod -Uri 'http://127.0.0.1:8001/api/health' -TimeoutSec 2; if ($h.ok) { exit 2 } } catch {}; exit 1" >nul 2>&1
if "%errorlevel%"=="0" (
    echo PCBA background watchdog is already running.
    exit /b 0
)
if "%errorlevel%"=="2" (
    echo ERROR: Port 8001 is already in use. Stop start.bat before installing background auto-start.
    exit /b 1
)
call :backup_internal >> "logs\backup.log" 2>&1
if errorlevel 1 exit /b 1
if exist "%PCBA_STOP_FILE%" del /f "%PCBA_STOP_FILE%" >nul 2>&1
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$q=[char]34; $bat=$env:PCBA_SERVICE_BAT; $p=Start-Process -FilePath $env:ComSpec -ArgumentList ('/d /c ' + $q + $q + $bat + $q + ' watch' + $q) -WorkingDirectory $env:PCBA_ROOT -WindowStyle Hidden -PassThru -ErrorAction Stop; Set-Content -LiteralPath $env:PCBA_WATCH_PID_FILE -Value $p.Id -Encoding ASCII; for ($i=0; $i -lt 20; $i++) { Start-Sleep -Seconds 1; $p.Refresh(); if ($p.HasExited) { Write-Host 'Watchdog exited during startup. See logs\watch.log and logs\server-error.log.'; exit 1 }; try { $health=Invoke-RestMethod -Uri 'http://127.0.0.1:8001/api/health' -TimeoutSec 2; if ($health.ok) { Write-Host ('Background watchdog started PID ' + $p.Id); exit 0 } } catch {} }; Set-Content -LiteralPath $env:PCBA_STOP_FILE -Value stop -Encoding ASCII; Write-Host 'Server did not become healthy in 20 seconds. See logs\watch.log and logs\server-error.log.'; exit 1"
exit /b %errorlevel%

:watch
call :prepare >> "logs\install.log" 2>&1
if errorlevel 1 exit /b 1
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$flag=$env:PCBA_STOP_FILE; $pidFile=$env:PCBA_PID_FILE; $watchLog=Join-Path $env:PCBA_ROOT 'logs\watch.log'; $out=Join-Path $env:PCBA_ROOT 'logs\server.log'; $err=Join-Path $env:PCBA_ROOT 'logs\server-error.log'; $serverArgs=@('-m','uvicorn','server.app:app','--host','0.0.0.0','--port','8001'); while (-not (Test-Path $flag)) { try { $p=Start-Process -FilePath $env:PCBA_PY -ArgumentList $serverArgs -WorkingDirectory $env:PCBA_ROOT -WindowStyle Hidden -RedirectStandardOutput $out -RedirectStandardError $err -PassThru -ErrorAction Stop; Set-Content -LiteralPath $pidFile -Value $p.Id -Encoding ASCII; Add-Content -LiteralPath $watchLog -Value ((Get-Date -Format s) + ' Started server PID ' + $p.Id); while (-not $p.HasExited) { if (Test-Path $flag) { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue; break }; Start-Sleep -Seconds 1; $p.Refresh() }; $p.WaitForExit(5000) | Out-Null; Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue; Add-Content -LiteralPath $watchLog -Value ((Get-Date -Format s) + ' Server exited with code ' + $p.ExitCode) } catch { Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue; Add-Content -LiteralPath $watchLog -Value ((Get-Date -Format s) + ' Watchdog error: ' + $_.Exception.Message) }; if (-not (Test-Path $flag)) { Start-Sleep -Seconds 5 } }; Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue; Remove-Item -LiteralPath $env:PCBA_WATCH_PID_FILE -Force -ErrorAction SilentlyContinue; Add-Content -LiteralPath $watchLog -Value ((Get-Date -Format s) + ' Watchdog stopped')"
exit /b %errorlevel%

:install
call :prepare
if errorlevel 1 exit /b 1
powershell -NoProfile -ExecutionPolicy Bypass -Command "if (Get-ScheduledTask -TaskName 'PCBA-Workbench-Boot' -TaskPath '\' -ErrorAction SilentlyContinue) { exit 0 }; exit 1" >nul 2>&1
if errorlevel 1 goto install_login
powershell -NoProfile -ExecutionPolicy Bypass -Command "try { $r=Invoke-RestMethod -Uri 'http://127.0.0.1:8001/api/health' -TimeoutSec 3; if ($r.ok) { exit 0 } } catch {}; exit 1" >nul 2>&1
if not errorlevel 1 (
    echo PCBA workbench is already running under boot auto-start at http://localhost:8001
    exit /b 0
)
goto run_bg

:install_login
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$q=[char]34; $startup=[Environment]::GetFolderPath('Startup'); $vbs=Join-Path $startup $env:PCBA_STARTUP_NAME; $bat=$env:PCBA_SERVICE_BAT; $line='CreateObject(' + $q + 'WScript.Shell' + $q + ').Run ' + $q + $q + $q + $bat + $q + $q + ' run-bg' + $q + ', 0, False'; Set-Content -LiteralPath $vbs -Value $line -Encoding Unicode; Write-Host ('Auto-start installed: ' + $vbs)"
if errorlevel 1 exit /b 1
call :run_bg
if errorlevel 1 (
    powershell -NoProfile -ExecutionPolicy Bypass -Command "$vbs=Join-Path ([Environment]::GetFolderPath('Startup')) $env:PCBA_STARTUP_NAME; Remove-Item -LiteralPath $vbs -Force -ErrorAction SilentlyContinue"
    call :stop
    exit /b 1
)
echo LAN URL: http://%COMPUTERNAME%:8001
echo If another PC cannot connect, run service.bat firewall as administrator.
exit /b 0

:install_boot
call :prepare
if errorlevel 1 exit /b 1
powershell -NoProfile -ExecutionPolicy Bypass -File "%PCBA_BOOT_TASK_SCRIPT%" -Mode Install
if errorlevel 1 exit /b 1
powershell -NoProfile -ExecutionPolicy Bypass -Command "$vbs=Join-Path ([Environment]::GetFolderPath('Startup')) $env:PCBA_STARTUP_NAME; if (Test-Path $vbs) { Remove-Item -LiteralPath $vbs -Force; Write-Host 'Removed login auto-start; boot task now owns startup.' }"
if errorlevel 1 exit /b 1
echo The boot task will start the workbench after the next Windows restart, without user login.
echo A SYSTEM task cannot normally write to a network share; check mirror status after reboot.
exit /b 0

:uninstall_boot
powershell -NoProfile -ExecutionPolicy Bypass -File "%PCBA_BOOT_TASK_SCRIPT%" -Mode Remove
if errorlevel 1 exit /b 1
call :stop
exit /b %errorlevel%

:uninstall
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$vbs=Join-Path ([Environment]::GetFolderPath('Startup')) $env:PCBA_STARTUP_NAME; if (Test-Path $vbs) { Remove-Item -LiteralPath $vbs -Force; Write-Host 'Auto-start removed.' } else { Write-Host 'No auto-start entry.' }"
if errorlevel 1 exit /b 1
call :stop
exit /b %errorlevel%

:stop
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "Set-Content -LiteralPath $env:PCBA_STOP_FILE -Value stop -Encoding ASCII; $f=$env:PCBA_PID_FILE; if (Test-Path $f) { $serverPid=0; if ([int]::TryParse((Get-Content $f),[ref]$serverPid)) { $p=Get-Process -Id $serverPid -ErrorAction SilentlyContinue; if ($p -and $p.Path -eq $env:PCBA_PY) { Stop-Process -Id $serverPid -Force; Write-Host ('Stopped server PID ' + $serverPid) } elseif ($p) { Write-Host 'Server PID belongs to another program; it was not stopped.' } }; Remove-Item -LiteralPath $f -Force -ErrorAction SilentlyContinue }; $wf=$env:PCBA_WATCH_PID_FILE; if (Test-Path $wf) { $watchPid=0; if ([int]::TryParse((Get-Content $wf),[ref]$watchPid)) { for ($i=0; $i -lt 10; $i++) { $w=Get-Process -Id $watchPid -ErrorAction SilentlyContinue; if (-not $w) { break }; Start-Sleep -Seconds 1 }; $w=Get-Process -Id $watchPid -ErrorAction SilentlyContinue; if ($w -and $w.Path -eq $env:ComSpec) { Write-Host 'Watchdog is still stopping; check service.bat status.'; exit 1 } }; Remove-Item -LiteralPath $wf -Force -ErrorAction SilentlyContinue }; Write-Host 'Background server and watchdog stopped.'"
exit /b %errorlevel%

:status
powershell -NoProfile -ExecutionPolicy Bypass -Command "$task=Get-ScheduledTask -TaskName 'PCBA-Workbench-Boot' -TaskPath '\' -ErrorAction SilentlyContinue; if ($task) { 'Boot auto-start: ' + $task.State + ' (SYSTEM)' } else { 'Boot auto-start: not installed' }"
powershell -NoProfile -ExecutionPolicy Bypass -Command "try { $r=Invoke-RestMethod -Uri 'http://127.0.0.1:8001/api/health' -TimeoutSec 3; $b=$r.backup; 'Local backup freshness: ' + $b.local_status + $(if ($null -ne $b.local_age_hours) { ' (' + $b.local_age_hours + ' hours old)' } else { '' }); if ($b.last_local_failure_at) { 'Last backup failure: ' + $b.last_local_failure_at }; if ($null -ne $b.disk_free_gib) { 'Disk free: ' + $b.disk_free_gib + ' GiB' + $(if ($b.disk_low) { ' - LOW SPACE' } else { '' }) } else { 'Disk free: unavailable' } } catch {}"
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$vbs=Join-Path ([Environment]::GetFolderPath('Startup')) $env:PCBA_STARTUP_NAME; 'Auto-start: ' + (Test-Path $vbs); $wf=$env:PCBA_WATCH_PID_FILE; if (Test-Path $wf) { $watchPid=0; if ([int]::TryParse((Get-Content $wf),[ref]$watchPid)) { $w=Get-Process -Id $watchPid -ErrorAction SilentlyContinue; if ($w -and $w.Path -eq $env:ComSpec) { 'Watchdog PID: ' + $watchPid } else { 'Watchdog PID: stale' } } else { 'Watchdog PID: invalid' } } else { 'Watchdog PID: none' }; $f=$env:PCBA_PID_FILE; if (Test-Path $f) { $serverPid=0; if ([int]::TryParse((Get-Content $f),[ref]$serverPid)) { $p=Get-Process -Id $serverPid -ErrorAction SilentlyContinue; if ($p -and $p.Path -eq $env:PCBA_PY) { 'Background PID: ' + $serverPid } else { 'Background PID: stale' } } else { 'Background PID: invalid' } }; try { $r=Invoke-RestMethod -Uri 'http://127.0.0.1:8001/api/health' -TimeoutSec 3; if ($r.ok -and $r.database -eq 'ok') { 'Health: OK' } else { 'Health: degraded' }; if ($r.backup.mirror_configured) { if ($r.backup.mirror_ok -eq $true) { 'Mirror backup: OK' } elseif ($r.backup.mirror_ok -eq $false) { 'Mirror backup: failed' } else { 'Mirror backup: pending' } } else { 'Mirror backup: not configured' } } catch { 'Health: unavailable' }; $latest=Get-ChildItem (Join-Path $env:PCBA_ROOT 'data\backups\pcba_*.db') -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 1; if ($latest) { 'Latest backup: ' + $latest.LastWriteTime + ' ' + $latest.Name } else { 'Latest backup: none' }; 'Local: http://localhost:8001'; 'LAN: http://' + $env:COMPUTERNAME + ':8001'"
exit /b %errorlevel%

:backup
call :prepare
if errorlevel 1 exit /b 1
if not exist "data\pcba.db" (
    echo No database exists yet; nothing to back up.
    exit /b 0
)
call :backup_internal
exit /b %errorlevel%

:firewall
net session >nul 2>&1
if errorlevel 1 (
    echo ERROR: Run this command as administrator to change Windows Firewall.
    exit /b 1
)
netsh advfirewall firewall delete rule name="PCBA Workbench 8001" >nul 2>&1
netsh advfirewall firewall add rule name="PCBA Workbench 8001" dir=in action=allow protocol=TCP localport=8001 profile=private
exit /b %errorlevel%
