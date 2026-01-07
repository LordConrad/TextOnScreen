@echo off
REM Build script for TxtOnScrn

echo Building TxtOnScrn...
echo.

REM Check if pyinstaller is installed
python -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    echo PyInstaller nenalezen. Instaluji...
    pip install pyinstaller
)

REM Build the executable
python -m PyInstaller ^
    --name=TxtOnScrn ^
    --onefile ^
    --windowed ^
    --icon=ico.ico ^
    --add-data "ico.ico;." ^
    --add-data "Tesseract-OCR;Tesseract-OCR" ^
    main.py

echo.
echo Build completed!
echo Executable is in: dist\TxtOnScrn.exe
