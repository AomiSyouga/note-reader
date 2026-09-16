@echo off
cd /d "%~dp0"
python app.py
if errorlevel 1 (
    echo.
    echo Error occurred. Press any key to close.
    pause >nul
)
