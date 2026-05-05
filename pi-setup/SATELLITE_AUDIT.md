# Satellite Pi — Code Audit & Improvement Plan

A deep-dive review of `pi-setup/setup-satellite-kiosk-boot.sh` (901 lines, current as of commit `0e5ac3f`+) plus the launcher it generates at `~/.hanryx-dual-monitor.sh`. Findings are ordered by impact.

---

## CRITICAL — likely root causes of the "glitchy" feel

### #1 Hostname collision: satellite Pi is named `hanryxvault` (same as main)

**Where**: `setup-satellite-kiosk-boot.sh` line 278

```bash
hostnamectl set-hostname hanryxvault 2>/dev/null || ...
```

The satellite ends up advertising itself as `hanryxvault.local` on the LAN — exactly the same as the main Pi. Result:

- mDNS confused — `hanryxvault.local` resolves to whichever Pi answered first
- Tailscale picks one of them as the canonical name; the other gets renamed to `hanryxvault-1`, then `hanryxvault-2`, etc.
- Your earlier "I can't find the satellite Pi" episode was probably a side-effect of this
- ARP/ssh known_hosts get stale after every reboot

**Fix**: hostname should be `hanryxvault-sat`. mDNS/avahi alias for `hanryxvault.local` should NOT include the satellite — only the main Pi answers that name.

### #2 Output detection breaks the moment the small screen reports unusual modes

**Where**: lines 502–518 (parser) and 530–547 (assignment)

The launcher parses `wlr-randr` output, extracts pixel widths, then assigns admin to widest, kiosk to narrowest. Failure modes I see:

- The regex on line 510 (`^[A-Za-z][A-Za-z0-9_-]+`) catches output names like `HDMI-A-1` BUT will silently miss outputs whose first line includes a quoted EDID name like `HDMI-A-2 "WaveShare 5inch HDMI"` if the bash version differs.
- The regex on line 512 (`^[[:space:]]+([0-9]+)x([0-9]+)[[:space:]]+px`) requires the literal token `px` — a labwc/wlroots version that prints `1280x800@60.000` instead of `1280x800 px` slips through silently. Result: `OUT_W[$CUR]` is never set for that output.
- If only one output's width is captured, the script falls into the `"${#OUT_W[@]}" -eq 1` branch (line 548) and **both windows land on the same screen** — exactly the "I see the wrong thing" symptom.
- When two outputs DO get parsed but report identical widths, it falls back to alphabetic sort (line 538), which is non-deterministic on labwc cold-boot vs warm-boot.

**Fix**: identify by HDMI connector name (HDMI-A-1 vs HDMI-A-2) when sizes are not reliable, and let the user pin assignments with `ADMIN_OUTPUT=` / `KIOSK_OUTPUT=` in `satellite.conf`. The override path already exists (line 526) — it just needs to be discoverable via a tool.

### #3 labwc `rc.xml` schema is incorrect

**Where**: lines 559–571

```xml
<labwc_config>
  <windowRules>
    <windowRule identifier="hvault-admin" matchType="exact">
      <action name="MoveToOutput"><output>...</output></action>
```

labwc uses an Openbox-derived schema. The root element should be `<openbox_config>`, and modern labwc accepts `<windowRule>` with `app_id` matching for Wayland surfaces. With the wrong root element, labwc may silently ignore the rule, leaving both windows wherever the compositor places them by default — usually both on the same monitor.

**Fix**: emit the correct schema and match on `app_id` for native Wayland or `WM_CLASS` for XWayland.

### #4 Confused Wayland vs XWayland mode

**Where**: lines 652–653 set `--ozone-platform=wayland`, while line 631 also sets `DISPLAY=:0` for XWayland.

The chromium binary attempts native Wayland first; if it succeeds, the labwc match on `app_id` would need to be the chromium `--class` value. If chromium falls back to XWayland, the same `--class` value becomes the X11 `WM_CLASS`. The window rule has to match both modes consistently. With #3 broken, neither matches.

**Fix**: pick one and stick to it. For Pi OS Bookworm Chromium I recommend XWayland (more reliable hardware-decode on Pi 5) with explicit `--ozone-platform=x11`, and use `WM_CLASS` matching in the rules.

---

## HIGH — reliability and observability

### #5 No way to swap screens at runtime

To flip admin/kiosk you have to re-run the 900-line installer. Operationally painful at a venue.

**Fix**: small `satellite-screens.sh` tool that edits `satellite.conf` and reloads the launcher.

### #6 Network watchdog nukes BOTH chromium windows on recovery

**Where**: lines 826–848. When the main Pi comes back online after an outage, the watchdog `pkill -f chromium` — both windows die simultaneously, then restart from splash. Customer-facing kiosk goes black for several seconds during a paying transaction window.

**Fix**: only kill the window that's actually showing an error, OR send a refresh signal instead of killing the process.

### #7 No hotplug handling

If a screen unplugs/replugs during operation (loose HDMI cable), nothing reassigns windows. They stay on whatever output they were on at launch — possibly an output that no longer exists, leaving them invisible.

**Fix**: udev rule that runs `labwcctl --reconfigure` when an HDMI hotplug fires, plus a wlr-randr poll loop in the launcher.

### #8 Backoff resets to 5s on every non-crash exit

**Where**: lines 731–736. If the kiosk URL is intermittently broken, chromium starts and exits cleanly after >=4s every time → no exponential backoff → constant respawn loop chewing CPU.

**Fix**: only reset backoff when the child process is alive AND has been alive for >60s.

---

## MEDIUM — robustness improvements

### #9 `--no-process-singleton` + manual `rm Singleton*` is fragile

**Where**: lines 651, 662. Bypassing chromium's profile-lock safety check can corrupt the profile (cookies, localStorage, IndexedDB). The fact that the script needs to disable the safety net is a sign the launcher itself is being started multiple times. The flock at line 410 helps but the LXDE/labwc autostart cleanup at line 791 is best-effort.

**Fix**: keep flock, drop `--no-process-singleton`, and trust the existing single-instance guard.

### #10 Heartbeat parses tailscale status with inline Python

**Where**: lines 762–764. The heartbeat loop pipes JSON through a `python3 -c` one-liner. If python3 isn't on PATH at runtime (rare, but possible on a stripped image) the heartbeat silently reports `unknown`.

**Fix**: use `tailscale status --peers=false --self=true` text format and grep, no python.

### #11 Splash hardcodes HEALTH_URL at script-write time

**Where**: line 605. If you change `MAIN_PI_TS_HOST` later in `satellite.conf`, the splash still polls the old URL until you re-run install.

**Fix**: make the splash fetch its target URL from a tiny endpoint (`file:///tmp/hvault-config.json`) that the launcher rewrites on startup.

### #12 `quiet` in `config.txt` is a kernel cmdline parameter, not config.txt

**Where**: line 350. The `quiet` token does nothing in `config.txt` — it belongs in `cmdline.txt`, which the script handles separately at line 362. Cosmetic but misleading.

### #13 Both Chromium profiles share `~/.hanryx/` parent directory

**Where**: lines 487–488. They're separate subdirs, but a stray `chmod -R` or backup-restore would clobber both. Minor isolation issue.

### #14 Heartbeat endpoint `/satellite/heartbeat` may not exist on main Pi

**Where**: line 765. The launcher posts heartbeats to a URL that the audit can't confirm exists in the main Pi's `server.py`. Result: every 60s, a useless POST + log line.

**Fix**: confirm the route exists; if not, either add it on the main Pi or stop posting.

### #15 SSH disabling can be a foot-gun

**Where**: line 813 enables SSH unconditionally — good, but no key-only enforcement. Trade-show WiFi networks can be hostile; password-only SSH on a Pi advertised via mDNS as `hanryxvault.local` is a soft target.

**Fix**: harden `sshd_config` with `PasswordAuthentication no` after the user installs an authorized_keys, and document the workflow.

---

## What I'm shipping with this audit

Three new tools, all idempotent, designed to **diagnose** before they **change** anything:

| Tool                         | Purpose                                                       |
| ---------------------------- | ------------------------------------------------------------- |
| `satellite-doctor.sh`        | Read-only diagnostic — dumps wlr-randr, autostart paths, log tail, current assignment, and prints recommendations |
| `satellite-screens.sh`       | Swap which output shows admin vs kiosk at runtime, no reinstall |
| `setup-satellite-hostname-fix.sh` | Renames satellite to `hanryxvault-sat`, fixes mDNS, updates Tailscale (issue #1) |

Run `satellite-doctor.sh` first — it'll tell you which of the 15 issues above are actually biting you, and which are theoretical. Then we fix only what matters.

---

## Priority recommendation for the next session

1. Run `satellite-doctor.sh` and paste the output
2. Apply `setup-satellite-hostname-fix.sh` (5 minutes — fixes the discoverability issue once and for all)
3. Use `satellite-screens.sh` to pin the 10.1" → admin and 5" → kiosk
4. Decide whether to do the bigger launcher rewrite (issues #2, #3, #4, #6, #7) — that's a 200–300 line surgical edit to `setup-satellite-kiosk-boot.sh` we can do in one push if the doctor confirms those are the live problems
