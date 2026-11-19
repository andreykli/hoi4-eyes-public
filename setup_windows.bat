@echo off
setlocal
pushd "%~dp0" || exit /b 1
if not exist ".venv\Scripts\python.exe" (
  py -3 -m venv ".venv"
  if errorlevel 1 (
    popd
    exit /b 1
  )
)
set "EYES_PYTHON=%CD%\.venv\Scripts\python.exe"
"%EYES_PYTHON%" -m pip install -r "%CD%\requirements.txt"
if errorlevel 1 (
  popd
  exit /b 1
)
call "%CD%\start.bat" --check
set "EYES_SETUP_RESULT=%ERRORLEVEL%"
popd
exit /b %EYES_SETUP_RESULT%
