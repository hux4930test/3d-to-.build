@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
set PYTHONDONTWRITEBYTECODE=1
set "TARGET_PYTHON_VERSION=3.13.11"
set "TARGET_PILLOW_VERSION=12.2.0"
set "TARGET_NUMPY_VERSION=1.26.4"
set "TARGET_TRIMESH_VERSION=4.12.2"
set "TARGET_PYRENDER_VERSION=0.1.45"
if not defined BUILD_TOOL_HOST set "BUILD_TOOL_HOST=127.0.0.1"
if not defined BUILD_TOOL_PORT set "BUILD_TOOL_PORT=8765"
title 3D Build Converter

call :find_python
if not defined PYRUN (
  call :install_python
  call :find_python
)

if not defined PYRUN (
  echo.
  echo Exact Python %TARGET_PYTHON_VERSION% could not be found or installed automatically.
  echo.
  echo Install this exact Python version, then run this file again:
  echo https://www.python.org/downloads/release/python-31311/
  echo.
  start "" "https://www.python.org/downloads/release/python-31311/"
  pause
  exit /b 1
)

if /I "%~1"=="--cli-only" goto python_tool

echo.
echo 3D Build Converter
echo.
echo Locked dependency versions:
echo Python %TARGET_PYTHON_VERSION%
echo Pillow %TARGET_PILLOW_VERSION%
echo NumPy %TARGET_NUMPY_VERSION%
echo Trimesh %TARGET_TRIMESH_VERSION%
echo Pyrender %TARGET_PYRENDER_VERSION%
echo.
echo Python command:
echo %PYRUN%
echo.
echo Server host: %BUILD_TOOL_HOST%
echo Server port: %BUILD_TOOL_PORT%
echo API docs: http://%BUILD_TOOL_HOST%:%BUILD_TOOL_PORT%/api/docs
echo OpenAPI JSON: http://%BUILD_TOOL_HOST%:%BUILD_TOOL_PORT%/api/openapi.json
echo.
echo If Pillow is missing or the wrong version, the tool will force-install Pillow %TARGET_PILLOW_VERSION%.
echo Build preview installs NumPy/Trimesh/Pyrender on first render if needed.
echo.
echo 1. Website mode
echo 2. Python tool mode
echo.
set /p "MODE=Pick 1 or 2 [1]: "
if "%MODE%"=="2" goto python_tool

:website
echo.
echo Opening website mode and Python tool mode...
call :stop_old_server
start "Python Tool" "%~f0" --cli-only
start "" http://%BUILD_TOOL_HOST%:%BUILD_TOOL_PORT%/
%PYRUN% server.py --host %BUILD_TOOL_HOST% --port %BUILD_TOOL_PORT%
pause
exit /b

:python_tool
echo.
echo Opening Python tool mode...
%PYRUN% server.py --cli
pause
exit /b

:find_python
set "PYRUN="
set "FOUND_PYTHON_VERSION="
call :check_candidate py -3.13 -B
if defined PYRUN goto :eof
call :check_candidate python -B
if defined PYRUN goto :eof
for %%P in (
  "%LocalAppData%\Programs\Python\Python313\python.exe"
  "%ProgramFiles%\Python313\python.exe"
) do (
  if exist "%%~fP" (
    call :check_candidate "%%~fP" -B
    if defined PYRUN goto :eof
  )
)
goto :eof

:stop_old_server
echo Stopping old website server on port %BUILD_TOOL_PORT% if one is already running...
for /f "tokens=5" %%P in ('netstat -ano ^| findstr /R /C:":%BUILD_TOOL_PORT% .*LISTENING" 2^>nul') do (
  if not "%%P"=="0" taskkill /PID %%P /F >nul 2>nul
)
goto :eof

:check_candidate
set "CANDIDATE=%*"
set "FOUND_PYTHON_VERSION="
for /f "delims=" %%V in ('!CANDIDATE! -c "import sys; print('.'.join(map(str, sys.version_info[:3])))" 2^>nul') do set "FOUND_PYTHON_VERSION=%%V"
if "!FOUND_PYTHON_VERSION!"=="%TARGET_PYTHON_VERSION%" (
  set "PYRUN=!CANDIDATE!"
  goto :eof
)
if defined FOUND_PYTHON_VERSION (
  echo Found Python !FOUND_PYTHON_VERSION!, but this tool is pinned to Python %TARGET_PYTHON_VERSION%.
)
goto :eof

:install_python
echo.
echo Exact Python %TARGET_PYTHON_VERSION% was not found.
echo Trying to install the exact pinned Python version with winget...
echo.
where winget >nul 2>nul
if not %errorlevel%==0 (
  echo winget is not available on this PC.
  goto :eof
)
winget install -e --id Python.Python.3.13 --version %TARGET_PYTHON_VERSION% --scope user --accept-package-agreements --accept-source-agreements
if not %errorlevel%==0 (
  echo.
  echo winget could not install exact Python %TARGET_PYTHON_VERSION%.
  echo The manual download page will open if no exact Python is found.
)
goto :eof
