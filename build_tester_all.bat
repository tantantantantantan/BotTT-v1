@echo off
setlocal
cd /d "%~dp0"
call generate_tester_license_2026-09-30.bat
if errorlevel 1 exit /b 1
call build_exe.bat
