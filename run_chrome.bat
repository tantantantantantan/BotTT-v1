@echo off
setlocal
title Ticket Assistant - Chrome

set "CHROME=C:\Program Files\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" set "CHROME=C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"

if not exist "%CHROME%" (
    echo ERROR: Google Chrome not found.
    pause
    exit /b 1
)

set "PROFILE=%~dp0ChromeProfile"
if not exist "%PROFILE%" mkdir "%PROFILE%"

start "" "%CHROME%" ^
  --remote-debugging-port=9222 ^
  --user-data-dir="%PROFILE%" ^
  https://www.thaiticketmajor.com/

timeout /t 2 >nul
endlocal
