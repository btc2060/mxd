@echo off
chcp 65001 >nul
cd /d "%~dp0.."
call "scripts\_python.cmd"
if not exist "config.receiver.local.json" copy "config.receiver.example.json" "config.receiver.local.json" >nul
"%FLEET_PY%" -m lionfleet.receiver --config "config.receiver.local.json" %*
pause
