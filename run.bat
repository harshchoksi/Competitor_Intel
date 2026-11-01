@echo off
cd /d "%~dp0"
if not exist .venv\Scripts\activate.bat (echo Run setup.bat first. & exit /b 1)
call .venv\Scripts\activate.bat
python -m src.web %*
