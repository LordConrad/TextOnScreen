@echo off
setlocal EnableExtensions

set "DEBUG_MODE=0"

set "DO_RELEASE=0"

REM Parse args in any order: onedir|onefile, debug, release
set "BUILD_TYPE=onefile"
for %%A in (%*) do (
    if /I "%%~A"=="debug" (
        set "DEBUG_MODE=1"
    ) else if /I "%%~A"=="release" (
        set "DO_RELEASE=1"
    ) else if /I "%%~A"=="onefile" (
        set "BUILD_TYPE=onefile"
    ) else if /I "%%~A"=="onedir" (
        set "BUILD_TYPE=onedir"
    )
)

if "%DEBUG_MODE%"=="1" (
    echo on
    echo [DEBUG] echo ON
)

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

set SPEC_FILE=TxtOnScrn.spec
if /I "%BUILD_TYPE%"=="onefile" set SPEC_FILE=TxtOnScrn_onefile.spec

echo Building TxtOnScrn (%BUILD_TYPE%)...
echo(

REM If the app is running, dist files may be locked (WinError 5). Try to stop it.
taskkill /f /im TxtOnScrn.exe >nul 2>&1

REM Remove previous outputs to avoid running the wrong build type
if exist "%~dp0dist\TxtOnScrn.exe" del /f /q "%~dp0dist\TxtOnScrn.exe" >nul 2>&1

call :delete_dir "%~dp0dist\TxtOnScrn" 5
if errorlevel 1 call :print_delete_error
if errorlevel 1 exit /b 1

REM Check if pyinstaller is installed
%PYTHON_EXE% -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    echo PyInstaller nenalezen. Instaluji...
    %PYTHON_EXE% -m pip install --upgrade pyinstaller
)

REM Build the executable
set "DISABLE_MODEL_SOURCE_CHECK=True"
%PYTHON_EXE% -m PyInstaller --noconfirm --clean %SPEC_FILE%
if errorlevel 1 (
    echo(
    echo ERROR: PyInstaller build failed.
    exit /b 1
)

if /I "%BUILD_TYPE%"=="onedir" (
    REM Sanity check for onedir output
    if not exist "%~dp0dist\TxtOnScrn\TxtOnScrn.exe" (
        echo(
        echo ERROR: Build output not found: dist\TxtOnScrn\TxtOnScrn.exe
        echo Check PyInstaller output above.
        exit /b 1
    )
    if not exist "%~dp0dist\TxtOnScrn\_internal\python*.dll" (
        echo(
        echo WARNING: python*.dll not found in dist\TxtOnScrn\_internal
        echo If you see "pythonXXX.dll was not found" at runtime,
        echo make sure you run/copy the ENTIRE dist\TxtOnScrn folder (exe + _internal).
    )
) else (
    REM Sanity check for onefile output
    if not exist "%~dp0dist\TxtOnScrn.exe" (
        echo(
        echo ERROR: Build output not found: dist\TxtOnScrn.exe
        echo Check PyInstaller output above.
        exit /b 1
    )
)

if "%DO_RELEASE%"=="1" (
    echo(
    echo Creating release package...
    if not exist "%~dp0release" mkdir "%~dp0release" >nul 2>&1

    if /I "%BUILD_TYPE%"=="onedir" (
        call :delete_dir "%~dp0release\TxtOnScrn" 5
        xcopy "%~dp0dist\TxtOnScrn" "%~dp0release\TxtOnScrn" /E /I /Y >nul
        powershell -NoProfile -Command "Compress-Archive -Path '%~dp0release\\TxtOnScrn\\*' -DestinationPath '%~dp0release\\TxtOnScrn_onedir_release.zip' -Force" >nul 2>&1
        echo Release folder: release\TxtOnScrn
        echo Release zip:    release\TxtOnScrn_onedir_release.zip
    ) else (
        copy /Y "%~dp0dist\TxtOnScrn.exe" "%~dp0release\TxtOnScrn.exe" >nul
        powershell -NoProfile -Command "Compress-Archive -Path '%~dp0release\\TxtOnScrn.exe' -DestinationPath '%~dp0release\\TxtOnScrn_onefile_release.zip' -Force" >nul 2>&1
        echo Release exe:    release\TxtOnScrn.exe
        echo Release zip:    release\TxtOnScrn_onefile_release.zip
    )
)

echo(
echo Build completed!
if /I "%BUILD_TYPE%"=="onedir" (
    echo Executable is in: dist\TxtOnScrn\TxtOnScrn.exe
    echo Note: This is an "onedir" build. Do NOT move/copy only the EXE.
    echo       Copy the entire dist\TxtOnScrn folder (it contains _internal).
) else (
    echo Executable is in: dist\TxtOnScrn.exe
    echo Note: This is a "onefile" build (single EXE). First start may be slower.
)

echo(
echo Usage:
echo   build.bat              ^(default: onefile^)
echo   build.bat onedir       ^(folder build: exe + _internal^)
echo   build.bat onefile      ^(single .exe for copying to another PC^)
echo   build.bat release      ^(build default onefile and create release\ + zip^)
echo   build.bat onedir release
echo   build.bat onefile release
echo   build.bat onefile debug

endlocal

goto :eof

:print_delete_error
echo(
echo ERROR: Nelze smazat dist\TxtOnScrn (zamčené soubory / WinError 5).
echo - Zavři běžící TxtOnScrn.exe (Task Manager)
echo - Zavři okna Exploreru otevřená na dist\TxtOnScrn
echo - Případně vypni dočasně antivirus nebo restartuj PC
exit /b 1

:delete_dir
REM Args: %1 = dir, %2 = retries
set "TARGET=%~1"
set "RETRIES=%~2"
if "%RETRIES%"=="" set "RETRIES=5"
if not exist "%TARGET%" exit /b 0
if "%DEBUG_MODE%"=="1" echo [DEBUG] delete_dir TARGET=[%TARGET%]
if "%DEBUG_MODE%"=="1" echo [DEBUG] delete_dir RETRIES=[%RETRIES%]

set /a TRY=1
:delete_dir_retry
rmdir /s /q "%TARGET%" >nul 2>&1
if not exist "%TARGET%" exit /b 0
if %TRY% GEQ %RETRIES% exit /b 1
set /a TRY+=1
timeout /t 1 /nobreak >nul
goto :delete_dir_retry
