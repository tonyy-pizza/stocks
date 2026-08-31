@echo off
rem  stock_view - launch the dashboard.
rem
rem  Double-click this file, or use the "Stock View" desktop shortcut that
rem  setup_shortcuts.ps1 creates. The shortcut is the more reliable of the two:
rem  it calls this script through cmd.exe explicitly, so it still works when
rem  Windows has lost or hijacked the .bat file association.
rem
rem  This does NOT run the scan pipeline. stock_view only reads the JSON the
rem  pipeline already wrote. To refresh that data, double-click run_scan.bat in
rem  the folder above (or use the "Run Stock Scan" shortcut), then press
rem  Reload in the sidebar.
rem
rem  ASCII only, on purpose: cmd.exe mangles non-ASCII under most code pages.

setlocal EnableExtensions
cd /d "%~dp0"
title stock_view

rem ------------------------------------------------------------------ venv --
rem  A virtual environment beside the app wins, then one a level up (the
rem  pipeline's own). Activating one puts its interpreter on PATH as "python".
if exist ".venv\Scripts\activate.bat"    goto :venv_here
if exist "venv\Scripts\activate.bat"     goto :venv_here_plain
if exist "..\.venv\Scripts\activate.bat" goto :venv_parent
goto :find_python

:venv_here
call ".venv\Scripts\activate.bat"
goto :find_python

:venv_here_plain
call "venv\Scripts\activate.bat"
goto :find_python

:venv_parent
call "..\.venv\Scripts\activate.bat"
goto :find_python

rem ---------------------------------------------------------- interpreter --
rem  Inside an activated venv, "python" is the one we want. Outside one, the
rem  py launcher is the reliable choice on Windows: it lives in C:\Windows and
rem  is on PATH even when the Python install itself is not.
:find_python
set "PY="
if defined VIRTUAL_ENV goto :try_python

py -3 -c "import sys" >nul 2>&1
if not errorlevel 1 set "PY=py -3"
if defined PY goto :check_streamlit

:try_python
python -c "import sys" >nul 2>&1
if not errorlevel 1 set "PY=python"
if defined PY goto :check_streamlit

py -c "import sys" >nul 2>&1
if not errorlevel 1 set "PY=py"
if defined PY goto :check_streamlit

echo.
echo   Could not find a working Python.
echo.
echo   Install it from https://www.python.org/downloads/ and tick
echo   "Add python.exe to PATH" in the installer, then run this again.
echo.
goto :stop

rem ------------------------------------------------------------- streamlit --
rem  Never call the bare "streamlit" command. It lives in the Scripts folder,
rem  which is frequently not on PATH - that is the "streamlit is not
rem  recognized" error. "%PY% -m streamlit" runs the same program through the
rem  interpreter, which always works.
:check_streamlit
%PY% -c "import streamlit" >nul 2>&1
if not errorlevel 1 goto :run

echo.
echo   Streamlit is not installed for this Python (%PY%).
echo.
set "REPLY="
set /p "REPLY=  Install it and the rest of the requirements now? [Y/n] "
if /i "%REPLY%"=="n" goto :stop

echo.
echo   Installing - this takes a minute the first time...
echo.
%PY% -m pip install -r requirements.txt
if errorlevel 1 goto :install_failed

%PY% -c "import streamlit" >nul 2>&1
if errorlevel 1 goto :install_failed
goto :run

:install_failed
echo.
echo   The install did not finish. Try it by hand to see the full error:
echo.
echo     %PY% -m pip install -r requirements.txt
echo.
goto :stop

rem ------------------------------------------------------------------- run --
:run
echo.
echo   Starting stock_view. Your browser should open on http://localhost:8501
echo   Leave this window open while you use it - closing it stops the server.
echo.
%PY% -m streamlit run stock_view.py
if errorlevel 1 goto :stop
endlocal
exit /b 0

rem  Always hold the window open on a failure. Without this the window closes
rem  the instant something goes wrong and takes the error message with it,
rem  which looks exactly like "double-clicking does nothing".
:stop
echo.
pause
endlocal
exit /b 1
