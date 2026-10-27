@echo off
chcp 65001 >nul
cd /d "%~dp0.."
call "scripts\_python.cmd"
call "scripts\_machine_config.cmd"
if errorlevel 1 exit /b 1
"%FLEET_PY%" -m lionfleet.garden --machine-config "config.machine.local.json" --config "config.garden.local.json" --observe --show
pause
