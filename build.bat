@echo off
REM Build script for TxtOnScrn

set PYTHON_EXE=python
if exist "%~dp0.venv\Scripts\python.exe" set PYTHON_EXE="%~dp0.venv\Scripts\python.exe"

echo Building TxtOnScrn...
echo.

REM Check if pyinstaller is installed
%PYTHON_EXE% -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    echo PyInstaller nenalezen. Instaluji...
    %PYTHON_EXE% -m pip install pyinstaller
)

REM Build the executable
%PYTHON_EXE% -m PyInstaller --noconfirm --clean TxtOnScrn.spec

echo.
echo Build completed!
echo Executable is in: dist\TxtOnScrn.exe
