@echo off
REM HBM AI Visibility Audits - start on Windows. Double-click this file.
REM First run: creates the Python environment, installs packages and asks for your API keys (typed hidden).
cd /d "%~dp0"
set PYTHONUTF8=1
if "%ADMIN_EMAILS%"=="" set ADMIN_EMAILS=lav@homebuildermarketers.com

where py >nul 2>nul && (set "PY=py -3") || (set "PY=python")
%PY% --version >nul 2>nul || (
  echo Python 3 is not installed. Get it from https://www.python.org/downloads/ and tick "Add python.exe to PATH".
  pause & exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo Setting up for the first time. This takes about a minute...
  %PY% -m venv .venv || (pause & exit /b 1)
  ".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
  ".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt || (pause & exit /b 1)
)

if not exist "%USERPROFILE%\.config\hbm-aeo\keys.env" (
  echo.
  echo Enter your API keys. Typing is hidden. Press Enter to skip any you do not have.
  ".venv\Scripts\python.exe" setup_keys.py
)

echo.
echo HBM AI Visibility Audits is starting at http://127.0.0.1:8080
echo Sign in: type your email and click "Email me a sign-in code". The code appears in THIS window.
echo Keep this window open while you use the app. Close it to stop.
start "" http://127.0.0.1:8080
".venv\Scripts\python.exe" serve.py
pause
