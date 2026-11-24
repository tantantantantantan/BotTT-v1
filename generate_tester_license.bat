@echo off
setlocal
cd /d "%~dp0"
echo ========================================
echo Generate TESTER License - v3.1.1
echo Expire: 2026-10-20 
echo ========================================
if not exist "owner_tools\private_key.pem" (
  echo ERROR: owner_tools\private_key.pem not found
  pause
  exit /b 1
)
py -m pip install cryptography
pushd owner_tools
py generate_license.py --customer "TESTER" --machine "*" --expire 2026-10-20 --output "..\license.json"
popd
if errorlevel 1 (
  echo LICENSE FAILED
  pause
  exit /b 1
)
echo.
echo LICENSE OK: license.json
pause


@REM  py owner_tools\generate_license.py --customer "TESTER" --machine "*" --expire 2026-09-30