@echo off
REM ============================================================
REM  HanryxVault Monitor — Windows EXE builder
REM  Run this from the pi-setup\ folder:
REM      build_exe.bat
REM
REM  Output: dist\HanryxVaultMonitor.exe  (single file, no console)
REM ============================================================

echo.
echo  HanryxVault Monitor — building Windows EXE...
echo.

REM Install / upgrade dependencies
pip install --upgrade psutil pyinstaller

echo.
echo  Running PyInstaller...
echo.

pyinstaller ^
    --onefile ^
    --windowed ^
    --name HanryxVaultMonitor ^
    --add-data "." ^
    --clean ^
    desktop_monitor.py

echo.
if exist dist\HanryxVaultMonitor.exe (
    echo  SUCCESS — exe created at:
    echo    %cd%\dist\HanryxVaultMonitor.exe
    echo.
    echo  Copy HanryxVaultMonitor.exe to any Windows PC and run it.
    echo  On first launch, go to the Settings tab and enter your Pi IP.
) else (
    echo  BUILD FAILED — check the output above for errors.
)
echo.
pause
