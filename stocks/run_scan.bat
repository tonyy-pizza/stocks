@echo off
rem  run_scan - run the scan pipeline and print the Tier 1 report.
rem
rem  Double-click this file, or use the "Run Stock Scan" desktop shortcut that
rem  setup_shortcuts.ps1 creates.
rem
rem  This is the one that touches the network: universe_screen -> the evaluator
rem  -> sentiment -> clustering -> sizing. A stage whose output is still fresh
rem  is skipped, so running it twice in a day mostly re-renders what is already
rem  on disk. It can take a long while on the first run of a day.
rem
rem  Any arguments you pass are handed to scan_report.py, so a shortcut can
rem  carry --force, --include-canada, --top 40 and so on.
rem
rem  ASCII only, on purpose: cmd.exe mangles non-ASCII under most code pages.

setlocal EnableExtensions
cd /d "%~dp0"
title Stock scan
set "CODE=1"

if exist ".venv\Scripts\activate.bat" goto :venv
if exist "venv\Scripts\activate.bat"  goto :venv_plain
goto :find_python

:venv
call ".venv\Scripts\activate.bat"
goto :find_python

:venv_plain
call "venv\Scripts\activate.bat"
goto :find_python

:find_python
set "PY="
if defined VIRTUAL_ENV goto :try_python

py -3 -c "import sys" >nul 2>&1
if not errorlevel 1 set "PY=py -3"
if defined PY goto :check_deps

:try_python
python -c "import sys" >nul 2>&1
if not errorlevel 1 set "PY=python"
if defined PY goto :check_deps

py -c "import sys" >nul 2>&1
if not errorlevel 1 set "PY=py"
if defined PY goto :check_deps

echo.
echo   Could not find a working Python.
echo.
echo   Install it from https://www.python.org/downloads/ and tick
echo   "Add python.exe to PATH" in the installer, then run this again.
echo.
goto :stop

:check_deps
%PY% -c "import yfinance, pandas, numpy" >nul 2>&1
if not errorlevel 1 goto :run

echo.
echo   The pipeline's dependencies are not installed for this Python (%PY%).
echo.
set "REPLY="
set /p "REPLY=  Install them now? [Y/n] "
if /i "%REPLY%"=="n" goto :stop

echo.
%PY% -m pip install yfinance curl_cffi requests numpy pandas
if errorlevel 1 goto :stop

:run
echo.
echo   Running the scan. The first run of a day makes a lot of requests and
echo   can take a while - the sentiment stage is the slow one.
echo.
%PY% scan_report.py %*
set "CODE=%ERRORLEVEL%"

echo.
if not "%CODE%"=="0" echo   The run finished with errors (exit code %CODE%). See above.
echo   Press a key to close. The dashboard reads what this just wrote -
echo   open Stock View and press Reload in the sidebar.

rem  The pause is unconditional: the report itself is the output and it is
rem  printed right here, so closing the window automatically would throw it
rem  away unread.
:stop
echo.
pause
endlocal
exit /b %CODE%
