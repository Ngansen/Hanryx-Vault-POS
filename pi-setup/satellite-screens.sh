#!/usr/bin/env bash
# =============================================================================
# satellite-screens.sh — runtime tool to pin admin/kiosk to specific HDMI outputs
#
# Lets you swap which screen shows admin vs kiosk WITHOUT re-running the
# 900-line installer. Edits ~/.hanryx/satellite.conf, regenerates the labwc
# rc.xml, and reloads the launcher.
#
# Usage:
#   bash ~/Hanryx-Vault-POS/pi-setup/satellite-screens.sh           # interactive picker
#   bash satellite-screens.sh --admin HDMI-A-1 --kiosk HDMI-A-2     # explicit
#   bash satellite-screens.sh --swap                                # flip current assignment
#   bash satellite-screens.sh --auto                                # clear pinning, fall back to size detection
#   bash satellite-screens.sh --list                                # just show outputs and current pinning
# =============================================================================
set -u

CURRENT_USER="${SUDO_USER:-$(whoami)}"
HOME_DIR=$(getent passwd "$CURRENT_USER" | cut -d: -f6)
CONFIG_FILE="$HOME_DIR/.hanryx/satellite.conf"
LABWC_RC="$HOME_DIR/.config/labwc/rc.xml"
LAUNCH_SCRIPT="$HOME_DIR/.hanryx-dual-monitor.sh"

C='\033[0;36m'; G='\033[0;32m'; Y='\033[1;33m'; R='\033[0;31m'; B='\033[1m'; N='\033[0m'
ok()   { echo -e "${G}[✓]${N} $1"; }
info() { echo -e "${C}[i]${N} $1"; }
warn() { echo -e "${Y}[!]${N} $1"; }
bad()  { echo -e "${R}[✗]${N} $1"; }

require_user_session() {
    # Need an active Wayland session to query wlr-randr; if not present, we
    # rely on whatever is in the config or the user's CLI args.
    RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
    SOCK=$(ls "$RUNTIME_DIR"/wayland-* 2>/dev/null | grep -v lock | head -1 || true)
    if [ -n "$SOCK" ]; then
        export WAYLAND_DISPLAY="$(basename "$SOCK")"
    fi
}

list_outputs() {
    require_user_session
    if ! command -v wlr-randr >/dev/null 2>&1; then
        warn "wlr-randr not installed — install with: sudo apt install -y wlr-randr"
        return 1
    fi
    if [ -z "${WAYLAND_DISPLAY:-}" ]; then
        warn "no Wayland session — can only show pinned config, not live outputs"
        return 1
    fi
    wlr-randr 2>/dev/null
}

current_pinning() {
    if [ -f "$CONFIG_FILE" ]; then
        local a k
        a=$(grep '^ADMIN_OUTPUT=' "$CONFIG_FILE" 2>/dev/null | cut -d= -f2 | tr -d '"')
        k=$(grep '^KIOSK_OUTPUT=' "$CONFIG_FILE" 2>/dev/null | cut -d= -f2 | tr -d '"')
        echo "${a:-(unset)}|${k:-(unset)}"
    else
        echo "(unset)|(unset)"
    fi
}

write_pinning() {
    local admin="$1" kiosk="$2"
    mkdir -p "$(dirname "$CONFIG_FILE")"
    touch "$CONFIG_FILE"
    # Strip existing entries
    grep -v '^ADMIN_OUTPUT=' "$CONFIG_FILE" 2>/dev/null \
        | grep -v '^KIOSK_OUTPUT=' > "${CONFIG_FILE}.tmp" || true
    mv "${CONFIG_FILE}.tmp" "$CONFIG_FILE"
    if [ -n "$admin" ] && [ -n "$kiosk" ]; then
        echo "ADMIN_OUTPUT=$admin" >> "$CONFIG_FILE"
        echo "KIOSK_OUTPUT=$kiosk" >> "$CONFIG_FILE"
        ok "Pinned ADMIN=$admin  KIOSK=$kiosk in $CONFIG_FILE"
    else
        ok "Cleared screen pinning — launcher will fall back to size detection"
    fi
    chown "$CURRENT_USER:$CURRENT_USER" "$CONFIG_FILE" 2>/dev/null || true
}

regenerate_rc_xml() {
    local admin="$1" kiosk="$2"
    [ -z "$admin" ] || [ -z "$kiosk" ] && return 0
    mkdir -p "$(dirname "$LABWC_RC")"
    cat > "$LABWC_RC" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<openbox_config xmlns="http://openbox.org/3.4/rc">
  <windowRules>
    <windowRule identifier="hvault-admin" matchType="exact">
      <action name="MoveToOutput"><output>${admin}</output></action>
    </windowRule>
    <windowRule identifier="hvault-kiosk" matchType="exact">
      <action name="MoveToOutput"><output>${kiosk}</output></action>
    </windowRule>
  </windowRules>
</openbox_config>
EOF
    chown -R "$CURRENT_USER:$CURRENT_USER" "$(dirname "$LABWC_RC")" 2>/dev/null || true
    ok "Wrote $LABWC_RC"
    # Reload labwc if it's running
    pkill -SIGHUP labwc 2>/dev/null || true
}

reload_launcher() {
    if pgrep -f "$LAUNCH_SCRIPT" >/dev/null 2>&1; then
        info "Restarting launcher to pick up new assignment…"
        pkill -f chromium 2>/dev/null || true
        pkill -f "$LAUNCH_SCRIPT" 2>/dev/null || true
        sleep 2
        require_user_session
        nohup bash "$LAUNCH_SCRIPT" >/dev/null 2>&1 & disown
        ok "Launcher restarted"
    else
        warn "Launcher not currently running — start it with: bash $LAUNCH_SCRIPT"
    fi
}

interactive_pick() {
    require_user_session
    info "Querying outputs…"
    local outputs_raw outputs names
    outputs_raw=$(wlr-randr 2>/dev/null || true)
    if [ -z "$outputs_raw" ]; then
        bad "Cannot query wlr-randr (no Wayland session?)"
        return 1
    fi
    mapfile -t names < <(echo "$outputs_raw" | grep -E '^[A-Za-z][A-Za-z0-9_-]+ ' | awk '{print $1}')
    if [ ${#names[@]} -lt 2 ]; then
        bad "Only ${#names[@]} output(s) detected — both monitors must be plugged in for this to work"
        echo "Detected: ${names[*]}"
        return 1
    fi

    echo ""
    echo "Detected outputs:"
    local i=1
    for n in "${names[@]}"; do
        local size
        size=$(echo "$outputs_raw" | awk -v out="$n" '$0 ~ "^"out" " {flag=1;next} /^[A-Za-z]/{flag=0} flag && /current/{print; exit}' | head -1 | xargs)
        echo "  $i) $n   ${size:-(no current mode)}"
        i=$((i+1))
    done
    echo ""

    local admin_idx kiosk_idx
    read -rp "  Which output is the ADMIN screen (10.1\")? [1-${#names[@]}]: " admin_idx
    read -rp "  Which output is the KIOSK screen (5\")?    [1-${#names[@]}]: " kiosk_idx

    if ! [[ "$admin_idx" =~ ^[0-9]+$ ]] || [ "$admin_idx" -lt 1 ] || [ "$admin_idx" -gt "${#names[@]}" ]; then
        bad "Invalid admin selection"; return 1
    fi
    if ! [[ "$kiosk_idx" =~ ^[0-9]+$ ]] || [ "$kiosk_idx" -lt 1 ] || [ "$kiosk_idx" -gt "${#names[@]}" ]; then
        bad "Invalid kiosk selection"; return 1
    fi
    if [ "$admin_idx" = "$kiosk_idx" ]; then
        bad "Admin and kiosk cannot be the same output"; return 1
    fi

    local admin="${names[$((admin_idx-1))]}"
    local kiosk="${names[$((kiosk_idx-1))]}"

    write_pinning "$admin" "$kiosk"
    regenerate_rc_xml "$admin" "$kiosk"
    reload_launcher
}

# ── Argument parsing ────────────────────────────────────────────────────────
ADMIN=""; KIOSK=""; ACTION="interactive"
while [ $# -gt 0 ]; do
    case "$1" in
        --admin) ADMIN="$2"; shift 2 ;;
        --kiosk) KIOSK="$2"; shift 2 ;;
        --swap)  ACTION="swap"; shift ;;
        --auto)  ACTION="auto"; shift ;;
        --list)  ACTION="list"; shift ;;
        -h|--help)
            sed -n '2,15p' "$0"; exit 0 ;;
        *) warn "unknown arg: $1"; shift ;;
    esac
done

if [ -n "$ADMIN" ] && [ -n "$KIOSK" ]; then
    ACTION="explicit"
fi

case "$ACTION" in
    list)
        echo ""
        info "Currently pinned in config:"
        IFS='|' read -r a k < <(current_pinning)
        echo "    ADMIN_OUTPUT = $a"
        echo "    KIOSK_OUTPUT = $k"
        echo ""
        info "Live outputs (wlr-randr):"
        list_outputs | sed 's/^/    /' || true
        ;;
    swap)
        IFS='|' read -r a k < <(current_pinning)
        if [ "$a" = "(unset)" ] || [ "$k" = "(unset)" ]; then
            bad "No current pinning to swap — run interactively first"
            exit 1
        fi
        write_pinning "$k" "$a"
        regenerate_rc_xml "$k" "$a"
        reload_launcher
        ;;
    auto)
        write_pinning "" ""
        # Don't touch rc.xml — launcher will rewrite on next start
        reload_launcher
        ;;
    explicit)
        write_pinning "$ADMIN" "$KIOSK"
        regenerate_rc_xml "$ADMIN" "$KIOSK"
        reload_launcher
        ;;
    interactive)
        interactive_pick
        ;;
esac
