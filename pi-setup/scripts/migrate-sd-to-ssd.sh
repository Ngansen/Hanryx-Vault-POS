#!/usr/bin/env bash
# ============================================================================
# HanryxVault — Migrate SD Card → SSD/USB Hard Drive
# ============================================================================
# This script:
#   1. Formats an external drive (SSD/USB/NVMe) as ext4
#   2. Clones the entire SD card to the new drive
#   3. Updates boot config so the Pi boots from the SSD instead
#
# Requirements:
#   - Raspberry Pi 5 with Raspberry Pi OS (Bookworm)
#   - External drive connected via USB or NVMe HAT
#   - Run as root (sudo)
#
# Usage:
#   sudo bash migrate-sd-to-ssd.sh
#
# After running:
#   - Reboot the Pi
#   - It will boot from the SSD
#   - You can remove the SD card once confirmed working
# ============================================================================

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

if [ "$EUID" -ne 0 ]; then
    echo -e "${RED}ERROR: This script must be run as root (sudo).${NC}"
    exit 1
fi

echo ""
echo -e "${GREEN}============================================${NC}"
echo -e "${GREEN}  HanryxVault — SD → SSD Migration Tool${NC}"
echo -e "${GREEN}============================================${NC}"
echo ""

# --------------------------------------------------------------------------
# Step 1: Detect drives
# --------------------------------------------------------------------------
echo -e "${YELLOW}[1/6] Detecting drives...${NC}"
echo ""

SD_DEVICE="/dev/mmcblk0"
if [ ! -b "$SD_DEVICE" ]; then
    echo -e "${RED}Cannot find SD card at $SD_DEVICE${NC}"
    exit 1
fi

echo "SD card: $SD_DEVICE"
echo ""
echo "Available drives (excluding the SD card):"
echo "-------------------------------------------"

lsblk -dpno NAME,SIZE,MODEL | grep -v mmcblk | grep -v loop || true

echo ""
echo -e "${YELLOW}Enter the target drive (e.g. /dev/sda or /dev/nvme0n1):${NC}"
read -r TARGET_DRIVE

if [ ! -b "$TARGET_DRIVE" ]; then
    echo -e "${RED}ERROR: $TARGET_DRIVE does not exist.${NC}"
    exit 1
fi

if [ "$TARGET_DRIVE" = "$SD_DEVICE" ]; then
    echo -e "${RED}ERROR: Target cannot be the SD card itself.${NC}"
    exit 1
fi

TARGET_SIZE=$(lsblk -bdno SIZE "$TARGET_DRIVE")
SD_SIZE=$(lsblk -bdno SIZE "$SD_DEVICE")
TARGET_GB=$((TARGET_SIZE / 1073741824))
SD_GB=$((SD_SIZE / 1073741824))

echo ""
echo "SD card:      ${SD_GB}GB ($SD_DEVICE)"
echo "Target drive: ${TARGET_GB}GB ($TARGET_DRIVE)"

if [ "$TARGET_SIZE" -lt "$SD_SIZE" ]; then
    echo -e "${RED}ERROR: Target drive (${TARGET_GB}GB) is smaller than SD card (${SD_GB}GB).${NC}"
    echo "The target drive must be at least as large as the SD card."
    exit 1
fi

echo ""
echo -e "${RED}╔═══════════════════════════════════════════════════════╗${NC}"
echo -e "${RED}║  WARNING: This will ERASE ALL DATA on $TARGET_DRIVE  ║${NC}"
echo -e "${RED}╚═══════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "Type ${YELLOW}YES${NC} to continue:"
read -r CONFIRM

if [ "$CONFIRM" != "YES" ]; then
    echo "Aborted."
    exit 0
fi

# --------------------------------------------------------------------------
# Step 2: Stop Docker services to ensure clean copy
# --------------------------------------------------------------------------
echo ""
echo -e "${YELLOW}[2/6] Stopping Docker containers for clean copy...${NC}"

if command -v docker &>/dev/null; then
    docker compose -f /home/*/hanryx-vault-pos/pi-setup/docker-compose.yml down 2>/dev/null || true
    docker stop $(docker ps -q) 2>/dev/null || true
    echo "Docker containers stopped."
else
    echo "Docker not running, skipping."
fi

# --------------------------------------------------------------------------
# Step 3: Unmount any existing partitions on the target drive
# --------------------------------------------------------------------------
echo ""
echo -e "${YELLOW}[3/6] Unmounting target drive partitions...${NC}"

for part in $(lsblk -lnpo NAME "$TARGET_DRIVE" | tail -n +2); do
    umount "$part" 2>/dev/null || true
    echo "  Unmounted $part"
done

# --------------------------------------------------------------------------
# Step 4: Clone SD card to target drive
# --------------------------------------------------------------------------
echo ""
echo -e "${YELLOW}[4/6] Cloning SD card to target drive...${NC}"
echo "  This will take 10-30 minutes depending on your SD card size."
echo "  Do NOT unplug anything during this process."
echo ""

dd if="$SD_DEVICE" of="$TARGET_DRIVE" bs=4M status=progress conv=fsync

echo ""
echo -e "${GREEN}Clone complete.${NC}"

# --------------------------------------------------------------------------
# Step 5: Expand the partition to fill the drive
# --------------------------------------------------------------------------
echo ""
echo -e "${YELLOW}[5/6] Expanding partition to fill entire drive...${NC}"

sleep 2
partprobe "$TARGET_DRIVE"
sleep 2

if [[ "$TARGET_DRIVE" == /dev/nvme* ]]; then
    ROOT_PART="${TARGET_DRIVE}p2"
    PART_NUM="2"
elif [[ "$TARGET_DRIVE" == /dev/sd* ]]; then
    ROOT_PART="${TARGET_DRIVE}2"
    PART_NUM="2"
else
    echo -e "${YELLOW}Could not determine partition scheme — you may need to expand manually.${NC}"
    ROOT_PART=""
    PART_NUM=""
fi

if [ -n "$ROOT_PART" ] && [ -b "$ROOT_PART" ]; then
    echo "  Expanding partition $ROOT_PART..."
    parted -s "$TARGET_DRIVE" resizepart "$PART_NUM" 100%
    sleep 1
    e2fsck -f -y "$ROOT_PART" || true
    resize2fs "$ROOT_PART"
    echo -e "${GREEN}  Partition expanded to fill ${TARGET_GB}GB drive.${NC}"
else
    echo -e "${YELLOW}  Skipped partition expansion — check manually with 'sudo parted $TARGET_DRIVE print'${NC}"
fi

# --------------------------------------------------------------------------
# Step 6: Update boot config to use SSD
# --------------------------------------------------------------------------
echo ""
echo -e "${YELLOW}[6/6] Updating boot configuration...${NC}"

TARGET_PARTUUID=$(blkid -s PARTUUID -o value "$ROOT_PART" 2>/dev/null || echo "")

if [ -n "$TARGET_PARTUUID" ]; then
    BOOT_CMDLINE="/boot/firmware/cmdline.txt"
    if [ ! -f "$BOOT_CMDLINE" ]; then
        BOOT_CMDLINE="/boot/cmdline.txt"
    fi

    if [ -f "$BOOT_CMDLINE" ]; then
        cp "$BOOT_CMDLINE" "${BOOT_CMDLINE}.bak.$(date +%Y%m%d)"
        sed -i "s|root=PARTUUID=[^ ]*|root=PARTUUID=${TARGET_PARTUUID}|g" "$BOOT_CMDLINE"
        echo -e "${GREEN}  Boot config updated: root=PARTUUID=${TARGET_PARTUUID}${NC}"
        echo "  Backup saved to ${BOOT_CMDLINE}.bak.*"
    else
        echo -e "${YELLOW}  Could not find cmdline.txt — update manually:${NC}"
        echo "  Set root=PARTUUID=${TARGET_PARTUUID} in /boot/firmware/cmdline.txt"
    fi
else
    echo -e "${YELLOW}  Could not determine PARTUUID — update boot config manually.${NC}"
fi

# --------------------------------------------------------------------------
# Also update fstab on the NEW drive
# --------------------------------------------------------------------------
MOUNT_TMP="/mnt/newssd"
mkdir -p "$MOUNT_TMP"
mount "$ROOT_PART" "$MOUNT_TMP"

if [ -f "$MOUNT_TMP/etc/fstab" ]; then
    SD_ROOT_PARTUUID=$(blkid -s PARTUUID -o value "${SD_DEVICE}p2" 2>/dev/null || echo "")
    if [ -n "$SD_ROOT_PARTUUID" ] && [ -n "$TARGET_PARTUUID" ]; then
        sed -i "s|${SD_ROOT_PARTUUID}|${TARGET_PARTUUID}|g" "$MOUNT_TMP/etc/fstab"
        echo -e "${GREEN}  Updated fstab on new drive.${NC}"
    fi
fi

umount "$MOUNT_TMP"
rmdir "$MOUNT_TMP"

# --------------------------------------------------------------------------
# Done
# --------------------------------------------------------------------------
echo ""
echo -e "${GREEN}============================================${NC}"
echo -e "${GREEN}  Migration complete!${NC}"
echo -e "${GREEN}============================================${NC}"
echo ""
echo "Next steps:"
echo "  1. Reboot:  sudo reboot"
echo "  2. After reboot, verify you're on the SSD:"
echo "     lsblk    (root should be on $TARGET_DRIVE, not mmcblk0)"
echo "     df -h /  (should show the full ${TARGET_GB}GB drive)"
echo "  3. Once confirmed, you can remove the SD card"
echo "     (keep it as a backup!)"
echo ""
echo "  If boot fails, put the SD card back in — it still works."
echo "  Then re-run this script or fix /boot/firmware/cmdline.txt manually."
echo ""
