@echo off
if exist "config.machine.local.json" exit /b 0
copy "config.machine.example.json" "config.machine.local.json" >nul
echo 已创建 config.machine.local.json。请填写本机编号和接收电脑的局域网 IP，保存后关闭记事本，再次运行。
notepad.exe "config.machine.local.json"
pause
exit /b 1
