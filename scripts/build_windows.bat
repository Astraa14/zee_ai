@echo off
REM Build the ZEE onedir EXE with PyInstaller, run a quick smoke test, then
REM (if Inno Setup is installed) package it into an installer .exe.
REM
REM Prereqs on the build machine:
REM   pip install -r requirements.txt -r requirements-gui.txt -r requirements-dev.txt
REM   Inno Setup 6  (https://jrsoftware.org/isinfo.php)  -> optional but recommended
REM
REM Optional signing (see docs/packaging_windows.md):
REM   set ZEE_SIGNING=1
REM   set WINDOWS_CERT_BASE64_FILE=path\to\cert.pfx.b64
REM   set CERT_PASSWORD=the-pfx-password
REM   (signtool must be on PATH or set SIGNTOOL=full\path\signtool.exe)
REM
REM Optional extra single-file EXE (in addition to the onedir bundle):
REM   set ZEE_ONEFILE=1
setlocal
cd /d "%~dp0\.."
set BUILD_ROOT=%cd%

REM --- 1. PyInstaller onedir build -------------------------------------
python -m PyInstaller --noconfirm --clean zee.spec
if errorlevel 1 (
    echo [ERROR] PyInstaller build failed.
    exit /b 1
)
echo [OK] Bundle: %BUILD_ROOT%\dist\Zee\

REM --- 2. Optional single-file EXE -------------------------------------
if "%ZEE_ONEFILE%"=="1" (
    python -m PyInstaller --noconfirm --clean --onefile --name Zee ^
        --hidden-import=PySide6.QtWebEngineCore ^
        --hidden-import=PySide6.QtWebEngineWidgets ^
        --collect-all vosk ^
        --add-data "%BUILD_ROOT%\templates;templates" ^
        --collect-submodules flask zee.py
    if errorlevel 1 (
        echo [ERROR] PyInstaller onefile build failed.
        exit /b 1
    )
    echo [OK] Single-file EXE: %BUILD_ROOT%\dist\Zee.exe
    if "%ZEE_SIGNING%"=="1" call :sign "%BUILD_ROOT%\dist\Zee.exe"
)

mkdir "%BUILD_ROOT%\artifacts" 2>nul

REM --- 3. Smoke test the bundle -----------------------------------------
echo Running bundle smoke test (Zee.exe doctor --smoke)...
"%BUILD_ROOT%\dist\Zee\Zee.exe" doctor --smoke
if errorlevel 1 (
    echo [ERROR] Bundle smoke test failed (Zee.exe doctor --smoke exited nonzero).
    exit /b 1
)
echo [OK] Bundle smoke test passed.

REM --- 4. Code-sign the exe (optional) ---------------------------------
if "%ZEE_SIGNING%"=="1" call :sign "%BUILD_ROOT%\dist\Zee\Zee.exe"

REM --- 5. Inno Setup installer (optional) ------------------------------
set "ISCC="
if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not defined ISCC if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"

if defined ISCC (
    "%ISCC%" "%BUILD_ROOT%\scripts\make_installer.iss"
    if errorlevel 1 (
        echo [ERROR] Inno Setup compile failed.
        exit /b 1
    )
    if "%ZEE_SIGNING%"=="1" (
        for %%f in ("%BUILD_ROOT%\artifacts\Zee-Setup-*.exe") do call :sign "%%f"
    )
    echo [OK] Installer: %BUILD_ROOT%\artifacts\Zee-Setup-*.exe
) else (
    echo [WARN] Inno Setup not found - skipped installer. Exe only.
)

exit /b 0

:sign
REM Sign a PE with signtool using the cert in WINDOWS_CERT_BASE64_FILE.
if "%SIGNTOOL%"=="" (
    where signtool >nul 2>nul
    if errorlevel 1 (
        echo [WARN] ZEE_SIGNING=1 but signtool not on PATH; skipping signing.
        exit /b 0
    )
    set SIGNTOOL=signtool
)
if "%WINDOWS_CERT_BASE64_FILE%"=="" (
    echo [WARN] ZEE_SIGNING=1 but WINDOWS_CERT_BASE64_FILE not set; skipping signing.
    exit /b 0
)
echo Converting certificate to PFX...
certutil -decode -f "%WINDOWS_CERT_BASE64_FILE%" "%BUILD_ROOT%\artifacts\code-sign.pfx" >nul
if errorlevel 1 (
    echo [ERROR] certutil failed to decode the certificate.
    exit /b 1
)
"%SIGNTOOL%" sign /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 /f "%BUILD_ROOT%\artifacts\code-sign.pfx" /p "%CERT_PASSWORD%" "%~1"
if errorlevel 1 (
    echo [ERROR] signtool failed for %~1
    exit /b 1
)
echo [OK] Signed %~1
exit /b 0