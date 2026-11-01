@echo off
REM One-time setup for Windows: creates .venv and installs everything.
REM Usage:  setup.bat            (core)
REM         setup.bat semantic   (also optional embedding model support, ~100 MB)
REM         setup.bat browser    (also headless-browser fallback)
cd /d "%~dp0"
where py >nul 2>nul && (set PYCMD=py -3) || (set PYCMD=python)
%PYCMD% -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" || (
  echo Python 3.11 or newer is required. Install it from https://www.python.org/downloads/ ^(tick "Add Python to PATH"^).
  exit /b 1
)
echo ==^> Creating virtual environment in .venv
%PYCMD% -m venv .venv || exit /b 1
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
echo ==^> Installing requirements
pip install -r requirements.txt || exit /b 1
if /i "%1"=="semantic" pip install -r requirements-semantic.txt
if /i "%1"=="browser" (pip install -r requirements-browser.txt && python -m playwright install chromium)
if not exist .env copy .env.example .env >nul
echo.
echo Setup complete. Start the app with:   run.bat          ^(real sites^)
echo                                       run.bat --demo   ^(6 fake grocery stores^)
echo Then open http://localhost:8000
