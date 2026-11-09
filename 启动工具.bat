@echo off
setlocal
title Compress Archive Tool

rem ---- working dir: map UNC share to a drive letter if needed ----
pushd "%~dp0" >nul 2>nul
set "APP_DIR=%~dp0"

rem ---- locate Python (auto-detect, no hardcoded paths) ----
set "PY="

where python >nul 2>nul
if not errorlevel 1 set "PY=python"

if not defined PY (
    py -3 -c "import sys" >nul 2>nul
    if not errorlevel 1 set "PY=py -3"
)

rem ---- common install locations (fallbacks) ----
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python310\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if not defined PY if exist "C:\Python310\python.exe" set "PY=C:\Python310\python.exe"
if not defined PY if exist "C:\Python311\python.exe" set "PY=C:\Python311\python.exe"

if not defined PY (
    echo [ERROR] Python not found. Please install Python 3.10+ first.
    echo Download: https://www.python.org/downloads/
    pause
    exit /b 1
)

rem ---- ensure pywebview is installed (auto-install on demand) ----
"%PY%" -c "import webview" >nul 2>nul
if errorlevel 1 (
    echo [SETUP] pywebview not found, installing dependencies ...
    "%PY%" -m pip install --disable-pip-version-check pywebview
    if errorlevel 1 (
        echo.
        echo [WARN] pywebview install failed. Falling back to browser mode.
        echo        If you want the desktop window, install it manually with:
        echo        %PY% -m pip install pywebview
        echo.
    )
)

echo ============================================
echo   Compress Archive Tool
echo   Starting, a browser window will open ...
echo   Close this window to exit the program.
echo ============================================
echo.

if "%PY:~0,5%"=="C:\Py" (
    "%PY%" "%APP_DIR%app.py" %*
) else (
    %PY% "%APP_DIR%app.py" %*
)

if errorlevel 1 (
    echo.
    echo [ERROR] Failed to start. Check Python and try again.
    echo Press any key to close ...
    pause >nul
)
endlocal
