@echo off
set "FLEET_PY=python.exe"
if exist ".venv\Scripts\python.exe" set "FLEET_PY=%CD%\.venv\Scripts\python.exe"
exit /b 0
