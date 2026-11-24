@echo off
setlocal
cd /d "%~dp0"
echo ========================================
echo Ticket Assistant v3.1.1 TESTER Builder
echo ========================================
if not exist ".venv\Scripts\python.exe" (
  py -m venv .venv
)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements_build.txt
python -m PyInstaller --noconfirm --clean TicketAssistant_v3_1_1.spec
if errorlevel 1 goto :error
copy /Y license.json dist\license.json >nul
copy /Y run_chrome.bat dist\run_chrome.bat >nul
echo.
echo BUILD OK
echo Output: dist\TicketAssistant_v3_1_1_TESTER.exe
echo Tester folder: dist\
pause
exit /b 0
:error
echo.
echo BUILD FAILED
pause
exit /b 1
