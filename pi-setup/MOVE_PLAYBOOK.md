# HanryxVault Move-Day Playbook

Step-by-step for packing up at one venue and getting fully operational at the next, **with no debugging required**.

## One-time setup (per Pi)

```bash
cd ~/Hanryx-Vault-POS && git pull
sudo bash pi-setup/install-reliability.sh
```

That installs:

- `hanryx-recover` — single command to "fix everything"
- `hanryx-heal.timer` — runs the recovery silently every 2 minutes
- `hanryx-boot.service` — blocks kiosk autostart until everything is actually healthy

After that, **you should never need to ssh in to debug after a move**. The system self-heals.

---

## Pre-move WiFi prep

Before you leave for a new venue, **save the venue's WiFi to NetworkManager** while you're still on a working network:

```bash
# Add the venue's WiFi
sudo nmcli connection add type wifi con-name "VENUE_WIFI" ifname wlan0 \
    ssid "VenueSSID" \
    wifi-sec.key-mgmt wpa-psk \
    wifi-sec.psk "venue-password"

sudo nmcli connection modify "VENUE_WIFI" connection.autoconnect yes
```

Also save your **phone hotspot** as a fallback:

```bash
sudo nmcli connection add type wifi con-name "PhoneHotspot" ifname wlan0 \
    ssid "Your-Phone-SSID" \
    wifi-sec.key-mgmt wpa-psk \
    wifi-sec.psk "hotspot-password"

sudo nmcli connection modify "PhoneHotspot" connection.autoconnect yes \
    connection.autoconnect-priority 50
```

Both Pis already have priority on the home WiFi (priority 100). The venue + hotspot fall through automatically.

---

## Move day — at the new venue

1. **Power on the Pis.** That's it. Wait 90 seconds.
2. If something looks wrong on the screen, ssh in from your phone and run **`hanryx-recover`** (one word, no flags). It prints a colored report and fixes anything fixable.
3. If the screen is stuck, run **`hanryx-recover --kiosk-only`** — this only resets Chromium without touching the docker stack.

That's the entire playbook.

---

## What `hanryx-recover` actually does

In order, with retries and timeouts:

1. Waits for the LAN gateway to be reachable (= WiFi up)
2. Waits for Tailscale to have an IP
3. Brings up the POS docker stack (idempotent)
4. Brings up the monitoring stack (Prometheus + Grafana + node-exporter)
5. Restarts only the containers marked unhealthy / restarting / exited
6. Verifies each port (8080, 9090, 9100, 3001) is actually listening
7. Hits each HTTP health endpoint and retries up to 3 times
8. Relaunches Chromium kiosk if the process is dead (handles Wayland env vars)
9. Writes a status line to `/run/hanryx-status` and prints a report

If everything is healthy at the end, exits 0. Otherwise exits 1 and the timer will re-attempt in 2 minutes.

---

## Useful commands

| Command                              | What it does                                           |
| ------------------------------------ | ------------------------------------------------------ |
| `hanryx-recover`                     | Full coloured status report                            |
| `hanryx-recover --quiet`             | Single OK/BROKEN line — used by the timer              |
| `hanryx-recover --kiosk-only`        | Skip docker, only fix Chromium                         |
| `cat /run/hanryx-status`             | Last status line written by the heal timer             |
| `tail -f /var/log/hanryx-recover.log` | Live heal log                                          |
| `systemctl list-timers \| grep hanryx` | When the next heal sweep runs                          |
| `systemctl status hanryx-heal`       | Last heal run's exit status                            |

---

## If `hanryx-recover` itself reports issues

The status line on the screen / `/run/hanryx-status` will tell you which check failed. Common ones:

- **"port 8080 (POS) NOT listening"** → main POS container failed to start. Check `docker logs hanryxvault` for the real error.
- **"tailscale not authenticated"** → run `sudo tailscale up` once and approve in the admin UI.
- **"gateway X.X.X.X unreachable"** → WiFi didn't associate. Try `nmcli device wifi list` then `nmcli device wifi connect "SSID" password "..."`.
- **"chromium kiosk not running"** → already self-recovers; if it persists, log into the desktop session and check `journalctl --user -u graphical-session.target`.
