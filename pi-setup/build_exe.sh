#!/usr/bin/env bash
# ============================================================
#  HanryxVault Monitor — Linux / Pi EXE builder
#  (produces a self-contained binary using PyInstaller)
#
#  Run from the pi-setup/ directory:
#      chmod +x build_exe.sh && ./build_exe.sh
#
#  Output: dist/HanryxVaultMonitor
# ============================================================
set -e

echo
echo " HanryxVault Monitor — building Linux binary..."
echo

pip3 install --upgrade psutil pyinstaller

pyinstaller \
    --onefile \
    --windowed \
    --name HanryxVaultMonitor \
    --clean \
    desktop_monitor.py

echo
if [ -f dist/HanryxVaultMonitor ]; then
    echo " SUCCESS — binary created at:"
    echo "   $(pwd)/dist/HanryxVaultMonitor"
    echo
    echo " To run: ./dist/HanryxVaultMonitor"
else
    echo " BUILD FAILED — check the output above."
fi
echo
