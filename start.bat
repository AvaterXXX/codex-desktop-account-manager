@echo off
REM Hide console: launch via VBS -> pythonw
cd /d "%~dp0"
wscript //nologo "%~dp0start.vbs"
exit /b 0
