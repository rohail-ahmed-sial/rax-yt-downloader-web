@echo off
setlocal EnableExtensions
title RAX YT Downloader
cd /d "%~dp0"
chcp 65001 >nul 2>&1
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"

if not exist "%~dp0server.py" goto :not_extracted

echo.
echo   RAX YT Downloader
echo   -----------------

rem ---- find Python 3.10 or newer
set "PY="
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if not errorlevel 1 set "PY=py -3"
if defined PY goto :have_python
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if not errorlevel 1 set "PY=python"
if defined PY goto :have_python
goto :no_python

:have_python
rem ---- a private Python setup inside this folder, so nothing clashes with your other projects
set "VPY=%~dp0.venv\Scripts\python.exe"
if exist "%VPY%" goto :have_venv
echo   First start: setting things up. This takes a few minutes, only once.
%PY% -m venv "%~dp0.venv"
if errorlevel 1 goto :venv_failed

:have_venv
set "PATH=%~dp0.venv\Scripts;%PATH%"
set "PIPQ=-q"
if not exist "%~dp0.venv\.rax-ready" set "PIPQ="

rem ---- update every start: YouTube changes often and new yt-dlp versions keep up
echo   Updating the download engine...
"%VPY%" -m pip install --disable-pip-version-check -q -U pip >nul 2>&1
"%VPY%" -m pip install --disable-pip-version-check %PIPQ% -U "yt-dlp[default]" "flask>=3.0"
if errorlevel 1 goto :engine_failed
"%VPY%" -m pip install --disable-pip-version-check %PIPQ% -U imageio-ffmpeg deno
if errorlevel 1 echo   Note: the FFmpeg or Deno helper could not be installed. Some videos may not work.
echo ok> "%~dp0.venv\.rax-ready"
goto :run

:engine_failed
echo.
echo   Could not update the download engine. Check your internet connection.
"%VPY%" -c "import yt_dlp, flask" >nul 2>&1
if errorlevel 1 goto :stop
echo   Starting with the version you already have.

:run
echo.
"%VPY%" "%~dp0server.py"
echo.
echo   The downloader has stopped. You can close this window.
pause
exit /b 0

:not_extracted
echo.
echo   Please unzip the folder first.
echo   Right-click the zip file, choose "Extract All", then open START.bat
echo   from the extracted folder.
echo.
pause
exit /b 1

:no_python
echo.
echo   Python 3.10 or newer is needed and was not found.
echo     1. Download it from https://www.python.org/downloads/
echo     2. While installing, tick "Add python.exe to PATH".
echo     3. Open START.bat again.
echo.
start "" "https://www.python.org/downloads/"
pause
exit /b 1

:venv_failed
echo.
echo   Could not set up Python for the downloader.
echo   Try reinstalling Python from https://www.python.org/downloads/
echo.
pause
exit /b 1

:stop
echo.
pause
exit /b 1
