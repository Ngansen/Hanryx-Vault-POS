#!/usr/bin/env bash
# usb_resilience_setup.sh
# ---------------------------------------------------------------------------
# Idempotent host-side hardening for the /mnt/cards USB drive on the Pi 5.
#
# What it does (each step is independently safe to re-run):
#   1. /etc/fstab — ensures the /mnt/cards line carries:
#        - errors=remount-ro       drop to RO on I/O error instead of EIO-limbo
#        - x-systemd.device-timeout=10s  don't hang boot if drive is missing
#        - commit=60               batch journal writes (less wear, safer)
#      Existing options (defaults,noatime,nofail) are preserved.
#   2. /etc/udev/rules.d/99-hanryxcards.rules — auto-remount when the drive
#      re-enumerates after a USB hiccup, no manual `mount -a` needed.
#   3. /etc/cron.d/cards-heartbeat — every minute, touch a heartbeat file on
#      /mnt/cards; on failure log a WARN to syslog so you can `journalctl -t
#      cards-mon -f` and catch a flake before a customer does.
#
# Usage:
#   sudo ./usb_resilience_setup.sh             # DRY RUN — show what would change
#   sudo ./usb_resilience_setup.sh --apply     # actually write changes
#   sudo ./usb_resilience_setup.sh --revert    # restore previous fstab/udev/cron
#
# All edits create timestamped backups in /etc/ so you can roll back manually:
#   sudo cp /etc/fstab.bak.<timestamp> /etc/fstab
# ---------------------------------------------------------------------------

set -euo pipefail

DRY_RUN=1
REVERT=0
LABEL="hanryxcards"
MOUNTPOINT="/mnt/cards"
TS="$(date +%Y%m%d-%H%M%S)"

# ------- arg parsing --------------------------------------------------------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --apply)   DRY_RUN=0; shift ;;
        --revert)  REVERT=1;  shift ;;
        -h|--help)
            sed -n '2,/^# ---$/p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *) echo "Unknown arg: $1 (try --help)" >&2; exit 1 ;;
    esac
done

# ------- helpers ------------------------------------------------------------
log() { printf '\033[1;36m[setup]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[setup]\033[0m %s\n' "$*" >&2; }
err()  { printf '\033[1;31m[setup]\033[0m %s\n' "$*" >&2; }

require_root() {
    if [[ $EUID -ne 0 ]]; then
        err "Must be run as root.  Try: sudo $0 ${*:-}"
        exit 1
    fi
}

write_file() {
    # write_file <path> <heredoc-content>
    local path="$1" content="$2"
    if [[ -f "$path" ]] && [[ "$(cat "$path")" == "$content" ]]; then
        log "  unchanged: $path"
        return 0
    fi
    if [[ -f "$path" ]]; then
        if [[ $DRY_RUN -eq 0 ]]; then
            cp -a "$path" "${path}.bak.${TS}"
        fi
        log "  backup:    ${path}.bak.${TS}"
    fi
    if [[ $DRY_RUN -eq 1 ]]; then
        log "  WOULD WRITE: $path"
        printf '%s\n' "$content" | sed 's/^/    | /'
    else
        printf '%s\n' "$content" > "$path"
        log "  wrote:     $path"
    fi
}

# ------- 1. fstab -----------------------------------------------------------
patch_fstab() {
    log "Step 1/3: patching /etc/fstab line for ${MOUNTPOINT}"

    if ! grep -qE "^[^#]*[[:space:]]${MOUNTPOINT}[[:space:]]" /etc/fstab; then
        warn "  no ${MOUNTPOINT} line in /etc/fstab — skipping (mount it manually first)"
        return 0
    fi

    # Read the current line; rewrite the options field (col 4) to ensure all
    # required options are present without losing existing ones.
    local current desired
    current=$(awk -v mp="$MOUNTPOINT" '$2 == mp && $1 !~ /^#/ {print; exit}' /etc/fstab)

    # Build the union: existing options + required options, deduped.
    local existing_opts new_opts merged
    existing_opts=$(awk '{print $4}' <<<"$current")
    new_opts="errors=remount-ro,x-systemd.device-timeout=10s,commit=60"
    merged=$(echo "${existing_opts},${new_opts}" \
             | tr ',' '\n' \
             | awk 'NF && !seen[$0]++' \
             | paste -sd',' -)

    desired=$(awk -v new="$merged" '{$4=new; print}' OFS='\t' <<<"$current")

    if [[ "$current" == "$desired" ]]; then
        log "  unchanged: fstab already has all hardened options"
        return 0
    fi

    log "  diff:"
    log "    -  $current"
    log "    +  $desired"

    if [[ $DRY_RUN -eq 0 ]]; then
        cp -a /etc/fstab "/etc/fstab.bak.${TS}"
        log "  backup:    /etc/fstab.bak.${TS}"
        # Use a python one-liner for the in-place line replacement so we
        # don't have to escape forward-slashes/whitespace through sed.
        python3 - "$current" "$desired" <<'PY'
import sys, pathlib
old, new = sys.argv[1], sys.argv[2]
p = pathlib.Path("/etc/fstab")
text = p.read_text()
if old not in text:
    sys.exit("ERROR: original fstab line vanished mid-edit; aborting")
p.write_text(text.replace(old, new, 1))
PY
        log "  wrote:     /etc/fstab"
        # Reload the systemd mount unit so the new options take effect on
        # the next remount without requiring a reboot.
        systemctl daemon-reload || true
    fi
}

# ------- 2. udev rule -------------------------------------------------------
install_udev_rule() {
    log "Step 2/3: installing udev auto-remount rule"
    local path="/etc/udev/rules.d/99-hanryxcards.rules"
    local content
    content=$(cat <<EOF
# Auto-remount the labeled card-storage drive (${LABEL}) the moment the
# kernel re-enumerates it after a USB hiccup.  Pairs with the nofail line in
# /etc/fstab so a missing drive at boot doesn't block startup, and with this
# rule the drive returns to /mnt/cards on its own when it comes back.
#
# Installed by pi-setup/usb_resilience_setup.sh — re-run that script to
# update or remove this file.
ACTION=="add", SUBSYSTEM=="block", ENV{ID_FS_LABEL}=="${LABEL}", \\
    RUN+="/bin/systemctl restart $(systemd-escape -p --suffix=mount "${MOUNTPOINT}")"
EOF
)
    write_file "$path" "$content"

    if [[ $DRY_RUN -eq 0 ]]; then
        udevadm control --reload
        log "  reloaded:  udev rules"
    fi
}

# ------- 3. heartbeat cron --------------------------------------------------
install_heartbeat() {
    log "Step 3/3: installing /mnt/cards heartbeat cron"
    local path="/etc/cron.d/cards-heartbeat"
    local content
    content=$(cat <<EOF
# Touch a heartbeat file on the card-storage drive every minute.  If the
# write fails, log a WARN to syslog under the 'cards-mon' tag so you can
# tail flakes in real time:
#
#     journalctl -t cards-mon -f
#
# Installed by pi-setup/usb_resilience_setup.sh — re-run that script to
# update or remove this file.
SHELL=/bin/sh
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
* * * * * root touch ${MOUNTPOINT}/.heartbeat 2>/dev/null || /usr/bin/logger -t cards-mon "WARN: ${MOUNTPOINT} write failed at \$(date -Iseconds)"
EOF
)
    write_file "$path" "$content"
}

# ------- revert -------------------------------------------------------------
revert() {
    log "Reverting USB resilience changes (most recent backups)"

    # fstab — pick the most recent backup
    local last_fstab
    last_fstab=$(ls -1t /etc/fstab.bak.* 2>/dev/null | head -1 || true)
    if [[ -n "$last_fstab" ]]; then
        if [[ $DRY_RUN -eq 0 ]]; then
            cp -a "$last_fstab" /etc/fstab
            log "  restored:  /etc/fstab from $last_fstab"
        else
            log "  WOULD restore /etc/fstab from $last_fstab"
        fi
    else
        warn "  no /etc/fstab.bak.* found — nothing to revert"
    fi

    # udev + cron
    for f in /etc/udev/rules.d/99-hanryxcards.rules /etc/cron.d/cards-heartbeat; do
        if [[ -f "$f" ]]; then
            if [[ $DRY_RUN -eq 0 ]]; then
                rm -f "$f"
                log "  removed:   $f"
            else
                log "  WOULD remove $f"
            fi
        fi
    done

    if [[ $DRY_RUN -eq 0 ]]; then
        udevadm control --reload || true
        systemctl daemon-reload || true
    fi
}

# ------- summary ------------------------------------------------------------
post_summary() {
    cat <<EOF

────────────────────────────────────────────────────────────────────────
Done.  Recommended verification:

  # 1. Re-apply fstab options (no reboot needed; remount in place):
  sudo mount -o remount ${MOUNTPOINT}
  mount | grep ${MOUNTPOINT}     # should show errors=remount-ro,commit=60,…

  # 2. Confirm the udev rule + heartbeat are in place:
  ls -la /etc/udev/rules.d/99-hanryxcards.rules /etc/cron.d/cards-heartbeat

  # 3. Tail the heartbeat — silence is healthy, WARNs are bad:
  journalctl -t cards-mon -f

  # 4. Bounce the pos container ONCE so it picks up the new compose
  #    bind-mount (propagation: rslave).  After this, future drive
  #    disconnects no longer require --force-recreate to recover:
  cd ~/Hanryx-Vault-POS/pi-setup
  docker compose up -d pos     # plain 'up -d' is enough (compose detects
                               # the volume change and recreates pos only)

If anything looks wrong, re-run with --revert to roll back.
────────────────────────────────────────────────────────────────────────
EOF
}

# ------- main ---------------------------------------------------------------
main() {
    require_root "$@"

    if [[ $REVERT -eq 1 ]]; then
        revert
    else
        if [[ $DRY_RUN -eq 1 ]]; then
            warn "DRY RUN — no files will be modified.  Re-run with --apply to commit."
        fi
        patch_fstab
        install_udev_rule
        install_heartbeat
    fi

    if [[ $DRY_RUN -eq 0 ]]; then
        post_summary
    fi
}

main "$@"
