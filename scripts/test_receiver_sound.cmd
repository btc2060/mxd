@echo off
chcp 65001 >nul
cd /d "%~dp0.."
call "scripts\_python.cmd"
"%FLEET_PY%" -m lionfleet.receiver --test-sound
pause
