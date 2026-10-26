@echo off
chcp 65001 >nul
title Qwen2-VL Model Downloader (resumable + progress bar)
cd /d "%~dp0"

set "PYEXE="
if exist "D:\python3.12\python.exe" set "PYEXE=D:\python3.12\python.exe"
if not defined PYEXE if exist "C:\Program Files\Python38\python.exe" set "PYEXE=C:\Program Files\Python38\python.exe"
if not defined PYEXE (
    where python >nul 2>nul && set "PYEXE=python"
)

if not defined PYEXE (
    echo [ERROR] Python was not found. Please install Python 3.8 or newer.
    pause
    exit /b 1
)

echo Using Python: %PYEXE%
echo.
"%PYEXE%" download_models.py
echo.
pause
