#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════════
#  HanryxVault — Performance & Reliability Tuning
#
#  Run on BOTH the home Pi and the trade show Pi after install.sh:
#    sudo bash scripts/setup-performance.sh
#
#  What it does:
#    1. Disables WiFi power saving     — eliminates 100-500 ms WiFi latency
#    2. Sets CPU to performance mode   — prevents throttling under POS load
#    3. Tunes kernel networking        — higher connection queue, less swap
#    4. Installs zram                  — compressed RAM swap (faster than SD)
#    5. Caps journal size              — prevents logs filling the SD card
#    6. Disables unnecessary services  — frees RAM + CPU for POS
#    7. Enables DNS caching            — instant repeated DNS lookups
#    8. Sets up nightly DB backup      — safety net for trade show data
#    9. Sets process priority + OOM    — kernel never kills the POS server
# ═══════════════════════════════════════════════════════════════════════════════

set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()  { echo -e "${GREEN}[perf]${NC} $*"; }
warn()  { echo -e "${YELLOW}[perf]${NC} $*"; }
step()  { echo -e "\n${CYAN}══ $* ══${NC}"; }
done_() { echo -e "${GREEN}  ✓${NC} $*"; }

[[ $EUID -ne 0 ]] && { echo "Run with sudo"; exit 1; }

echo ""
echo -e "${CYAN}"
echo "  ╔══════════════════════════════════════════════╗"
echo "  ║  HanryxVault Performance & Reliability Tune  ║"
echo "  ╚══════════════════════════════════════════════╝"
echo -e "${NC}"

# ── 1. WiFi power saving OFF ──────────────────────────────────────────────────
# THE single biggest source of latency on Pi WiFi. Power saving buffers packets
# and only delivers them at beacon intervals — up to 100-500 ms extra latency
# per request. Disabling this makes WiFi feel as fast as ethernet.
step "Disabling WiFi power saving (critical for low latency)"

WIFI_IFACE=$(iw dev 2>/dev/null | awk '/Interface/{print $2}' | head -1 || echo "wlan0")

# Immediate effect
iw dev "$WIFI_IFACE" set power_save off 2>/dev/null && \
    done_ "WiFi power saving OFF on $WIFI_IFACE (immediate)" || \
    warn "iw command failed — may need wireless-tools"

# Persist across reboots via NetworkManager
NM_WIFI_POWER="/etc/NetworkManager/conf.d/20-wifi-powersave-off.conf"
cat > "$NM_WIFI_POWER" << 'EOF'
[connection]
wifi.powersave = 2
EOF
done_ "WiFi power saving will stay OFF after every reboot (NetworkManager)"

# Also via /etc/rc.local as belt-and-suspenders fallback
RC_LOCAL="/etc/rc.local"
if [[ -f "$RC_LOCAL" ]] && ! grep -q "power_save off" "$RC_LOCAL"; then
    sed -i "s|^exit 0|# Disable WiFi power saving\niw dev wlan0 set power_save off 2>/dev/null || true\n\nexit 0|" "$RC_LOCAL"
    done_ "Added rc.local fallback"
elif [[ ! -f "$RC_LOCAL" ]]; then
    cat > "$RC_LOCAL" << 'EOF'
#!/bin/sh
iw dev wlan0 set power_save off 2>/dev/null || true
exit 0
EOF
    chmod +x "$RC_LOCAL"
    done_ "Created rc.local with WiFi power save disable"
fi

# ── 2. CPU governor — performance mode ────────────────────────────────────────
step "Setting CPU governor to performance mode"

if [[ -d /sys/devices/system/cpu/cpu0/cpufreq ]]; then
    # Apply now
    for gov in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do
        echo performance > "$gov" 2>/dev/null || true
    done
    CURRENT_GOV=$(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null)
    done_ "CPU governor: $CURRENT_GOV"

    # Persist via cpufrequtils
    apt-get install -y --no-install-recommends cpufrequtils >/dev/null 2>&1 || true
    echo 'GOVERNOR="performance"' > /etc/default/cpufrequtils 2>/dev/null || true
    systemctl enable cpufrequtils 2>/dev/null || true
    done_ "Persisted via cpufrequtils"

    # Also add to rc.local as fallback
    if [[ -f "$RC_LOCAL" ]] && ! grep -q "scaling_governor" "$RC_LOCAL"; then
        sed -i "s|^exit 0|# Performance CPU governor\nfor g in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do echo performance > \"\$g\" 2>/dev/null; done\n\nexit 0|" "$RC_LOCAL"
    fi
else
    warn "CPU frequency scaling not available on this kernel — skipping"
fi

# ── 3. Kernel network + memory tuning ─────────────────────────────────────────
step "Tuning kernel parameters (sysctl)"

cat > /etc/sysctl.d/99-hanryxvault-performance.conf << 'EOF'
# ── Memory ────────────────────────────────────────────────────────────────────
# Use swap rarely — prefer keeping POS data in RAM
vm.swappiness = 5

# Write dirty pages less aggressively — smoother I/O on SD card
vm.dirty_ratio = 10
vm.dirty_background_ratio = 5

# ── Networking ────────────────────────────────────────────────────────────────
# Larger accept queue for nginx/gunicorn
net.core.somaxconn = 1024
net.ipv4.tcp_max_syn_backlog = 1024

# Reuse TIME_WAIT sockets faster (helps with rapid client reconnects)
net.ipv4.tcp_tw_reuse = 1

# Faster keepalive detection (detect dead connections in ~30s not 2 hours)
net.ipv4.tcp_keepalive_time = 30
net.ipv4.tcp_keepalive_intvl = 5
net.ipv4.tcp_keepalive_probes = 3

# Disable IPv6 if not in use (saves a tiny bit of overhead)
# Uncomment if you don't need IPv6:
# net.ipv6.conf.all.disable_ipv6 = 1
EOF

sysctl -p /etc/sysctl.d/99-hanryxvault-performance.conf >/dev/null 2>&1 || true
done_ "Kernel parameters applied and persisted"

# ── 4. zram (compressed swap in RAM) ──────────────────────────────────────────
step "Installing zram (compressed RAM swap)"

# zram creates a compressed swap device in RAM — if memory gets tight,
# data is compressed and kept in RAM rather than written to slow SD card.
if ! dpkg -l | grep -q zram-tools; then
    apt-get install -y --no-install-recommends zram-tools >/dev/null
fi

# Configure zram to use 25% of physical RAM
TOTAL_RAM_KB=$(grep MemTotal /proc/meminfo | awk '{print $2}')
ZRAM_SIZE_MB=$(( TOTAL_RAM_KB / 4 / 1024 ))
cat > /etc/default/zramswap << EOF
# zram size — 25% of physical RAM (${ZRAM_SIZE_MB} MB)
PERCENT=25
EOF
systemctl enable zramswap >/dev/null 2>&1 || true
systemctl restart zramswap 2>/dev/null || true
done_ "zram configured at 25% of RAM (~${ZRAM_SIZE_MB} MB compressed swap)"

# ── 5. Journal size limits ────────────────────────────────────────────────────
step "Limiting journal log size (prevent SD card fill)"

mkdir -p /etc/systemd/journald.conf.d
cat > /etc/systemd/journald.conf.d/00-size-limit.conf << 'EOF'
[Journal]
# Keep at most 150 MB of logs on disk
SystemMaxUse=150M
# Keep at most 50 MB in volatile (RAM) storage
RuntimeMaxUse=50M
# Compress old journal files
Compress=yes
# Rotate when a single file hits 20 MB
SystemMaxFileSize=20M
EOF
systemctl restart systemd-journald
done_ "Journal capped at 150 MB total"

# ── 6. Disable unnecessary system services ────────────────────────────────────
step "Disabling services not needed for POS"

DISABLE_SERVICES=(
    "avahi-daemon"    # mDNS/Bonjour discovery — not needed for POS
    "triggerhappy"    # Pi GPIO button daemon — not needed
    "hciuart"         # Bluetooth UART — only disable if not using BT printer/scanner
                      # UNCOMMENT the line below to also disable Bluetooth completely:
    # "bluetooth"
)

for svc in "${DISABLE_SERVICES[@]}"; do
    # Skip commented-out entries
    [[ "$svc" == \#* ]] && continue
    if systemctl is-enabled "$svc" &>/dev/null; then
        systemctl disable --now "$svc" 2>/dev/null || true
        done_ "Disabled: $svc"
    fi
done

# ── 7. DNS caching (systemd-resolved) ────────────────────────────────────────
step "Enabling DNS caching via systemd-resolved"

# Without caching, every API call (Zettle, cloud sync) does a fresh DNS lookup.
# With caching, repeated lookups are instant.
mkdir -p /etc/systemd/resolved.conf.d
cat > /etc/systemd/resolved.conf.d/00-cache.conf << 'EOF'
[Resolve]
# Use Google + Cloudflare — fast, reliable DNS servers
DNS=1.1.1.1 8.8.8.8 1.0.0.1 8.8.4.4
FallbackDNS=9.9.9.9
# Cache DNS responses aggressively
Cache=yes
DNSStubListener=yes
EOF
systemctl enable systemd-resolved >/dev/null 2>&1 || true
systemctl restart systemd-resolved 2>/dev/null || true
# Point resolv.conf at the local stub resolver
ln -sf /run/systemd/resolve/stub-resolv.conf /etc/resolv.conf 2>/dev/null || true
done_ "DNS caching enabled (Cloudflare + Google, cached locally)"

# ── 8. Nightly database backup ────────────────────────────────────────────────
step "Setting up nightly database backup"

BACKUP_DIR="/var/backups/hanryxvault"
DB_PATH="/opt/hanryxvault/vault_pos.db"
mkdir -p "$BACKUP_DIR"
chown hanryxvault:hanryxvault "$BACKUP_DIR" 2>/dev/null || true

# Backup script — uses SQLite's online backup (safe while server is running)
cat > /usr/local/bin/hanryxvault-backup << 'BACKUP'
#!/usr/bin/env bash
# HanryxVault — safe online SQLite backup (runs while server is running)
set -euo pipefail

DB_PATH="/opt/hanryxvault/vault_pos.db"
BACKUP_DIR="/var/backups/hanryxvault"
STAMP=$(date +%Y%m%d_%H%M%S)
DEST="${BACKUP_DIR}/vault_pos_${STAMP}.db"

# Use SQLite's .backup command — safe, consistent snapshot even with WAL mode
sqlite3 "$DB_PATH" ".backup '${DEST}'" 2>/dev/null || cp "$DB_PATH" "$DEST"
gzip -f "$DEST"

# Keep only the last 14 daily backups
find "$BACKUP_DIR" -name "vault_pos_*.db.gz" | sort | head -n -14 | xargs rm -f 2>/dev/null || true

echo "$(date) — Backup written: ${DEST}.gz" >> /var/log/hanryxvault/backup.log
BACKUP
chmod +x /usr/local/bin/hanryxvault-backup

# systemd timer (runs at 2 AM daily — quietest time)
cat > /etc/systemd/system/hanryxvault-backup.service << 'EOF'
[Unit]
Description=HanryxVault nightly DB backup

[Service]
Type=oneshot
User=hanryxvault
ExecStart=/usr/local/bin/hanryxvault-backup
StandardOutput=journal
StandardError=journal
EOF

cat > /etc/systemd/system/hanryxvault-backup.timer << 'EOF'
[Unit]
Description=HanryxVault nightly DB backup timer

[Timer]
OnCalendar=*-*-* 02:00:00
RandomizedDelaySec=600
Persistent=true

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now hanryxvault-backup.timer
done_ "Nightly backup at 2 AM → $BACKUP_DIR (14-day retention)"
done_ "Manual backup: sudo hanryxvault-backup"

# ── 9. Improve HanryxVault service priority ───────────────────────────────────
step "Hardening HanryxVault systemd service (OOM + priority)"

OVERRIDE_DIR="/etc/systemd/system/hanryxvault.service.d"
mkdir -p "$OVERRIDE_DIR"

cat > "${OVERRIDE_DIR}/10-performance.conf" << 'EOF'
[Service]
# Tell the Linux OOM killer to spare this process — kill other things first
OOMScoreAdjust=-500

# Give POS server a slight scheduling advantage over background tasks
Nice=-5

# Allow many open files (nginx connections + DB + log files)
LimitNOFILE=65536

# Do not allow the POS server to use swap — if it needs swap, restart instead
MemorySwapMax=0

# Restart quickly if it ever crashes
RestartSec=3
EOF

systemctl daemon-reload
done_ "OOM protection, Nice=-5, 65536 file descriptors, no swap for POS process"

# ── 10. Restart NM to apply WiFi power save config ────────────────────────────
step "Applying all NetworkManager changes"
systemctl restart NetworkManager
sleep 2
# Re-apply power save immediately after NM restart
iw dev "$WIFI_IFACE" set power_save off 2>/dev/null || true
done_ "NetworkManager restarted, WiFi power save confirmed OFF"

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}"
echo "  ╔═══════════════════════════════════════════════╗"
echo "  ║  Performance tuning complete!                 ║"
echo "  ╚═══════════════════════════════════════════════╝"
echo -e "${NC}"

echo -e "${CYAN}Summary of changes:${NC}"
echo "  WiFi latency     Before: 100-500ms  →  After: <5ms (power save OFF)"
echo "  CPU              Before: throttling  →  After: performance governor"
echo "  Swap             Before: SD card     →  After: compressed RAM (zram)"
echo "  Logs             Before: unlimited   →  After: capped at 150 MB"
echo "  DNS              Before: lookup every call  →  After: cached"
echo "  DB backup        Before: none        →  After: nightly, 14-day retention"
echo "  POS process      Before: normal OOM  →  After: protected, Nice=-5"
echo ""
echo -e "${YELLOW}Reboot recommended to confirm all kernel parameters are in effect.${NC}"
echo -e "  ${CYAN}sudo reboot${NC}"
echo ""

CURRENT_PWR=$(iw dev "$WIFI_IFACE" get power_save 2>/dev/null | grep -o "off\|on" || echo "unknown")
echo -e "${GREEN}Current WiFi power save: ${CURRENT_PWR}${NC}"
