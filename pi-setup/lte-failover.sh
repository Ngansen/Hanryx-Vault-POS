#!/usr/bin/env bash
# =============================================================================
# HanryxVault — LTE Failover Heartbeat
# =============================================================================
# Pings a reliable upstream every 30 s over the primary route. If 3 consecutive
# pings fail (>90 s offline), switches the default route to the LTE modem.
# Pings again over LTE; if primary recovers (3 successes), switches back.
#
# Install:
#   sudo cp lte-failover.sh /usr/local/bin/hanryx-lte-failover.sh
#   sudo chmod +x /usr/local/bin/hanryx-lte-failover.sh
#   sudo cp lte-failover.service /etc/systemd/system/
#   sudo systemctl daemon-reload
#   sudo systemctl enable --now lte-failover.service
#
# Configure these for your hardware:
#   PRIMARY_IF   — wired/wifi interface (e.g. eth0, wlan0)
#   LTE_IF       — LTE modem interface (e.g. wwan0, usb0)
#   PRIMARY_GW   — gateway IP for the primary interface (your router)
#   LTE_GW       — gateway IP / leave blank to auto-detect from the LTE iface
# =============================================================================
set -u

PRIMARY_IF="${PRIMARY_IF:-eth0}"
LTE_IF="${LTE_IF:-wwan0}"
PRIMARY_GW="${PRIMARY_GW:-192.168.86.1}"
LTE_GW="${LTE_GW:-}"
PING_TARGET="${PING_TARGET:-1.1.1.1}"
INTERVAL="${INTERVAL:-30}"
FAIL_THRESHOLD="${FAIL_THRESHOLD:-3}"
RECOVER_THRESHOLD="${RECOVER_THRESHOLD:-3}"
LOG_TAG="hanryx-lte"

log()  { logger -t "$LOG_TAG" -- "$*"; echo "[$(date '+%H:%M:%S')] $*"; }

current_default_iface() {
    ip route show default | awk '/^default/ {print $5; exit}'
}

switch_to_lte() {
    if [ -z "$LTE_GW" ]; then
        # Prefer an existing default route bound to the LTE iface (modems set
        # this up via dhclient / ModemManager).
        LTE_GW="$(ip route show default dev "$LTE_IF" | awk '/^default/ {print $3; exit}')"
        # Fall back to a non-default route on the iface (peer/p2p modems)
        if [ -z "$LTE_GW" ]; then
            LTE_GW="$(ip route show dev "$LTE_IF" | awk '/via/ {for(i=1;i<=NF;i++) if($i=="via"){print $(i+1); exit}}')"
        fi
    fi
    if [ -z "$LTE_GW" ]; then
        log "FAIL: cannot determine LTE gateway on $LTE_IF, aborting failover"
        return 1
    fi
    log "Switching default route to LTE ($LTE_IF via $LTE_GW)"
    ip route del default 2>/dev/null || true
    ip route add default via "$LTE_GW" dev "$LTE_IF" metric 50
}

switch_to_primary() {
    log "Switching default route back to primary ($PRIMARY_IF via $PRIMARY_GW)"
    ip route del default 2>/dev/null || true
    ip route add default via "$PRIMARY_GW" dev "$PRIMARY_IF" metric 100
}

probe() {
    # -I binds the source interface so we test that path specifically
    local iface="$1"
    ping -c 1 -W 2 -I "$iface" "$PING_TARGET" >/dev/null 2>&1
}

log "Starting LTE failover heartbeat: primary=$PRIMARY_IF lte=$LTE_IF target=$PING_TARGET"

fails=0
ok=0
on_lte=0

while :; do
    if [ "$on_lte" -eq 0 ]; then
        if probe "$PRIMARY_IF"; then
            fails=0
        else
            fails=$((fails+1))
            log "Primary probe failed ($fails/$FAIL_THRESHOLD)"
            if [ "$fails" -ge "$FAIL_THRESHOLD" ]; then
                if switch_to_lte; then
                    on_lte=1
                    fails=0
                    ok=0
                fi
            fi
        fi
    else
        # On LTE — count successful primary probes for recovery
        if probe "$PRIMARY_IF"; then
            ok=$((ok+1))
            log "Primary recovered ($ok/$RECOVER_THRESHOLD)"
            if [ "$ok" -ge "$RECOVER_THRESHOLD" ]; then
                switch_to_primary
                on_lte=0
                ok=0
                fails=0
            fi
        else
            ok=0
        fi
    fi
    sleep "$INTERVAL"
done
