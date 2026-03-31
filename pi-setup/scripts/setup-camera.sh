#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════════
#  HanryxVault — Pi Camera Setup for Pokémon Card Scanning
#
#  Optimises a Pi Camera Module (v2 / v3 / HQ) for scanning glossy trading cards.
#
#  The main enemy for card scanning is GLARE on the glossy surface.
#  These settings attack it from multiple angles:
#    • Slightly under-expose to stop highlights blowing out on shiny holos
#    • Disable auto white-balance (consistent colour under artificial light)
#    • Higher sharpness to read fine card text and card numbers
#    • Longer shutter speed for low-light show floors (better than pushing ISO)
#    • Flip/rotation options if your camera is mounted upside down
#
#  Usage:
#    sudo bash scripts/setup-camera.sh
#
#  After running:
#    • Snap a test capture:  libcamera-still -o /tmp/test_card.jpg --tuning-file /opt/hanryxvault/card_scan.json
#    • Check the result and tweak SHARPNESS / EV_COMPENSATION below if needed
# ═══════════════════════════════════════════════════════════════════════════════

set -euo pipefail

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; RED='\033[0;31m'; NC='\033[0m'
info()  { echo -e "${GREEN}[camera]${NC} $*"; }
warn()  { echo -e "${YELLOW}[camera]${NC} $*"; }
step()  { echo -e "${CYAN}══ $* ══${NC}"; }
error() { echo -e "${RED}[camera] ERROR:${NC} $*"; exit 1; }

[[ $EUID -ne 0 ]] && error "Run with sudo"

INSTALL_DIR="/opt/hanryxvault"
BOOT_CONFIG="/boot/firmware/config.txt"
[[ ! -f "$BOOT_CONFIG" ]] && BOOT_CONFIG="/boot/config.txt"   # older Pi OS

# ── Detect camera module ───────────────────────────────────────────────────────
step "Detecting camera module"
CAM_MODEL="unknown"
if command -v libcamera-hello >/dev/null 2>&1; then
    CAM_INFO=$(libcamera-hello --list-cameras 2>&1 || true)
    if echo "$CAM_INFO" | grep -qi "imx708"; then
        CAM_MODEL="v3"
        info "Detected: Pi Camera Module 3 (IMX708) — autofocus capable ✓"
    elif echo "$CAM_INFO" | grep -qi "imx477"; then
        CAM_MODEL="hq"
        info "Detected: Pi HQ Camera (IMX477)"
    elif echo "$CAM_INFO" | grep -qi "imx219"; then
        CAM_MODEL="v2"
        info "Detected: Pi Camera Module 2 (IMX219)"
    else
        warn "Could not identify camera model — applying generic settings"
    fi
else
    warn "libcamera-hello not found — install libcamera-apps first"
fi

# ── Enable camera in boot config ───────────────────────────────────────────────
step "Enabling camera interface"
if ! grep -q "^camera_auto_detect=1" "$BOOT_CONFIG" 2>/dev/null; then
    echo ""                         >> "$BOOT_CONFIG"
    echo "# HanryxVault camera"    >> "$BOOT_CONFIG"
    echo "camera_auto_detect=1"    >> "$BOOT_CONFIG"
    info "Added camera_auto_detect=1 to $BOOT_CONFIG"
else
    info "camera_auto_detect already set"
fi

# Disable legacy camera stack (required for libcamera on Pi OS Bullseye+)
if grep -q "^start_x=1" "$BOOT_CONFIG" 2>/dev/null; then
    sed -i 's/^start_x=1/# start_x=1  # disabled — using libcamera/' "$BOOT_CONFIG"
    info "Disabled legacy camera stack (start_x=1) — libcamera replaces it"
fi

# ── Install libcamera apps ─────────────────────────────────────────────────────
step "Installing libcamera apps"
if ! command -v libcamera-still >/dev/null 2>&1; then
    info "Installing libcamera-apps..."
    apt-get install -y --no-install-recommends libcamera-apps
else
    info "libcamera-apps already installed"
fi

# ── Write card-scan capture script ─────────────────────────────────────────────
# This script is called by the POS server for camera-based card capture.
# Tweak the values here for your specific lighting conditions.
step "Writing card-scan capture script"

cat > "$INSTALL_DIR/card_capture.sh" << 'CAPTURE_EOF'
#!/usr/bin/env bash
# ───────────────────────────────────────────────────────────────────────────────
# HanryxVault Card Capture
# Captures a single high-quality still for Pokémon card identification.
#
# Usage:
#   bash card_capture.sh [output_path]
#   Defaults to /tmp/card_scan.jpg
#
# Called by the POS server's /card/capture endpoint.
# Tune the settings below for your lighting setup.
# ───────────────────────────────────────────────────────────────────────────────

OUTPUT="${1:-/tmp/card_scan.jpg}"
LOG_TAG="[card-capture]"

# ── Core settings (tune these for your bench lighting) ───────────────────────

# EV compensation:  -0.5 = slightly underexpose to tame glossy highlights
# Range: -4.0 to +4.0   (0 = auto, negative = darker)
EV_COMPENSATION="-0.5"

# Sharpness: boost card text and fine detail
# Range: 0.0 – 16.0     (1.0 = default)
SHARPNESS="2.0"

# Contrast: mild boost for worn/faded cards
# Range: 0.0 – 32.0     (1.0 = default)
CONTRAST="1.1"

# Saturation: slightly reduce to stop holofoil from blowing out colour channels
# Range: 0.0 – 32.0     (1.0 = default)
SATURATION="0.9"

# White balance: "incandescent" for shop/show lighting, "daylight" for natural light
# Options: auto | incandescent | tungsten | fluorescent | indoor | daylight | cloudy
AWB_MODE="indoor"

# Autofocus mode (Pi Camera 3 only — ignored on v2/HQ which have fixed focus)
# "auto" = single-shot AF before capture
# "manual" = use LENS_POSITION below for fixed focus
AF_MODE="auto"

# Manual focus position (0.0 = infinity, higher = closer; ~8-12 for ~15cm card distance)
# Only used when AF_MODE="manual"
LENS_POSITION="10.0"

# Output resolution — 1920x1440 gives plenty of detail without huge files
WIDTH=1920
HEIGHT=1440

# JPEG quality
QUALITY=90

# Rotation: 0, 90, 180, 270 — set to 180 if camera is mounted upside-down
ROTATION=0

# ── Build libcamera-still command ─────────────────────────────────────────────
CMD=(
    libcamera-still
    --output "$OUTPUT"
    --width  "$WIDTH"
    --height "$HEIGHT"
    --quality "$QUALITY"
    --ev "$EV_COMPENSATION"
    --sharpness "$SHARPNESS"
    --contrast "$CONTRAST"
    --saturation "$SATURATION"
    --awb "$AWB_MODE"
    --rotation "$ROTATION"
    --nopreview
    --immediate       # capture without a preview delay
    --timeout 2000    # 2 s for AE/AWB to settle before capture
)

# Autofocus (Pi Camera 3 only)
if [[ "$AF_MODE" == "manual" ]]; then
    CMD+=(--autofocus-mode manual --lens-position "$LENS_POSITION")
else
    CMD+=(--autofocus-mode auto)
fi

echo "$LOG_TAG Capturing: ${CMD[*]}"
"${CMD[@]}" 2>&1

EXIT_CODE=$?
if [[ $EXIT_CODE -eq 0 ]]; then
    SIZE=$(stat -c%s "$OUTPUT" 2>/dev/null || echo "?")
    echo "$LOG_TAG Saved: $OUTPUT  (${SIZE} bytes)"
else
    echo "$LOG_TAG Capture failed with exit code $EXIT_CODE"
fi
exit $EXIT_CODE
CAPTURE_EOF

chmod +x "$INSTALL_DIR/card_capture.sh"
info "Wrote $INSTALL_DIR/card_capture.sh"

# ── Lighting advice ────────────────────────────────────────────────────────────
step "Camera placement guide for glossy cards"
echo ""
echo -e "${CYAN}  Best results for glossy Pokémon cards:${NC}"
echo ""
echo "  Distance:   15–20 cm from card surface (fills frame, good AF range)"
echo "  Angle:      Tilt camera 5–10° off vertical to push glare away from lens"
echo "              (even a small tilt eliminates most holo glare)"
echo "  Lighting:   Two diffuse lights at 45° either side"
echo "              Avoid single overhead light — causes centre hotspot on holos"
echo "              LED ring lights are NOT ideal (creates circular glare on foil)"
echo "  Background: Dark matte surface behind the card (black felt is perfect)"
echo "              High-contrast border helps the scanner isolate the card"
echo ""
echo -e "${CYAN}  Quick test (after reboot):${NC}"
echo ""
echo "    bash $INSTALL_DIR/card_capture.sh /tmp/test.jpg"
echo "    # View on your laptop: scp pi@<ip>:/tmp/test.jpg ."
echo ""

# ── FPC cable reminder for Pi Camera v3 ───────────────────────────────────────
if [[ "$CAM_MODEL" == "v3" ]]; then
    echo -e "${YELLOW}  Pi Camera Module 3 note:${NC}"
    echo "  • Use the CAM0 (not DISP) connector on Pi 5"
    echo "  • The ribbon cable gold contacts face AWAY from the board on Pi 5"
    echo "  • EV compensation and autofocus settings above are already tuned for IMX708"
    echo ""
fi

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
info "Camera setup complete."
warn "A REBOOT is required for camera_auto_detect to take effect."
echo ""
info "After reboot, test with:"
info "  libcamera-hello --timeout 5000"
info "  bash $INSTALL_DIR/card_capture.sh /tmp/test_card.jpg"
echo ""
