#!/usr/bin/env python3
"""
HanryxVault Monitor  — Desktop GUI
Works on Windows, Linux, and Raspberry Pi.

On Windows / remote machine:
    python desktop_monitor.py
    (or run the HanryxVaultMonitor.exe built by build_exe.bat)

On Raspberry Pi (local):
    python3 desktop_monitor.py

Kiosk / auto-start mode (Pi with a dedicated monitor, no desktop):
    python3 desktop_monitor.py --kiosk

    --kiosk   Full-screen, cursor hidden, auto-connects to localhost,
              title bar removed.  Press Ctrl+Alt+Q or F11 to exit.

The monitor talks to the Pi POS server over HTTP — no direct
database or filesystem access is required.  Configure the Pi's
IP/hostname in the Settings tab on first launch.

Dependencies (pip install -r monitor_requirements.txt):
    psutil >= 5.9.0
"""

import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox, simpledialog
import threading
import subprocess
import datetime
import os
import sys
import json
import urllib.request
import urllib.error
import time
import platform
import webbrowser
import math
from collections import deque

# ── Try psutil (cross-platform system stats) ──────────────────────────────────
try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False

# ── Platform detection ────────────────────────────────────────────────────────
IS_WINDOWS = platform.system() == "Windows"
IS_LINUX   = platform.system() == "Linux"
IS_PI      = IS_LINUX and os.path.exists("/sys/class/thermal/thermal_zone0/temp")

# ── Persistent config (~/.hanryxvault_monitor.json) ───────────────────────────
CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".hanryxvault_monitor.json")
DEFAULT_CFG = {
    "host":       "127.0.0.1",
    "port":       "8080",
    "admin_pass": "",
}


def load_config():
    try:
        with open(CONFIG_PATH) as f:
            c = json.load(f)
        for k, v in DEFAULT_CFG.items():
            c.setdefault(k, v)
        return c
    except Exception:
        return dict(DEFAULT_CFG)


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w") as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        pass


CFG = load_config()


def server_url():
    return f"http://{CFG['host']}:{CFG['port']}"


# ── Colours ───────────────────────────────────────────────────────────────────
GOLD   = "#FFD700"
BG     = "#0d0d0d"
BG2    = "#141414"
BG3    = "#1a1a1a"
BORDER = "#2a2a2a"
GREEN  = "#4caf50"
RED    = "#f44336"
ORANGE = "#ff9800"
BLUE   = "#2196F3"
GREY   = "#666666"
WHITE  = "#e0e0e0"

REFRESH_MS = 4000   # UI refresh every 4 s
LOG_LINES  = 80

WEBSITES = [
    ("hanryxvault.cards", "https://hanryxvault.cards"),
    ("hanryxvault.app",   "https://hanryxvault.app"),
]

SERVICES = [
    ("POS Server",  f"http://{{host}}:{{port}}/health"),
    ("Admin Panel", f"http://{{host}}:{{port}}/admin"),
    ("nginx",       f"http://{{host}}:8080/"),
    ("Storefront",  f"http://{{host}}:8080/store/"),
]


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def _get(path, timeout=4):
    """GET request; returns (status_code, body_dict_or_str, latency_ms)."""
    url = f"{server_url()}{path}"
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "HanryxVault-Monitor/2.0"}
        )
        start = time.time()
        with urllib.request.urlopen(req, timeout=timeout) as r:
            ms   = int((time.time() - start) * 1000)
            body = r.read().decode("utf-8", errors="replace")
            try:
                return r.status, json.loads(body), ms
            except Exception:
                return r.status, body, ms
    except urllib.error.HTTPError as e:
        return e.code, {}, 0
    except Exception:
        return 0, {}, 0


def _post(path, data=None, timeout=10):
    payload = json.dumps(data or {}).encode()
    url = f"{server_url()}{path}"
    try:
        req = urllib.request.Request(
            url, data=payload, method="POST",
            headers={"Content-Type": "application/json",
                     "User-Agent":   "HanryxVault-Monitor/2.0"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", errors="replace")
            try:
                return r.status, json.loads(body)
            except Exception:
                return r.status, body
    except Exception as e:
        return 0, str(e)


def ping_url(url, timeout=4):
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "HanryxVault-Monitor/2.0"}
        )
        start = time.time()
        with urllib.request.urlopen(req, timeout=timeout) as r:
            ms = int((time.time() - start) * 1000)
            return r.status, ms
    except Exception:
        return 0, 0


# ── System stats (psutil preferred, shell fallback on Linux) ──────────────────

def sys_cpu():
    if HAS_PSUTIL:
        return psutil.cpu_percent(interval=0.3)
    if IS_LINUX:
        try:
            line = subprocess.check_output(
                "top -bn1 | grep 'Cpu(s)'", shell=True,
                stderr=subprocess.DEVNULL, timeout=3
            ).decode()
            idle = float(line.split(',')[3].split()[0])
            return round(100 - idle, 1)
        except Exception:
            pass
    return 0.0


def sys_ram():
    """Returns (used_mb, total_mb, pct)."""
    if HAS_PSUTIL:
        v = psutil.virtual_memory()
        return round(v.used / 1024**2), round(v.total / 1024**2), v.percent
    if IS_LINUX:
        try:
            out = subprocess.check_output(
                "free -m | grep Mem", shell=True,
                stderr=subprocess.DEVNULL, timeout=3
            ).decode().split()
            total, used = int(out[1]), int(out[2])
            return used, total, round(used / total * 100, 1)
        except Exception:
            pass
    return 0, 0, 0.0


def sys_disk(path="/mnt/cards"):
    """Returns (used_str, total_str, pct_float) for `path`.

    Defaults to the external UGreen dock at /mnt/cards (the working
    storage for the cards library). The Pi 5's own SD card / root
    partition is intentionally NOT surfaced on the Diagnostics tab —
    we only care about the external array's headroom.
    """
    if HAS_PSUTIL:
        try:
            d = psutil.disk_usage(path)
            return (
                f"{d.used / 1024**3:.1f}G",
                f"{d.total / 1024**3:.1f}G",
                d.percent,
            )
        except Exception:
            pass
    if IS_LINUX:
        try:
            out = subprocess.check_output(
                f"df -h {path} | tail -1", shell=True,
                stderr=subprocess.DEVNULL, timeout=3
            ).decode().split()
            return out[2], out[1], float(out[4].rstrip("%"))
        except Exception:
            pass
    return "?", "?", 0.0


def sys_cpu_temp():
    """Degrees C, best-effort."""
    if HAS_PSUTIL:
        try:
            temps = psutil.sensors_temperatures()
            for key in ("cpu_thermal", "coretemp", "k10temp", "acpitz"):
                if key in temps and temps[key]:
                    return temps[key][0].current
        except Exception:
            pass
    if IS_PI:
        try:
            with open("/sys/class/thermal/thermal_zone0/temp") as f:
                return float(f.read().strip()) / 1000
        except Exception:
            pass
    return None   # unavailable on Windows / non-Pi


def run_shell(cmd):
    try:
        return subprocess.check_output(
            cmd, shell=True, stderr=subprocess.DEVNULL, timeout=4
        ).decode().strip()
    except Exception:
        return ""


# ── Extended Pi / system diagnostics (best-effort, all degrade gracefully) ───

#  Only the external UGreen dock is surfaced on the Diagnostics tab —
#  the SD card root (/) and /boot/firmware are intentionally hidden
#  because the operator only cares about the cards-library array's
#  headroom, not the OS partition.
MOUNTS_TO_WATCH = ["/mnt/cards"]
NET_IFACES_SKIP = {"lo"}


def pi_hardware():
    """Pi model, revision, serial, kernel — all best-effort."""
    out = {"model": "", "revision": "", "serial": "", "kernel": ""}
    try:
        with open("/proc/device-tree/model", "rb") as f:
            out["model"] = f.read().decode("utf-8", errors="ignore").strip("\x00").strip()
    except Exception:
        pass
    try:
        with open("/proc/cpuinfo") as f:
            for ln in f:
                if ln.startswith("Revision"):
                    out["revision"] = ln.split(":", 1)[1].strip()
                elif ln.startswith("Serial"):
                    out["serial"] = ln.split(":", 1)[1].strip()
    except Exception:
        pass
    out["kernel"] = run_shell("uname -r")
    return out


def pi_throttled():
    """Decode vcgencmd get_throttled bits into human-readable status."""
    out = {"raw": "", "status": "n/a", "ok": True, "past": False}
    raw = run_shell("vcgencmd get_throttled 2>/dev/null")
    if not raw or "=" not in raw:
        return out
    try:
        val = int(raw.split("=", 1)[1], 16)
        out["raw"] = f"0x{val:x}"
        # bits 0-3: NOW; bits 16-19: SINCE BOOT
        now_flags = []
        if val & (1 << 0): now_flags.append("under-volt")
        if val & (1 << 1): now_flags.append("arm-cap")
        if val & (1 << 2): now_flags.append("throttled")
        if val & (1 << 3): now_flags.append("soft-temp")
        past_flags = []
        if val & (1 << 16): past_flags.append("under-volt")
        if val & (1 << 17): past_flags.append("arm-cap")
        if val & (1 << 18): past_flags.append("throttled")
        if val & (1 << 19): past_flags.append("soft-temp")
        if now_flags:
            out["status"] = "NOW: " + ", ".join(now_flags)
            out["ok"] = False
        elif past_flags:
            out["status"] = "Past: " + ", ".join(past_flags)
            out["past"] = True
        else:
            out["status"] = "OK — no throttling"
    except Exception:
        pass
    return out


def pi_voltages():
    rails = {}
    for k in ("core", "sdram_c", "sdram_i", "sdram_p"):
        v = run_shell(f"vcgencmd measure_volts {k} 2>/dev/null")
        if v and "=" in v:
            rails[k] = v.split("=", 1)[1].rstrip("V'\"")
    return rails


def pi_clocks():
    """MHz for arm/core/v3d/uart/emmc."""
    out = {}
    for k in ("arm", "core", "v3d", "emmc"):
        v = run_shell(f"vcgencmd measure_clock {k} 2>/dev/null")
        if v and "=" in v:
            try:
                out[k] = int(v.split("=", 1)[1]) // 1_000_000
            except Exception:
                pass
    return out


def pi_pmic_temp():
    """PMIC temp on Pi 5 (separate from CPU temp)."""
    v = run_shell("vcgencmd measure_temp pmic 2>/dev/null")
    if v and "=" in v:
        try:
            return float(v.split("=", 1)[1].rstrip("'C\n "))
        except Exception:
            pass
    return None


def cpu_per_core():
    if HAS_PSUTIL:
        try:
            return psutil.cpu_percent(interval=0.2, percpu=True)
        except Exception:
            return []
    return []


def cpu_freq_info():
    out = {"cur": 0, "min": 0, "max": 0, "governor": ""}
    if HAS_PSUTIL:
        try:
            f = psutil.cpu_freq()
            if f:
                out["cur"] = round(f.current or 0)
                out["min"] = round(f.min or 0)
                out["max"] = round(f.max or 0)
        except Exception:
            pass
    try:
        with open("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor") as f:
            out["governor"] = f.read().strip()
    except Exception:
        pass
    return out


def mem_detail():
    out = {"used_mb": 0, "avail_mb": 0, "total_mb": 0, "pct": 0.0,
           "cached_mb": 0, "buffers_mb": 0,
           "swap_used_mb": 0, "swap_total_mb": 0, "swap_pct": 0.0}
    if HAS_PSUTIL:
        try:
            v = psutil.virtual_memory()
            out["used_mb"]    = round(v.used      / 1024**2)
            out["avail_mb"]   = round(v.available / 1024**2)
            out["total_mb"]   = round(v.total     / 1024**2)
            out["pct"]        = v.percent
            out["cached_mb"]  = round(getattr(v, "cached", 0)  / 1024**2)
            out["buffers_mb"] = round(getattr(v, "buffers", 0) / 1024**2)
            s = psutil.swap_memory()
            out["swap_used_mb"]  = round(s.used  / 1024**2)
            out["swap_total_mb"] = round(s.total / 1024**2)
            out["swap_pct"]      = s.percent
        except Exception:
            pass
    return out


def disks_per_mount(mounts=MOUNTS_TO_WATCH):
    out = []
    for m in mounts:
        if not os.path.exists(m):
            continue
        if HAS_PSUTIL:
            try:
                d = psutil.disk_usage(m)
                out.append({
                    "mount":    m,
                    "used_gb":  d.used  / 1024**3,
                    "total_gb": d.total / 1024**3,
                    "pct":      d.percent,
                })
            except Exception:
                pass
    return out


def net_io_per_iface():
    """Snapshot bytes_sent / bytes_recv per interface (filters lo)."""
    out = {}
    if HAS_PSUTIL:
        try:
            for name, c in psutil.net_io_counters(pernic=True).items():
                if name in NET_IFACES_SKIP:
                    continue
                out[name] = (c.bytes_sent, c.bytes_recv)
        except Exception:
            pass
    return out


def disk_io_total():
    """(read_bytes, write_bytes) snapshot."""
    if HAS_PSUTIL:
        try:
            c = psutil.disk_io_counters()
            if c:
                return (c.read_bytes, c.write_bytes)
        except Exception:
            pass
    return (0, 0)


def top_processes(n=5):
    """Top N by CPU and by RAM.

    Uses warmup-then-sample so CPU% reflects the last ~0.5 s, not the
    cumulative-since-process-start value. Without this, the first call
    (and all subsequent first-time-seen processes) would report 0 % CPU.
    """
    out = {"by_cpu": [], "by_ram": []}
    if not HAS_PSUTIL:
        return out
    proc_objs = []
    try:
        # Pass 1 — gather Process objects and prime per-process baseline
        for p in psutil.process_iter(["pid"]):
            try:
                p.cpu_percent(None)   # establish baseline; first call returns 0
                proc_objs.append(p)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            except Exception:
                continue
        time.sleep(0.5)
        # Pass 2 — measure CPU% delta against baseline + collect names/RAM
        procs = []
        for p in proc_objs:
            try:
                cpu  = p.cpu_percent(None)
                name = (p.name() or "?")[:24]
                ram  = p.memory_percent()
                procs.append({"pid": p.pid, "name": name, "cpu": cpu, "ram": ram})
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            except Exception:
                continue
        out["by_cpu"] = sorted(procs, key=lambda x: x["cpu"], reverse=True)[:n]
        out["by_ram"] = sorted(procs, key=lambda x: x["ram"], reverse=True)[:n]
    except Exception:
        pass
    return out


def docker_summary():
    out = {"running": 0, "stopped": 0, "unhealthy": 0, "lines": []}
    raw = run_shell(
        "docker ps -a --format '{{.Names}}|{{.State}}|{{.Status}}' 2>/dev/null"
    )
    if not raw:
        return out
    for line in raw.splitlines():
        parts = line.split("|", 2)
        if len(parts) < 3:
            continue
        name, state, status = parts
        unhealthy = (state == "running" and "unhealthy" in status.lower())
        if state == "running":
            out["running"] += 1
            if unhealthy:
                out["unhealthy"] += 1
        else:
            out["stopped"] += 1
        out["lines"].append((name, state, status, unhealthy))
    return out


def load_avg_str():
    try:
        a, b, c = os.getloadavg()
        return f"{a:.2f}  {b:.2f}  {c:.2f}"
    except Exception:
        return "—"


def uptime_short():
    raw = run_shell("uptime -p")
    return raw.replace("up ", "") if raw else "—"


def tailscale_ip():
    return run_shell("tailscale ip -4 2>/dev/null | head -1") or ""


def fmt_rate(bytes_per_sec):
    """Human-friendly KiB/MiB/s."""
    if bytes_per_sec < 0:
        return "—"
    if bytes_per_sec >= 1024 * 1024:
        return f"{bytes_per_sec / 1024**2:6.2f} MiB/s"
    if bytes_per_sec >= 1024:
        return f"{bytes_per_sec / 1024:6.1f} KiB/s"
    return f"{bytes_per_sec:6.0f}  B/s"


# ── Diagnostics widgets (ring gauges / sparklines / LED dots) ────────────────
#
# All built on plain tkinter Canvas — no matplotlib / PIL dependency, so the
# kiosk can run with just `pip install psutil` like before.

# Enterprise-rack aesthetic
RACK_RAIL_OK   = "#22c55e"
RACK_RAIL_WARN = "#f59e0b"
RACK_RAIL_ERR  = "#ef4444"
RACK_BLADE     = "#141414"
RACK_BLADE_HDR = "#1c1c1c"
RACK_BORDER    = "#2a2a2a"
RING_TRACK     = "#262626"   # background ring track


class RingGauge(tk.Canvas):
    """Circular ring/donut gauge with a centered numeric readout.

    The arc sweeps clockwise from 12 o'clock proportional to value/max_val.
    Use .set_value(v, color=…) on every refresh; redraw is cheap (<1 ms).
    """

    def __init__(self, parent, size=130, label="", unit="%",
                 max_val=100, fg=GREEN, bg=BG3, **kw):
        super().__init__(parent, width=size, height=size,
                         bg=bg, highlightthickness=0, borderwidth=0, **kw)
        self.size     = size
        self.label    = label
        self.unit     = unit
        self.max_val  = float(max_val)
        self.fg       = fg
        self.bg_color = bg
        self._value   = 0.0
        self._draw()

    def set_value(self, value, color=None, label_override=None):
        try:
            self._value = float(value)
        except (TypeError, ValueError):
            self._value = 0.0
        if color is not None:
            self.fg = color
        if label_override is not None:
            self.label = label_override
        self._draw()

    def _draw(self):
        self.delete("all")
        s     = self.size
        pad   = 10
        thick = 11

        # Outer track ring
        self.create_arc(pad, pad, s - pad, s - pad,
                        start=0, extent=359.999,
                        outline=RING_TRACK, width=thick, style="arc")

        # Active arc — clockwise from 12 o'clock (start=90, negative extent)
        if self.max_val > 0:
            frac = max(0.0, min(1.0, self._value / self.max_val))
            ext  = -frac * 359.999
            if abs(ext) > 0.5:
                self.create_arc(pad, pad, s - pad, s - pad,
                                start=90, extent=ext,
                                outline=self.fg, width=thick, style="arc")

        # Center number + unit
        if self.max_val == 100:
            num_txt = f"{self._value:.0f}"
        else:
            num_txt = f"{self._value:.1f}"
        self.create_text(s / 2, s / 2 - 6, text=num_txt,
                         font=("Helvetica", 22, "bold"), fill=self.fg)
        self.create_text(s / 2, s / 2 + 16, text=self.unit,
                         font=("Helvetica", 10), fill=GREY)

        # Bottom caption
        self.create_text(s / 2, s - 8, text=self.label.upper(),
                         font=("Helvetica", 9, "bold"), fill=GREY)


class Sparkline(tk.Canvas):
    """Mini rolling time-series line graph.

    Keeps the most recent `max_samples` numeric samples and re-draws on every
    add_sample() call. Auto-scales to the max value seen in the current window
    (with 10 % headroom). Newest sample is always at the right edge.
    """

    def __init__(self, parent, width=260, height=66, label="",
                 max_samples=60, fg=BLUE, fill_color=None,
                 unit="", bg=BG3, **kw):
        super().__init__(parent, width=width, height=height,
                         bg=bg, highlightthickness=0, borderwidth=0, **kw)
        self.w           = width
        self.h           = height
        self.label       = label
        self.max_samples = max_samples
        self.fg          = fg
        self.fill_color  = fill_color
        self.unit        = unit
        self._samples    = deque(maxlen=max_samples)
        self._draw()

    def add_sample(self, val):
        try:
            self._samples.append(float(val))
        except (TypeError, ValueError):
            return
        self._draw()

    def _draw(self):
        self.delete("all")
        # Top label + current/max readouts
        cur = self._samples[-1] if self._samples else 0.0
        mx  = max(self._samples) if self._samples else 0.0
        self.create_text(8, 11, text=self.label.upper(),
                         font=("Courier", 9, "bold"),
                         fill=GREY, anchor="w")
        self.create_text(self.w - 8, 11,
                         text=f"{cur:.1f}{self.unit}",
                         font=("Courier", 10, "bold"),
                         fill=self.fg, anchor="e")
        self.create_text(self.w - 8, 24,
                         text=f"max {mx:.1f}",
                         font=("Courier", 8),
                         fill=GREY, anchor="e")

        # Plot region
        if len(self._samples) < 2:
            return
        top  = 28
        bot  = self.h - 4
        plot = bot - top
        scale_max = max(max(self._samples), 1.0) * 1.1
        n    = len(self._samples)
        step = (self.w - 16) / max(1, self.max_samples - 1)

        coords = []
        for i, v in enumerate(self._samples):
            x = 8 + (self.max_samples - n + i) * step
            y = bot - (v / scale_max) * plot
            coords.extend([x, y])

        if self.fill_color:
            poly = list(coords) + [coords[-2], bot, coords[0], bot]
            self.create_polygon(poly, fill=self.fill_color, outline="")

        self.create_line(coords, fill=self.fg, width=1.5, smooth=False)


def make_led(parent, color=GREEN, size=10, bg=BG3):
    """Filled-circle status LED. Returned canvas has .set_color(c)."""
    pad = 2
    c = tk.Canvas(parent, width=size + pad * 2, height=size + pad * 2,
                  bg=bg, highlightthickness=0, borderwidth=0)
    oval = c.create_oval(pad, pad, size + pad, size + pad,
                         fill=color, outline="")

    def set_color(col):
        c.itemconfigure(oval, fill=col)

    c.set_color = set_color
    return c


# ── Main application ──────────────────────────────────────────────────────────

class HanryxMonitor(tk.Tk):

    def __init__(self, kiosk: bool = False):
        super().__init__()
        self._kiosk = kiosk

        self.title("HanryxVault Monitor")
        self.configure(bg=BG)

        if kiosk:
            # ── Kiosk mode: fill the entire screen, no title bar, hidden cursor ──
            # Auto-connect to localhost so no Settings dialog is needed on boot
            CFG["host"] = "127.0.0.1"
            CFG["port"] = "8080"
            save_config(CFG)

            self.attributes("-fullscreen", True)
            self.config(cursor="none")
            self.resizable(False, False)

            # Allow graceful exit from the kiosk without a mouse
            self.bind("<F11>",                    lambda e: self.destroy())
            self.bind("<Control-Alt-KeyPress-q>", lambda e: self.destroy())
            self.bind("<Control-Alt-KeyPress-Q>", lambda e: self.destroy())
        else:
            self.geometry("1260x820")
            self.minsize(960, 640)

        # Delta-state for rate computations in extended diagnostics
        self._last_disk_io     = (0, 0)
        self._last_disk_io_ts  = 0.0
        self._last_net_io      = {}
        self._last_net_io_ts   = 0.0
        # Re-entry lock: skip a poll tick if the previous _bg_refresh_extras
        # is still running (vcgencmd + docker shell-outs can occasionally
        # exceed REFRESH_MS, which would otherwise cause overlapping threads
        # to clobber the shared rate-delta state). A real Lock with
        # non-blocking acquire is used (a plain bool check-then-set is not
        # atomic across CPython threads even with the GIL).
        self._extras_lock      = threading.Lock()

        self._build_ui()
        self._refresh()

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        # Header bar
        hdr = tk.Frame(self, bg=BG, pady=10)
        hdr.pack(fill="x", padx=16)
        tk.Label(hdr, text="HanryxVault", font=("Helvetica", 22, "bold"),
                 fg=GOLD, bg=BG).pack(side="left")
        tk.Label(hdr, text="  Monitor", font=("Helvetica", 16),
                 fg=GREY, bg=BG).pack(side="left")
        self.lbl_host = tk.Label(
            hdr, text=f"  ⇒  {server_url()}", font=("Helvetica", 11),
            fg=GREY, bg=BG)
        self.lbl_host.pack(side="left")
        self.lbl_time = tk.Label(hdr, text="", font=("Helvetica", 12),
                                  fg=GREY, bg=BG)
        self.lbl_time.pack(side="right")

        # Tabs
        style = ttk.Style(self)
        style.theme_use("default")
        style.configure("TNotebook",     background=BG,  borderwidth=0)
        style.configure("TNotebook.Tab", background=BG3, foreground=GREY,
                         padding=[14, 8], font=("Helvetica", 11))
        style.map("TNotebook.Tab",
                  background=[("selected", BG2)],
                  foreground=[("selected", GOLD)])
        style.configure("TFrame", background=BG)

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=8, pady=4)

        self.tab_dash     = ttk.Frame(nb)
        self.tab_business = ttk.Frame(nb)
        self.tab_system   = ttk.Frame(nb)
        self.tab_sites    = ttk.Frame(nb)
        self.tab_logs     = ttk.Frame(nb)
        self.tab_settings = ttk.Frame(nb)

        nb.add(self.tab_dash,     text="  Dashboard  ")
        nb.add(self.tab_business, text="  Business   ")
        nb.add(self.tab_system,   text="  Diagnostics ")
        nb.add(self.tab_sites,    text="  Sites      ")
        nb.add(self.tab_logs,     text="  Logs       ")
        nb.add(self.tab_settings, text="  Settings   ")

        self._build_dashboard()
        self._build_business()
        self._build_system()
        self._build_sites()
        self._build_logs()
        self._build_settings()
        self._build_actions()

    # ── Reusable widgets ──────────────────────────────────────────────────────

    def _card(self, parent, row, col, label, value="—", color=GOLD,
              colspan=1, rowspan=1, font_size=26):
        f = tk.Frame(parent, bg=BG3, padx=14, pady=10,
                     highlightbackground=BORDER, highlightthickness=1)
        f.grid(row=row, column=col, columnspan=colspan, rowspan=rowspan,
               sticky="nsew", padx=5, pady=5)
        tk.Label(f, text=label.upper(), font=("Helvetica", 9),
                 fg=GREY, bg=BG3, anchor="w").pack(anchor="w")
        lbl = tk.Label(f, text=value,
                        font=("Helvetica", font_size, "bold"),
                        fg=color, bg=BG3, anchor="w")
        lbl.pack(anchor="w", pady=(2, 0))
        return lbl

    def _section(self, parent, title):
        f = tk.Frame(parent, bg=BG3,
                     highlightbackground=BORDER, highlightthickness=1)
        f.pack(fill="x", padx=14, pady=5)
        tk.Label(f, text=title, font=("Helvetica", 9),
                 fg=GREY, bg=BG3, padx=12, pady=6).pack(anchor="w")
        return f

    # ── Dashboard tab ─────────────────────────────────────────────────────────

    def _build_dashboard(self):
        p = self.tab_dash

        # Row 1: sales KPI cards
        sf = tk.Frame(p, bg=BG)
        sf.pack(fill="x", padx=8, pady=6)
        for i in range(6):
            sf.columnconfigure(i, weight=1)

        self.c_today_sales  = self._card(sf, 0, 0, "Today's Sales",  "—", GREEN)
        self.c_today_rev    = self._card(sf, 0, 1, "Revenue Today",  "—", GOLD)
        self.c_today_tips   = self._card(sf, 0, 2, "Tips Today",     "—", GOLD)
        self.c_total_sales  = self._card(sf, 0, 3, "All-Time Sales", "—", WHITE)
        self.c_inv_count    = self._card(sf, 0, 4, "Products",       "—", WHITE)
        self.c_pending      = self._card(sf, 0, 5, "Pending Scans",  "—", ORANGE)

        # Stock alerts
        af = self._section(p, "STOCK ALERTS")
        row2 = tk.Frame(af, bg=BG3)
        row2.pack(anchor="w", padx=12, pady=(0, 8))
        self.lbl_low_stock = tk.Label(row2, text="Low (≤5): —",
                                       font=("Helvetica", 13), fg=ORANGE, bg=BG3)
        self.lbl_low_stock.pack(side="left", padx=(0, 24))
        self.lbl_out_stock = tk.Label(row2, text="Out of stock: —",
                                       font=("Helvetica", 13), fg=RED, bg=BG3)
        self.lbl_out_stock.pack(side="left")

        # Services
        svc_frame = self._section(p, "SERVICE HEALTH")
        self.svc_labels = {}
        for i, (name, _url_tpl) in enumerate(SERVICES):
            row_f = tk.Frame(svc_frame, bg=BG3)
            row_f.pack(fill="x", padx=12, pady=3)
            tk.Label(row_f, text=name, font=("Helvetica", 12),
                     fg=WHITE, bg=BG3, width=18, anchor="w").pack(side="left")
            dot = tk.Label(row_f, text="●", font=("Helvetica", 14),
                           fg=GREY, bg=BG3)
            dot.pack(side="left", padx=6)
            lbl = tk.Label(row_f, text="checking…", font=("Helvetica", 11),
                           fg=GREY, bg=BG3, width=22, anchor="w")
            lbl.pack(side="left")
            self.svc_labels[name] = (dot, lbl)

        # Server ping
        ping_f = self._section(p, "SERVER PING")
        self.lbl_ping = tk.Label(ping_f, text="—",
                                  font=("Helvetica", 13), fg=GREEN, bg=BG3)
        self.lbl_ping.pack(anchor="w", padx=12, pady=(0, 8))

    # ── Business tab ──────────────────────────────────────────────────────────

    def _build_business(self):
        p = self.tab_business

        # Row 1 — operational counts
        r1 = tk.Frame(p, bg=BG)
        r1.pack(fill="x", padx=8, pady=6)
        for i in range(4):
            r1.columnconfigure(i, weight=1)

        self.c_open_laybys   = self._card(r1, 0, 0, "Open Laybys",    "—", BLUE)
        self.c_layby_bal     = self._card(r1, 0, 1, "Layby Outstanding", "—", BLUE)
        self.c_open_pos      = self._card(r1, 0, 2, "Open POs",        "—", ORANGE)
        self.c_open_trade_in = self._card(r1, 0, 3, "Open Trade-Ins",  "—", ORANGE)

        # Row 2 — 30-day P&L
        r2 = tk.Frame(p, bg=BG)
        r2.pack(fill="x", padx=8, pady=4)
        for i in range(4):
            r2.columnconfigure(i, weight=1)

        self.c_pl_rev    = self._card(r2, 0, 0, "30d Revenue",   "—", GOLD)
        self.c_pl_profit = self._card(r2, 0, 1, "30d Profit",    "—", GREEN)
        self.c_pl_margin = self._card(r2, 0, 2, "30d Margin",    "—", GREEN)
        self.c_eod       = self._card(r2, 0, 3, "EOD Today",     "—", GREY)

        # Quick-links panel
        ql = self._section(p, "QUICK LINKS")
        btn_row = tk.Frame(ql, bg=BG3)
        btn_row.pack(fill="x", padx=12, pady=(0, 10))

        def qbtn(label, path, color=BG2):
            def go():
                webbrowser.open(f"{server_url()}{path}")
            tk.Button(btn_row, text=label, command=go,
                      bg=color, fg=GOLD, relief="flat",
                      font=("Helvetica", 11, "bold"),
                      padx=12, pady=6, cursor="hand2"
                      ).pack(side="left", padx=4, pady=4)

        qbtn("📊 P&L Report",        "/admin/profit-loss")
        qbtn("🏷️ Laybys",            "/admin/layby")
        qbtn("🛒 Purchase Orders",   "/admin/purchases")
        qbtn("🔁 Trade-Ins",         "/admin/trade-in")
        qbtn("🏧 End of Day",        "/admin/eod")
        qbtn("📥 Import / Export",   "/admin/csv")

    # ── Diagnostics tab — enterprise rack layout ─────────────────────────────
    #
    # Pi-only system telemetry on a single non-scrolling page. Layout is a
    # vertical stack of "rack-unit" blades (U1–U5), each with a colored LED
    # status rail on the left edge:
    #
    #   ┌─ HEADER strip (host, status LED, uptime, load) ─────────────────┐
    #   │ U1  CORE METRICS         (4 ring gauges)                        │
    #   │ U2  TIME SERIES          (4 sparklines: CPU/RAM/NET/DISK I/O)   │
    #   │ U3  PER-CORE  /  POWER & THERMAL                                │
    #   │ U4  STORAGE ARRAY  /  NETWORK                                   │
    #   │ U5  CONTAINERS  /  TOP PROCESSES                                │
    #   └────────────────────────────────────────────────────────────────────┘

    def _blade(self, parent, slot, title):
        """One rack-unit blade. Returns (content_frame, led_rail)."""
        outer = tk.Frame(parent, bg=BG)
        outer.pack(fill="x", padx=10, pady=2)

        rail = tk.Frame(outer, bg=RACK_RAIL_OK, width=4)
        rail.pack(side="left", fill="y")

        body = tk.Frame(outer, bg=RACK_BLADE,
                        highlightbackground=RACK_BORDER,
                        highlightthickness=1)
        body.pack(side="left", fill="both", expand=True)

        hdr = tk.Frame(body, bg=RACK_BLADE_HDR)
        hdr.pack(fill="x")
        tk.Label(hdr, text=slot, font=("Courier", 10, "bold"),
                 fg=GOLD, bg=RACK_BLADE_HDR, padx=10, pady=3
                 ).pack(side="left")
        tk.Label(hdr, text=title, font=("Courier", 10, "bold"),
                 fg=WHITE, bg=RACK_BLADE_HDR, padx=4, pady=3
                 ).pack(side="left")

        content = tk.Frame(body, bg=RACK_BLADE, padx=10, pady=8)
        content.pack(fill="both", expand=True)
        return content, rail

    def _build_system(self):
        p = self.tab_system

        # Rolling sample buffers fed every 4 s by the refresh threads.
        # 60 samples × 4 s/sample = 4 minutes of recent history per graph.
        self._hist_cpu = deque(maxlen=60)
        self._hist_ram = deque(maxlen=60)
        self._hist_net = deque(maxlen=60)
        self._hist_dio = deque(maxlen=60)

        # ttk style for the slim per-core / per-disk progress bars
        st = ttk.Style(self)
        st.configure("Diag.Horizontal.TProgressbar",
                     background=GREEN, troughcolor=RING_TRACK,
                     bordercolor=BG3, lightcolor=GREEN, darkcolor=GREEN,
                     thickness=10)

        # ── Header status strip ────────────────────────────────────────────
        top = tk.Frame(p, bg=RACK_BLADE_HDR, padx=12, pady=6,
                       highlightbackground=RACK_BORDER, highlightthickness=1)
        top.pack(fill="x", padx=10, pady=(8, 4))
        tk.Label(top, text="◾ HANRYXVAULT", font=("Courier", 12, "bold"),
                 fg=GOLD, bg=RACK_BLADE_HDR).pack(side="left")
        tk.Label(top, text="POS  ·  PI DIAGNOSTICS",
                 font=("Courier", 11),
                 fg=GREY, bg=RACK_BLADE_HDR, padx=10).pack(side="left")

        self.lbl_diag_status = tk.Label(top, text="● SYSTEM OK",
                                        font=("Courier", 11, "bold"),
                                        fg=GREEN, bg=RACK_BLADE_HDR)
        self.lbl_diag_status.pack(side="right", padx=8)
        self.lbl_diag_uptime = tk.Label(top, text="UPTIME —",
                                        font=("Courier", 10),
                                        fg=WHITE, bg=RACK_BLADE_HDR, padx=12)
        self.lbl_diag_uptime.pack(side="right")
        self.lbl_diag_load = tk.Label(top, text="LOAD —",
                                      font=("Courier", 10),
                                      fg=WHITE, bg=RACK_BLADE_HDR, padx=12)
        self.lbl_diag_load.pack(side="right")

        # ── U1 — CORE METRICS (4 ring gauges) ──────────────────────────────
        c1, _ = self._blade(p, "U1", "CORE METRICS")
        g = tk.Frame(c1, bg=RACK_BLADE)
        g.pack(fill="x")
        for i in range(4):
            g.columnconfigure(i, weight=1)
        self.gauge_cpu  = RingGauge(g, size=130, label="CPU",
                                    unit="%", fg=GREEN, bg=RACK_BLADE)
        # CPU TEMP gauge displayed in °F (max scale 185 °F ≈ 85 °C, the
        # Pi 5 thermal-throttle ceiling). Internal threshold logic stays
        # in Celsius — only the readout is converted in _update_ui.
        self.gauge_temp = RingGauge(g, size=130, label="CPU TEMP",
                                    unit="°F", max_val=185,
                                    fg=ORANGE, bg=RACK_BLADE)
        self.gauge_ram  = RingGauge(g, size=130, label="RAM",
                                    unit="%", fg=BLUE, bg=RACK_BLADE)
        # External-dock usage (UGreen at /mnt/cards). Root partition is
        # not displayed — see sys_disk() / MOUNTS_TO_WATCH for rationale.
        self.gauge_disk = RingGauge(g, size=130, label="CARDS DISK",
                                    unit="%", fg=WHITE, bg=RACK_BLADE)
        self.gauge_cpu .grid(row=0, column=0, pady=2)
        self.gauge_temp.grid(row=0, column=1, pady=2)
        self.gauge_ram .grid(row=0, column=2, pady=2)
        self.gauge_disk.grid(row=0, column=3, pady=2)

        # ── U2 — TIME SERIES (4 sparklines) ───────────────────────────────
        c2, _ = self._blade(p, "U2", "TIME SERIES  (60 samples · ~4 min)")
        s = tk.Frame(c2, bg=RACK_BLADE)
        s.pack(fill="x")
        for i in range(4):
            s.columnconfigure(i, weight=1)
        self.spk_cpu  = Sparkline(s, width=260, height=66, label="CPU",
                                  fg=GREEN, fill_color="#0c2010",
                                  unit="%", bg=RACK_BLADE)
        self.spk_ram  = Sparkline(s, width=260, height=66, label="RAM",
                                  fg=BLUE,  fill_color="#0c1828",
                                  unit="%", bg=RACK_BLADE)
        self.spk_net  = Sparkline(s, width=260, height=66, label="NET ↓",
                                  fg=GOLD,  fill_color="#1d1505",
                                  unit="K", bg=RACK_BLADE)
        self.spk_dio  = Sparkline(s, width=260, height=66, label="DISK I/O",
                                  fg=ORANGE, fill_color="#1f1206",
                                  unit="K", bg=RACK_BLADE)
        self.spk_cpu.grid(row=0, column=0, padx=4, pady=4, sticky="ew")
        self.spk_ram.grid(row=0, column=1, padx=4, pady=4, sticky="ew")
        self.spk_net.grid(row=0, column=2, padx=4, pady=4, sticky="ew")
        self.spk_dio.grid(row=0, column=3, padx=4, pady=4, sticky="ew")

        # ── U3 — PER-CORE  /  POWER & THERMAL ─────────────────────────────
        c3, self.rail_thermal = self._blade(
            p, "U3", "PER-CORE CPU  /  POWER & THERMAL")
        pt = tk.Frame(c3, bg=RACK_BLADE)
        pt.pack(fill="x")
        pt.columnconfigure(0, weight=1)
        pt.columnconfigure(1, weight=1)
        self.f_cores = tk.Frame(pt, bg=RACK_BLADE)
        self.f_cores.grid(row=0, column=0, sticky="nsew", padx=(0, 14))
        self._core_widgets = []
        self.lbl_thermal = tk.Label(pt, text="Loading…",
                                    font=("Courier", 10),
                                    fg=WHITE, bg=RACK_BLADE,
                                    justify="left", anchor="nw")
        self.lbl_thermal.grid(row=0, column=1, sticky="nw")

        # ── U4 — STORAGE  /  NETWORK ──────────────────────────────────────
        c4, _ = self._blade(p, "U4", "STORAGE ARRAY  /  NETWORK")
        sn = tk.Frame(c4, bg=RACK_BLADE)
        sn.pack(fill="x")
        sn.columnconfigure(0, weight=1)
        sn.columnconfigure(1, weight=1)
        self.f_storage = tk.Frame(sn, bg=RACK_BLADE)
        self.f_storage.grid(row=0, column=0, sticky="nsew", padx=(0, 14))
        self._storage_widgets = []
        self.f_network = tk.Frame(sn, bg=RACK_BLADE)
        self.f_network.grid(row=0, column=1, sticky="nsew")
        self._network_widgets = []

        # ── U5 — CONTAINERS  /  TOP PROCESSES ─────────────────────────────
        c5, _ = self._blade(p, "U5", "CONTAINERS  /  TOP PROCESSES")
        cp = tk.Frame(c5, bg=RACK_BLADE)
        cp.pack(fill="x")
        cp.columnconfigure(0, weight=1)
        cp.columnconfigure(1, weight=1)
        self.f_docker = tk.Frame(cp, bg=RACK_BLADE)
        self.f_docker.grid(row=0, column=0, sticky="nw", padx=(0, 14))
        self._docker_widgets = []
        self.lbl_procs = tk.Label(cp, text="Loading…",
                                  font=("Courier", 10),
                                  fg=WHITE, bg=RACK_BLADE,
                                  justify="left", anchor="nw")
        self.lbl_procs.grid(row=0, column=1, sticky="nw")

        if IS_WINDOWS:
            tk.Label(p, text="★  Local stats reflect this Windows machine "
                            "— full Pi-only telemetry appears when running on the Pi.",
                     font=("Helvetica", 9, "italic"),
                     fg=GREY, bg=BG).pack(anchor="w", padx=14, pady=(4, 0))

    # ── Extended diagnostics polling (runs in background thread) ─────────────

    def _bg_refresh_extras(self):
        """Collect extended Pi/system diagnostics and post update to UI thread."""
        # Re-entry lock: if the previous extras run is still in flight, skip
        # this tick. Non-blocking acquire is the correct primitive — a bool
        # check-then-set is NOT atomic across CPython threads.
        if not self._extras_lock.acquire(blocking=False):
            return
        try:
            self._do_bg_refresh_extras()
        finally:
            self._extras_lock.release()

    def _do_bg_refresh_extras(self):
        # Stagger slightly so we don't fight _bg_refresh's cpu_percent call
        time.sleep(0.4)
        now = time.time()

        cores    = cpu_per_core()
        hw       = pi_hardware()    if IS_PI else {}
        throttle = pi_throttled()   if IS_PI else {"status": "n/a", "ok": True, "past": False}
        voltages = pi_voltages()    if IS_PI else {}
        clocks   = pi_clocks()      if IS_PI else {}
        pmic     = pi_pmic_temp()   if IS_PI else None
        freq     = cpu_freq_info()
        mem      = mem_detail()
        disks    = disks_per_mount()
        net      = net_io_per_iface()
        disk_io  = disk_io_total()
        procs    = top_processes(5)
        docker   = docker_summary()
        load     = load_avg_str()
        uptime   = uptime_short()
        ts_ip    = tailscale_ip()

        # Disk I/O rate (bytes/sec) from delta vs last sample
        disk_io_rate = (-1.0, -1.0)
        if self._last_disk_io_ts > 0:
            dt = now - self._last_disk_io_ts
            if dt > 0:
                dr = (disk_io[0] - self._last_disk_io[0]) / dt
                dw = (disk_io[1] - self._last_disk_io[1]) / dt
                disk_io_rate = (max(0.0, dr), max(0.0, dw))
        self._last_disk_io    = disk_io
        self._last_disk_io_ts = now

        # Per-iface network rates from deltas
        net_rates = {}
        if self._last_net_io_ts > 0:
            dt = now - self._last_net_io_ts
            if dt > 0:
                for iface, (s, r) in net.items():
                    if iface in self._last_net_io:
                        ls, lr = self._last_net_io[iface]
                        net_rates[iface] = (
                            max(0.0, (s - ls) / dt),
                            max(0.0, (r - lr) / dt),
                        )
        self._last_net_io    = net
        self._last_net_io_ts = now

        try:
            self.after(0, self._update_extras, dict(
                cores=cores, hw=hw, throttle=throttle, voltages=voltages,
                clocks=clocks, pmic=pmic, freq=freq, mem=mem, disks=disks,
                net=net, net_rates=net_rates, disk_io_rate=disk_io_rate,
                procs=procs, docker=docker, load=load, uptime=uptime,
                ts_ip=ts_ip,
            ))
        except Exception:
            # Window may have been destroyed mid-update
            pass

    def _update_extras(self, e):
        """Apply extended diagnostics dict `e` to the Diagnostics-tab widgets."""

        # ── Header status strip ────────────────────────────────────────────
        th = e["throttle"]
        if not th["ok"]:
            self.lbl_diag_status.config(text=f"⚠ {th['status']}", fg=RED)
            self.rail_thermal.config(bg=RACK_RAIL_ERR)
        elif th["past"]:
            self.lbl_diag_status.config(text=f"◐ {th['status']}", fg=ORANGE)
            self.rail_thermal.config(bg=RACK_RAIL_WARN)
        else:
            self.lbl_diag_status.config(text="● SYSTEM OK", fg=GREEN)
            self.rail_thermal.config(bg=RACK_RAIL_OK)
        self.lbl_diag_load.config(text=f"LOAD {e['load']}")
        self.lbl_diag_uptime.config(text=f"UPTIME {e['uptime']}")

        # ── Per-core mini-bars ─────────────────────────────────────────────
        cores = e["cores"] or []
        if len(self._core_widgets) != len(cores):
            for w in self._core_widgets:
                w["frame"].destroy()
            self._core_widgets = []
            for i in range(len(cores)):
                f = tk.Frame(self.f_cores, bg=RACK_BLADE)
                f.pack(fill="x", pady=2)
                tk.Label(f, text=f"C{i}", font=("Courier", 10, "bold"),
                         fg=GOLD, bg=RACK_BLADE, width=4, anchor="w"
                         ).pack(side="left")
                led = make_led(f, GREEN, size=8, bg=RACK_BLADE)
                led.pack(side="left", padx=(0, 6))
                pb = ttk.Progressbar(f, length=180, maximum=100,
                                     style="Diag.Horizontal.TProgressbar")
                pb.pack(side="left", fill="x", expand=True)
                lbl = tk.Label(f, text="—", font=("Courier", 10),
                               fg=WHITE, bg=RACK_BLADE, width=8, anchor="e")
                lbl.pack(side="left", padx=(8, 0))
                self._core_widgets.append({"frame": f, "led": led,
                                           "pb": pb, "lbl": lbl})
        for i, pct in enumerate(cores):
            cw = self._core_widgets[i]
            col = RED if pct > 85 else (ORANGE if pct > 60 else GREEN)
            cw["led"].set_color(col)
            cw["pb"]["value"] = pct
            cw["lbl"].config(text=f"{pct:5.1f}%", fg=col)

        # ── Power / thermal panel (right column of U3) ─────────────────────
        fr   = e["freq"]
        cl   = e["clocks"]
        vo   = e["voltages"]
        pmic = e["pmic"]
        hw   = e["hw"]
        parts = []
        model = (hw.get("model", "") or "Unknown")[:34]
        parts.append(f"  MODEL    : {model}")
        parts.append(f"  KERNEL   : {hw.get('kernel', '?')[:34]}")
        parts.append(f"  GOVERNOR : {fr.get('governor', '?')}")
        parts.append(f"  CPU FREQ : {fr.get('cur', 0):>4} / "
                     f"{fr.get('max', 0):>4} MHz")
        if cl:
            parts.append(f"  CLOCKS   : arm={cl.get('arm', '?')} "
                         f" core={cl.get('core', '?')} "
                         f" v3d={cl.get('v3d', '?')} MHz")
        if vo:
            parts.append(f"  VCORE    : {vo.get('core', '?')} V")
            parts.append(f"  VSDRAM_C : {vo.get('sdram_c', '?')} V")
        parts.append(f"  PMIC TEMP: {pmic * 9 / 5 + 32:.1f} °F"
                     if pmic is not None else "  PMIC TEMP: —")
        parts.append(f"  THROTTLE : {th['status']}")
        # Memory secondary stats (cached/buffers/swap) — keep them on this
        # panel so U2's RAM gauge stays uncluttered
        m = e["mem"]
        parts.append("")
        parts.append(f"  CACHED   : {m['cached_mb']:>5} MB    "
                     f"BUFFERS: {m['buffers_mb']:>5} MB")
        parts.append(f"  SWAP     : {m['swap_used_mb']:>5} / "
                     f"{m['swap_total_mb']:>5} MB ({m['swap_pct']:.1f}%)")
        self.lbl_thermal.config(text="\n".join(parts))

        # ── Storage rows ───────────────────────────────────────────────────
        disks = e["disks"]
        if len(self._storage_widgets) != len(disks):
            for w in self._storage_widgets:
                w["frame"].destroy()
            self._storage_widgets = []
            for d in disks:
                f = tk.Frame(self.f_storage, bg=RACK_BLADE)
                f.pack(fill="x", pady=2)
                led = make_led(f, GREEN, size=8, bg=RACK_BLADE)
                led.pack(side="left", padx=(0, 6))
                tk.Label(f, text=d["mount"], font=("Courier", 10, "bold"),
                         fg=WHITE, bg=RACK_BLADE, width=14, anchor="w"
                         ).pack(side="left")
                pb = ttk.Progressbar(f, length=140, maximum=100,
                                     style="Diag.Horizontal.TProgressbar")
                pb.pack(side="left", fill="x", expand=True)
                lbl = tk.Label(f, text="—", font=("Courier", 9),
                               fg=GREY, bg=RACK_BLADE, width=22, anchor="e")
                lbl.pack(side="left", padx=(6, 0))
                self._storage_widgets.append({"frame": f, "led": led,
                                              "pb": pb, "lbl": lbl})
        for i, d in enumerate(disks):
            sw = self._storage_widgets[i]
            col = RED if d["pct"] > 90 else (ORANGE if d["pct"] > 75 else GREEN)
            sw["led"].set_color(col)
            sw["pb"]["value"] = d["pct"]
            sw["lbl"].config(
                text=f"{d['used_gb']:.1f} / {d['total_gb']:.1f} GB ({d['pct']:.0f}%)",
                fg=col)

        # I/O rate row (rebuilt at the bottom of f_storage if missing)
        if not hasattr(self, "lbl_disk_io"):
            self.lbl_disk_io = tk.Label(self.f_storage, text="",
                                        font=("Courier", 9),
                                        fg=GREY, bg=RACK_BLADE,
                                        anchor="w")
            self.lbl_disk_io.pack(fill="x", pady=(4, 0))
        dr, dw = e["disk_io_rate"]
        if dr >= 0:
            self.lbl_disk_io.config(
                text=f"  I/O   read {fmt_rate(dr)}    write {fmt_rate(dw)}")
            self._hist_dio.append((dr + dw) / 1024)
            self.spk_dio.add_sample((dr + dw) / 1024)
        else:
            self.lbl_disk_io.config(text="")

        # ── Network rows ───────────────────────────────────────────────────
        nets   = list(e["net"].items())
        nrates = e["net_rates"]
        wanted = len(nets) + (1 if e["ts_ip"] else 0)
        if len(self._network_widgets) != wanted:
            for w in self._network_widgets:
                w["frame"].destroy()
            self._network_widgets = []
            for iface, _ in nets:
                f = tk.Frame(self.f_network, bg=RACK_BLADE)
                f.pack(fill="x", pady=2)
                led = make_led(f, GREEN, size=8, bg=RACK_BLADE)
                led.pack(side="left", padx=(0, 6))
                tk.Label(f, text=iface, font=("Courier", 10, "bold"),
                         fg=WHITE, bg=RACK_BLADE, width=12, anchor="w"
                         ).pack(side="left")
                lbl = tk.Label(f, text="—", font=("Courier", 9),
                               fg=GREY, bg=RACK_BLADE, anchor="w")
                lbl.pack(side="left", padx=(6, 0), fill="x", expand=True)
                self._network_widgets.append({"frame": f, "led": led,
                                              "lbl": lbl, "iface": iface})
            if e["ts_ip"]:
                f = tk.Frame(self.f_network, bg=RACK_BLADE)
                f.pack(fill="x", pady=2)
                led = make_led(f, BLUE, size=8, bg=RACK_BLADE)
                led.pack(side="left", padx=(0, 6))
                tk.Label(f, text="tailscale", font=("Courier", 10, "bold"),
                         fg=GOLD, bg=RACK_BLADE, width=12, anchor="w"
                         ).pack(side="left")
                lbl = tk.Label(f, text=e["ts_ip"], font=("Courier", 10),
                               fg=GOLD, bg=RACK_BLADE, anchor="w")
                lbl.pack(side="left", padx=(6, 0), fill="x", expand=True)
                self._network_widgets.append({"frame": f, "led": led,
                                              "lbl": lbl, "iface": "_ts"})
        # Update existing rows + accumulate downstream rate for the sparkline
        total_dn = 0.0
        for w in self._network_widgets:
            if w["iface"] == "_ts":
                w["lbl"].config(text=e["ts_ip"])
                continue
            s, r   = e["net"].get(w["iface"], (0, 0))
            rs, rr = nrates.get(w["iface"], (-1.0, -1.0))
            if rs >= 0:
                total_dn += rr
                w["lbl"].config(
                    text=f"↑ {fmt_rate(rs)}  ↓ {fmt_rate(rr)}    "
                         f"tot {r/1024**2:6.1f} MiB")
            else:
                w["lbl"].config(
                    text=f"  tot ↑{s/1024**2:6.1f}  ↓{r/1024**2:6.1f} MiB")
        if total_dn > 0 or self._hist_net:
            self._hist_net.append(total_dn / 1024)
            self.spk_net.add_sample(total_dn / 1024)

        # ── Containers ─────────────────────────────────────────────────────
        dk    = e["docker"]
        lines = dk.get("lines", []) or []
        # Header line above the rows (rebuild lazily)
        if not hasattr(self, "lbl_docker_hdr"):
            self.lbl_docker_hdr = tk.Label(
                self.f_docker, text="—",
                font=("Courier", 9, "bold"),
                fg=GREY, bg=RACK_BLADE, anchor="w")
            self.lbl_docker_hdr.pack(fill="x", pady=(0, 4))
        if lines:
            self.lbl_docker_hdr.config(
                text=f"  RUNNING {dk['running']}   "
                     f"STOPPED {dk['stopped']}   "
                     f"UNHEALTHY {dk['unhealthy']}",
                fg=ORANGE if dk["unhealthy"] else GREY)
        else:
            self.lbl_docker_hdr.config(text="  (docker not available)")

        shown = lines[:8]
        if len(self._docker_widgets) != len(shown):
            for w in self._docker_widgets:
                w["frame"].destroy()
            self._docker_widgets = []
            for _ in shown:
                f = tk.Frame(self.f_docker, bg=RACK_BLADE)
                f.pack(fill="x", pady=1)
                led = make_led(f, GREEN, size=8, bg=RACK_BLADE)
                led.pack(side="left", padx=(0, 6))
                name_lbl = tk.Label(f, text="", font=("Courier", 10, "bold"),
                                    fg=WHITE, bg=RACK_BLADE,
                                    width=18, anchor="w")
                name_lbl.pack(side="left")
                stat_lbl = tk.Label(f, text="", font=("Courier", 9),
                                    fg=GREY, bg=RACK_BLADE, anchor="w")
                stat_lbl.pack(side="left", padx=(4, 0), fill="x", expand=True)
                self._docker_widgets.append({"frame": f, "led": led,
                                             "name": name_lbl,
                                             "stat": stat_lbl})
        for i, (n, state, st, unhealthy) in enumerate(shown):
            if unhealthy:
                col = ORANGE
            elif state == "running":
                col = GREEN
            else:
                col = RED
            dwid = self._docker_widgets[i]
            dwid["led"].set_color(col)
            dwid["name"].config(text=n[:18], fg=col)
            dwid["stat"].config(text=st[:42])

        # ── Top processes (compact 2-column table) ─────────────────────────
        proc = e["procs"]
        if proc["by_cpu"] or proc["by_ram"]:
            rows = [f"  {'-- BY CPU --':<26}    {'-- BY RAM --':<26}"]
            for i in range(5):
                cp = proc["by_cpu"][i] if i < len(proc["by_cpu"]) else None
                rp = proc["by_ram"][i] if i < len(proc["by_ram"]) else None
                cl_t = (f"  {cp['name'][:16]:<16} {cp['cpu']:5.1f}%"
                        if cp else " " * 26)
                rl_t = (f"  {rp['name'][:16]:<16} {rp['ram']:5.1f}%"
                        if rp else "")
                rows.append(f"{cl_t:<30}    {rl_t}")
            self.lbl_procs.config(text="\n".join(rows))
        else:
            self.lbl_procs.config(text="  (process info unavailable — install psutil)")

    # ── Sites tab ─────────────────────────────────────────────────────────────

    def _build_sites(self):
        p = self.tab_sites

        tk.Label(p, text="WEBSITES", font=("Helvetica", 9),
                 fg=GREY, bg=BG, pady=8).pack(anchor="w", padx=14)

        self.site_rows = {}
        sites_f = tk.Frame(p, bg=BG3, padx=8, pady=8,
                           highlightbackground=BORDER, highlightthickness=1)
        sites_f.pack(fill="x", padx=14, pady=4)

        for name, url in WEBSITES:
            row_f = tk.Frame(sites_f, bg=BG3)
            row_f.pack(fill="x", padx=8, pady=6)
            tk.Label(row_f, text=name, font=("Helvetica", 13, "bold"),
                     fg=GOLD, bg=BG3, width=24, anchor="w").pack(side="left")
            dot = tk.Label(row_f, text="●", font=("Helvetica", 14),
                           fg=GREY, bg=BG3)
            dot.pack(side="left", padx=8)
            lbl = tk.Label(row_f, text="checking…", font=("Helvetica", 11),
                           fg=GREY, bg=BG3, width=24, anchor="w")
            lbl.pack(side="left")
            ms_lbl = tk.Label(row_f, text="", font=("Helvetica", 11),
                              fg=GREY, bg=BG3)
            ms_lbl.pack(side="left")
            self.site_rows[name] = (dot, lbl, ms_lbl)

    # ── Logs tab ──────────────────────────────────────────────────────────────

    def _build_logs(self):
        p = self.tab_logs

        ctrl = tk.Frame(p, bg=BG)
        ctrl.pack(fill="x", padx=8, pady=6)

        if IS_WINDOWS:
            tk.Label(ctrl,
                     text="Log streaming is only available when running directly on the Pi.  "
                          "Use the buttons below to open the admin dashboard in your browser.",
                     font=("Helvetica", 11, "italic"), fg=GREY, bg=BG, wraplength=800,
                     justify="left").pack(side="left", padx=4)
        else:
            tk.Label(ctrl, text="Service:", fg=GREY, bg=BG,
                     font=("Helvetica", 11)).pack(side="left", padx=(0, 6))
            self.log_svc = tk.StringVar(value="hanryxvault")
            for display, value in [("POS Server", "hanryxvault"),
                                   ("nginx", "nginx"),
                                   ("WireGuard", "wg-quick@wg0")]:
                tk.Radiobutton(ctrl, text=display, variable=self.log_svc,
                               value=value, bg=BG, fg=WHITE,
                               selectcolor=BG3, activebackground=BG,
                               font=("Helvetica", 11),
                               command=self._refresh_logs).pack(side="left", padx=6)
            tk.Button(ctrl, text="Refresh", command=self._refresh_logs,
                      bg=BG3, fg=GOLD, relief="flat",
                      font=("Helvetica", 11), padx=12, pady=4
                      ).pack(side="right", padx=4)

        self.log_box = scrolledtext.ScrolledText(
            p, bg="#060606", fg="#c0c0c0",
            font=("Courier", 10), relief="flat",
            wrap="none", state="disabled"
        )
        self.log_box.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        if IS_WINDOWS:
            open_row = tk.Frame(p, bg=BG)
            open_row.pack(fill="x", padx=8, pady=4)
            for label, path in [("Open Admin Dashboard", "/admin"),
                                  ("Open Sales Log", "/admin/sales"),
                                  ("Open Error Log", "/admin/error-log")]:
                tk.Button(open_row, text=label,
                          command=lambda u=f"{server_url()}{path}": webbrowser.open(u),
                          bg=BG3, fg=GOLD, relief="flat",
                          font=("Helvetica", 11), padx=12, pady=6
                          ).pack(side="left", padx=4)

    # ── Settings tab ──────────────────────────────────────────────────────────

    def _build_settings(self):
        p = self.tab_settings

        frm = tk.Frame(p, bg=BG3, padx=24, pady=20,
                       highlightbackground=BORDER, highlightthickness=1)
        frm.pack(padx=40, pady=30, fill="x")

        tk.Label(frm, text="Pi Connection Settings",
                 font=("Helvetica", 16, "bold"), fg=GOLD, bg=BG3).pack(anchor="w")
        tk.Label(frm,
                 text="Enter the IP address (or hostname) and port of your Raspberry Pi POS server.",
                 font=("Helvetica", 11), fg=GREY, bg=BG3, wraplength=700
                 ).pack(anchor="w", pady=(4, 16))

        def field(label, default):
            row = tk.Frame(frm, bg=BG3)
            row.pack(fill="x", pady=4)
            tk.Label(row, text=label, font=("Helvetica", 12),
                     fg=WHITE, bg=BG3, width=16, anchor="w").pack(side="left")
            var = tk.StringVar(value=default)
            ent = tk.Entry(row, textvariable=var, font=("Helvetica", 12),
                           bg=BG, fg=WHITE, relief="flat",
                           insertbackground=WHITE, width=30)
            ent.pack(side="left", padx=(8, 0))
            return var

        self.cfg_host = field("Pi Host / IP",  CFG.get("host", "127.0.0.1"))
        self.cfg_port = field("Port",           CFG.get("port", "8080"))

        def apply_settings():
            CFG["host"] = self.cfg_host.get().strip() or "127.0.0.1"
            CFG["port"] = self.cfg_port.get().strip() or "8080"
            save_config(CFG)
            self.lbl_host.config(text=f"  ⇒  {server_url()}")
            messagebox.showinfo("Settings Saved",
                                f"Now monitoring: {server_url()}\n\n"
                                "The dashboard will refresh automatically.")

        tk.Button(frm, text="Save & Apply", command=apply_settings,
                  bg=GOLD, fg="#000", font=("Helvetica", 12, "bold"),
                  relief="flat", padx=16, pady=8, cursor="hand2"
                  ).pack(anchor="w", pady=(16, 0))

        # Platform info
        info_f = tk.Frame(p, bg=BG3, padx=24, pady=16,
                          highlightbackground=BORDER, highlightthickness=1)
        info_f.pack(padx=40, pady=8, fill="x")
        tk.Label(info_f, text="Environment",
                 font=("Helvetica", 13, "bold"), fg=GOLD, bg=BG3).pack(anchor="w")

        plat = platform.platform()
        py_v = sys.version.split()[0]
        psutil_v = "installed" if HAS_PSUTIL else "NOT installed — run: pip install psutil"
        info_lines = [
            f"Platform: {plat}",
            f"Python:   {py_v}",
            f"psutil:   {psutil_v}",
            f"Config:   {CONFIG_PATH}",
        ]
        for line in info_lines:
            tk.Label(info_f, text=line, font=("Courier", 11),
                     fg=WHITE, bg=BG3, anchor="w").pack(anchor="w")

    # ── Action bar ────────────────────────────────────────────────────────────

    def _build_actions(self):
        bar = tk.Frame(self, bg=BG2, pady=8,
                       highlightbackground=BORDER, highlightthickness=1)
        bar.pack(fill="x", side="bottom")

        def btn(label, cmd, color=BG3, fg=GOLD):
            tk.Button(bar, text=label, command=cmd,
                      bg=color, fg=fg, relief="flat",
                      font=("Helvetica", 11, "bold"),
                      padx=12, pady=6, cursor="hand2"
                      ).pack(side="left", padx=5, pady=4)

        btn("Open Admin",       lambda: webbrowser.open(f"{server_url()}/admin"))
        btn("📊 P&L",           lambda: webbrowser.open(f"{server_url()}/admin/profit-loss"))
        btn("🏷️ Laybys",        lambda: webbrowser.open(f"{server_url()}/admin/layby"))
        btn("📥 Export CSV",    lambda: webbrowser.open(f"{server_url()}/admin/inventory/export"))
        btn("Backup DB",        self._backup_db)

        if not IS_WINDOWS:
            btn("Restart Server", self._restart_server_pi)

        btn("Quit", self.destroy, color="#1a0000", fg=RED)

    # ── Periodic refresh ──────────────────────────────────────────────────────

    def _refresh(self):
        self.lbl_time.config(
            text=datetime.datetime.now().strftime("  %A %d %b %Y  %H:%M:%S")
        )
        threading.Thread(target=self._bg_refresh,        daemon=True).start()
        threading.Thread(target=self._bg_refresh_extras, daemon=True).start()
        self.after(REFRESH_MS, self._refresh)

    def _bg_refresh(self):
        # --- POS stats via HTTP API ---
        sc, monitor_data, ping_ms = _get("/admin/monitor-stats", timeout=5)
        _, health_data, _         = _get("/health", timeout=4)

        # --- Service health checks ---
        svc_results = {}
        for name, url_tpl in SERVICES:
            url = url_tpl.format(host=CFG["host"], port=CFG["port"])
            code, ms = ping_url(url, timeout=3)
            svc_results[name] = (code in (200, 301, 302), ms)

        # --- Website pings ---
        site_results = {name: ping_url(url) for name, url in WEBSITES}

        # --- Local system (this machine) ---
        cpu      = sys_cpu()
        ram_u, ram_t, ram_pct = sys_ram()
        disk_u, disk_t, disk_pct = sys_disk()
        temp     = sys_cpu_temp()

        self.after(0, self._update_ui,
                   sc, ping_ms, monitor_data, health_data,
                   svc_results, site_results,
                   cpu, ram_u, ram_t, ram_pct,
                   disk_u, disk_t, disk_pct, temp)

    def _update_ui(self, sc, ping_ms, d, health,
                   svc_results, site_results,
                   cpu, ram_u, ram_t, ram_pct,
                   disk_u, disk_t, disk_pct, temp):

        if not isinstance(d, dict):
            d = {}

        # ── Dashboard ─────────────────────────────────────────────────────────
        self.c_today_sales.config(text=str(d.get("today_sales", "—")))
        rev  = d.get("today_revenue", None)
        tips = d.get("today_tips", None)
        self.c_today_rev.config(
            text=f"£{rev:.2f}"  if rev  is not None else "—")
        self.c_today_tips.config(
            text=f"£{tips:.2f}" if tips is not None else "—")
        self.c_total_sales.config(text=str(d.get("total_sales", "—")))
        self.c_inv_count.config(text=str(d.get("inv_count",    "—")))

        pending = d.get("pending_scans", 0)
        self.c_pending.config(text=str(pending) if pending != "—" else "—",
                               fg=ORANGE if pending else GREEN)

        low = d.get("low_stock", 0)
        out = d.get("out_stock", 0)
        self.lbl_low_stock.config(
            text=f"Low (≤5): {low}", fg=ORANGE if low else GREEN)
        self.lbl_out_stock.config(
            text=f"Out of stock: {out}", fg=RED if out else GREEN)

        # Services
        for name, (ok, ms) in svc_results.items():
            dot, lbl = self.svc_labels[name]
            dot.config(fg=GREEN if ok else RED)
            lbl.config(text=f"ok  {ms}ms" if ok else "unreachable",
                       fg=GREEN if ok else RED)

        # Server ping
        if sc in (200, 301, 302) and ping_ms:
            col = GREEN if ping_ms < 100 else (ORANGE if ping_ms < 300 else RED)
            self.lbl_ping.config(text=f"✓  {ping_ms} ms  (HTTP {sc})", fg=col)
        else:
            self.lbl_ping.config(
                text=f"✗  No response from {server_url()}", fg=RED)

        # ── Business ──────────────────────────────────────────────────────────
        ol   = d.get("open_laybys", "—")
        lb   = d.get("layby_outstanding", None)
        op   = d.get("open_pos", "—")
        oti  = d.get("open_trade_ins", "—")
        eod  = d.get("eod_today", None)

        self.c_open_laybys.config(
            text=str(ol), fg=ORANGE if ol and ol != "—" and ol > 0 else BLUE)
        self.c_layby_bal.config(
            text=f"£{lb:.2f}" if lb is not None else "—",
            fg=ORANGE if lb and lb > 0 else BLUE)
        self.c_open_pos.config(
            text=str(op), fg=ORANGE if op and op != "—" and op > 0 else GREEN)
        self.c_open_trade_in.config(
            text=str(oti), fg=ORANGE if oti and oti != "—" and oti > 0 else GREEN)

        pl_r = d.get("pl_30d_revenue", None)
        pl_p = d.get("pl_30d_profit",  None)
        pl_m = d.get("pl_30d_margin",  None)
        self.c_pl_rev.config(
            text=f"£{pl_r:.2f}"  if pl_r is not None else "—")
        self.c_pl_profit.config(
            text=f"£{pl_p:.2f}"  if pl_p is not None else "—",
            fg=GREEN if pl_p and pl_p >= 0 else RED)
        self.c_pl_margin.config(
            text=f"{pl_m:.1f}%"  if pl_m is not None else "—",
            fg=GREEN if pl_m and pl_m >= 0 else RED)
        if eod is True:
            self.c_eod.config(text="✓ Done", fg=GREEN)
        elif eod is False:
            self.c_eod.config(text="Pending", fg=ORANGE)
        else:
            self.c_eod.config(text="—", fg=GREY)

        # ── Diagnostics tab — feed ring gauges + sparklines ───────────────────
        cpu_col = RED if cpu > 80 else (ORANGE if cpu > 60 else GREEN)
        self.gauge_cpu.set_value(cpu, color=cpu_col)
        self._hist_cpu.append(cpu)
        self.spk_cpu.add_sample(cpu)

        if temp is not None:
            # Color thresholds stay in °C (sensor's native unit); the
            # gauge value is converted to °F for display.
            temp_col = RED if temp > 75 else (ORANGE if temp > 65 else GREEN)
            self.gauge_temp.set_value(temp * 9 / 5 + 32, color=temp_col)
        else:
            self.gauge_temp.set_value(0, color=GREY)

        ram_col = RED if ram_pct > 85 else (ORANGE if ram_pct > 70 else BLUE)
        self.gauge_ram.set_value(ram_pct, color=ram_col)
        self._hist_ram.append(ram_pct)
        self.spk_ram.add_sample(ram_pct)

        disk_pct_n = disk_pct if isinstance(disk_pct, (int, float)) else 0
        disk_col = RED if disk_pct_n > 90 else (ORANGE if disk_pct_n > 75 else WHITE)
        self.gauge_disk.set_value(disk_pct_n, color=disk_col)

        # Server-side stats (DB size, server uptime) intentionally NOT shown
        # on the Diagnostics tab — that page is strictly Pi telemetry.

        # ── Sites ─────────────────────────────────────────────────────────────
        for name, _ in WEBSITES:
            sc2, ms2 = site_results.get(name, (0, 0))
            dot, lbl, ms_lbl = self.site_rows[name]
            if sc2 in (200, 301, 302):
                dot.config(fg=GREEN)
                lbl.config(text="online", fg=GREEN)
                ms_lbl.config(text=f"  {ms2} ms", fg=GREY)
            else:
                dot.config(fg=RED)
                lbl.config(text="offline / unreachable", fg=RED)
                ms_lbl.config(text="")

    # ── Logs refresh ──────────────────────────────────────────────────────────

    def _refresh_logs(self):
        if IS_WINDOWS:
            return
        svc = self.log_svc.get()
        try:
            out = subprocess.check_output(
                f"journalctl -u {svc} -n {LOG_LINES} --no-pager "
                f"--output=short-iso 2>/dev/null",
                shell=True, stderr=subprocess.DEVNULL, timeout=5
            ).decode()
        except Exception:
            out = "Could not fetch logs. Make sure you are running on the Pi."

        self.log_box.config(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.insert("end", out)
        self.log_box.see("end")
        self.log_box.config(state="disabled")

    # ── Actions ───────────────────────────────────────────────────────────────

    def _backup_db(self):
        def do_backup():
            sc, resp = _post("/admin/backup-db")
            if sc == 200:
                self.after(0, lambda: messagebox.showinfo(
                    "Backup", resp.get("message", "Backup complete.")))
            else:
                self.after(0, lambda: messagebox.showwarning(
                    "Backup", f"Server returned {sc}.\n"
                              f"Check the admin panel for manual backup options."))
        threading.Thread(target=do_backup, daemon=True).start()

    def _restart_server_pi(self):
        if not messagebox.askyesno("Restart Server",
                                   "Restart the HanryxVault POS server?\n"
                                   "(Requires sudo on this machine)"):
            return
        try:
            subprocess.run(
                "sudo systemctl restart hanryxvault",
                shell=True, check=True, timeout=12)
            messagebox.showinfo("Done", "Server restarted.")
        except subprocess.CalledProcessError as e:
            messagebox.showerror("Error", str(e))


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    _parser = argparse.ArgumentParser(description="HanryxVault Monitor")
    _parser.add_argument(
        "--kiosk", action="store_true",
        help="Fullscreen kiosk mode — auto-connects to localhost, hides cursor, "
             "no title bar.  Press F11 or Ctrl+Alt+Q to exit.",
    )
    _args = _parser.parse_args()
    app = HanryxMonitor(kiosk=_args.kiosk)
    app.mainloop()
