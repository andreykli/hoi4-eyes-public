@echo off
setlocal
pushd "%~dp0"
if errorlevel 1 (
  echo Cannot open HOI4-EYES directory: "%~dp0"
  pause
  exit /b 1
)
if not defined EYES_PYTHON if exist "%~dp0.venv\Scripts\python.exe" set "EYES_PYTHON=%~dp0.venv\Scripts\python.exe"
if not defined EYES_PYTHON if exist "%~dp0..\venv\Scripts\python.exe" set "EYES_PYTHON=%~dp0..\venv\Scripts\python.exe"
if not defined EYES_PYTHON if exist "%~dp0..\.venv\Scripts\python.exe" set "EYES_PYTHON=%~dp0..\.venv\Scripts\python.exe"
if not defined EYES_PYTHON set "EYES_PYTHON=python.exe"
"%EYES_PYTHON%" -c "import sys; assert sys.version_info >= (3, 10); import flask, requests, dotenv, tkinter" >nul 2>&1
if errorlevel 1 (
  echo Unable to start Python 3.10+ with dependencies. Run setup_windows.bat or set EYES_PYTHON.
  popd
  exit /b 1
)
if not exist "tools\experimental_hub.py" (
  echo HOI4-EYES launcher not found in "%CD%"
  popd
  pause
  exit /b 1
)
if not defined EYES_EXPRESSION_MODE set "EYES_EXPRESSION_MODE=auto"
if not defined EYES_EXPRESSION_TIMEOUT set "EYES_EXPRESSION_TIMEOUT=12"
if not defined EYES_OLLAMA_THREADS set "EYES_OLLAMA_THREADS=2"
if /i "%~1"=="--check" goto check
if /i "%~1"=="--headless" goto headless
if /i "%~1"=="--check-game" goto check_game
set "EYES_PYTHONW="
set "EYES_HUB_MODE="
set "EYES_ENTRY=windows_desktop.py"
if /i "%~1"=="--manual" (
  set "EYES_HUB_MODE=--manual"
  set "EYES_ENTRY=experimental_hub.py"
) else (
  "%EYES_PYTHON%" -c "import webview, clr" >nul 2>&1
  if errorlevel 1 (
    echo WebView2 integration is missing. Run setup_windows.bat or start.bat --manual.
    popd
    pause
    exit /b 1
  )
)
for %%I in ("%EYES_PYTHON%") do if exist "%%~dpIpythonw.exe" set "EYES_PYTHONW=%%~dpIpythonw.exe"
if exist "%EYES_PYTHONW%" (
  start "" "%EYES_PYTHONW%" "%CD%\tools\%EYES_ENTRY%" --root "%CD%" %EYES_HUB_MODE%
) else (
  start "HOI4-EYES" "%EYES_PYTHON%" "%CD%\tools\%EYES_ENTRY%" --root "%CD%" %EYES_HUB_MODE%
)
popd
exit /b 0
:headless
"%EYES_PYTHON%" "tools\experimental_stack.py" --root "%CD%"
set "EYES_LAUNCH_RESULT=%ERRORLEVEL%"
popd
pause
exit /b %EYES_LAUNCH_RESULT%

:check
"%EYES_PYTHON%" "tools\release_preflight.py"
set "EYES_LAUNCH_RESULT=%ERRORLEVEL%"
popd
exit /b %EYES_LAUNCH_RESULT%

:check_game
"%EYES_PYTHON%" "tools\release_acceptance.py" scan-game
set "EYES_LAUNCH_RESULT=%ERRORLEVEL%"
popd
pause
exit /b %EYES_LAUNCH_RESULT%
