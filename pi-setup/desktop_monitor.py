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


def sys_disk():
    """Returns (used_str, total_str, pct_float)."""
    if HAS_PSUTIL:
        d = psutil.disk_usage("/")
        return (
            f"{d.used / 1024**3:.1f}G",
            f"{d.total / 1024**3:.1f}G",
            d.percent,
        )
    if IS_LINUX:
        try:
            out = subprocess.check_output(
                "df -h / | tail -1", shell=True,
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
        nb.add(self.tab_system,   text="  System     ")
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

    # ── System tab ────────────────────────────────────────────────────────────

    def _build_system(self):
        p = self.tab_system
        for i in range(4):
            p.columnconfigure(i, weight=1)

        self.c_cpu   = self._card(p, 0, 0, "CPU Usage", "—",   GREEN)
        self.c_temp  = self._card(p, 0, 1, "CPU Temp",  "N/A", ORANGE)
        self.c_ram   = self._card(p, 0, 2, "RAM Used",  "—",   WHITE)
        self.c_disk  = self._card(p, 0, 3, "Disk Used", "—",   WHITE)

        # Progress bars
        bars = tk.Frame(p, bg=BG)
        bars.grid(row=1, column=0, columnspan=4, sticky="ew", padx=8, pady=4)
        bars.columnconfigure(1, weight=1)

        def bar_row(label, row):
            tk.Label(bars, text=label, font=("Helvetica", 10),
                     fg=GREY, bg=BG, width=8, anchor="e").grid(
                row=row, column=0, padx=(0, 8), pady=4)
            pb = ttk.Progressbar(bars, length=400, maximum=100)
            pb.grid(row=row, column=1, sticky="ew", pady=4)
            lbl = tk.Label(bars, text="", font=("Helvetica", 10),
                           fg=WHITE, bg=BG, width=14, anchor="w")
            lbl.grid(row=row, column=2, padx=8, pady=4)
            return pb, lbl

        self.pb_cpu,  self.pb_cpu_lbl  = bar_row("CPU",  0)
        self.pb_ram,  self.pb_ram_lbl  = bar_row("RAM",  1)
        self.pb_disk, self.pb_disk_lbl = bar_row("Disk", 2)

        # Server info
        srv_f = tk.Frame(p, bg=BG3, padx=14, pady=10,
                         highlightbackground=BORDER, highlightthickness=1)
        srv_f.grid(row=2, column=0, columnspan=4, sticky="ew", padx=14, pady=8)
        tk.Label(srv_f, text="SERVER / DATABASE", font=("Helvetica", 9),
                 fg=GREY, bg=BG3).pack(anchor="w")
        self.lbl_db_info = tk.Label(srv_f, text="Loading…",
                                     font=("Helvetica", 12), fg=WHITE, bg=BG3)
        self.lbl_db_info.pack(anchor="w", pady=(4, 0))
        self.lbl_uptime = tk.Label(srv_f, text="",
                                    font=("Helvetica", 12), fg=GREY, bg=BG3)
        self.lbl_uptime.pack(anchor="w")

        if IS_WINDOWS:
            tk.Label(p, text="★  Local system stats shown above reflect this Windows machine.",
                     font=("Helvetica", 10, "italic"), fg=GREY, bg=BG
                     ).grid(row=3, column=0, columnspan=4, padx=14, pady=4, sticky="w")

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
        threading.Thread(target=self._bg_refresh, daemon=True).start()
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

        # ── System ────────────────────────────────────────────────────────────
        cpu_col = RED if cpu > 80 else (ORANGE if cpu > 60 else GREEN)
        self.c_cpu.config(text=f"{cpu:.1f}%", fg=cpu_col)

        if temp is not None:
            temp_col = RED if temp > 75 else (ORANGE if temp > 65 else GREEN)
            self.c_temp.config(text=f"{temp:.1f}°C", fg=temp_col)
        else:
            self.c_temp.config(text="N/A", fg=GREY)

        self.c_ram.config(
            text=f"{ram_pct:.0f}%", fg=RED if ram_pct > 85 else WHITE)
        self.c_disk.config(text=f"{disk_u}/{disk_t}")

        self.pb_cpu["value"]  = cpu
        self.pb_cpu_lbl.config(text=f"{cpu:.1f}%")
        self.pb_ram["value"]  = ram_pct
        self.pb_ram_lbl.config(text=f"{ram_u}/{ram_t} MB")
        disk_pct_n = disk_pct if isinstance(disk_pct, (int, float)) else 0
        self.pb_disk["value"] = disk_pct_n
        self.pb_disk_lbl.config(text=f"{disk_u}/{disk_t}")

        db_mb  = d.get("db_size_mb", None)
        uptime = d.get("uptime_s", None)
        db_txt = f"DB size: {db_mb:.2f} MB" if db_mb is not None else "DB: N/A"
        self.lbl_db_info.config(text=db_txt)
        if uptime is not None:
            h, rem = divmod(uptime, 3600)
            m, s   = divmod(rem, 60)
            self.lbl_uptime.config(text=f"Server uptime: {h}h {m}m {s}s")

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
