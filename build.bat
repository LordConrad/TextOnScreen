@echo off
setlocal EnableExtensions

REM Build script for TxtOnScrn
REM Ensure we run from this script's directory (important for .spec paths).
cd /d "%~dp0" || (
    echo Failed to switch to script directory: %~dp0
    exit /b 1
)

REM Improve Czech/UTF-8 output in some consoles
chcp 65001 >nul

set PYTHON_EXE=python
if exist "%~dp0.venv\Scripts\python.exe" set PYTHON_EXE="%~dp0.venv\Scripts\python.exe"

echo Building TxtOnScrn...
echo.

REM Remove previous outputs to avoid running the wrong build type
if exist "%~dp0dist\TxtOnScrn.exe" del /f /q "%~dp0dist\TxtOnScrn.exe" >nul 2>&1
if exist "%~dp0dist\TxtOnScrn" rmdir /s /q "%~dp0dist\TxtOnScrn" >nul 2>&1

REM Check if pyinstaller is installed
%PYTHON_EXE% -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    echo PyInstaller nenalezen. Instaluji...
    %PYTHON_EXE% -m pip install pyinstaller
)

REM Build the executable
%PYTHON_EXE% -m PyInstaller --noconfirm --clean TxtOnScrn.spec

REM Sanity check for onedir output
if not exist "%~dp0dist\TxtOnScrn\TxtOnScrn.exe" (
    echo.
    echo ERROR: Build output not found: dist\TxtOnScrn\TxtOnScrn.exe
    echo Check PyInstaller output above.
    exit /b 1
)
if not exist "%~dp0dist\TxtOnScrn\_internal\python313.dll" (
    echo.
    echo WARNING: python313.dll not found in dist\TxtOnScrn\_internal
    echo If you see "python313.dll was not found" at runtime,
    echo make sure you run/copy the ENTIRE dist\TxtOnScrn folder (exe + _internal).
)

echo.
echo Build completed!
echo Executable is in: dist\TxtOnScrn\TxtOnScrn.exe
echo Note: This is an "onedir" build. Do NOT move/copy only the EXE.
echo       Copy the entire dist\TxtOnScrn folder (it contains _internal).

endlocal
 