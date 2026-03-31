#!/usr/bin/env python3
"""
HanryxVault Pi Monitor — Desktop GUI
Run on your Raspberry Pi desktop:  python3 desktop_monitor.py

Shows live stats for:
  - POS server (sales, inventory, scan queue)
  - All services (hanryxvault, nginx, wireguard)
  - System performance (CPU, RAM, temp, disk)
  - Both websites (hanryxvault.cards, hanryxvault.app)
  - Quick action buttons
  - Live server log tail
"""

import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
import threading
import subprocess
import sqlite3
import datetime
import os
import json
import urllib.request
import time
import platform

# ── Config ────────────────────────────────────────────────────────────────────
DB_PATH      = "/opt/hanryxvault/vault_pos.db"
SERVER_URL   = "http://127.0.0.1:8080"
LOG_PATH     = "/var/log/hanryxvault/error.log"
REFRESH_MS   = 3000   # UI refresh every 3 seconds
LOG_LINES    = 80     # lines to tail in log viewer

GOLD   = "#FFD700"
BG     = "#0d0d0d"
BG2    = "#141414"
BG3    = "#1a1a1a"
BORDER = "#2a2a2a"
GREEN  = "#4caf50"
RED    = "#f44336"
ORANGE = "#ff9800"
GREY   = "#666666"
WHITE  = "#e0e0e0"

WEBSITES = [
    ("hanryxvault.cards", "https://hanryxvault.cards"),
    ("hanryxvault.app",   "https://hanryxvault.app"),
]

SERVICES = [
    ("POS Server",  "hanryxvault"),
    ("nginx",       "nginx"),
    ("WireGuard",   "wg-quick@wg0"),
    ("fail2ban",    "fail2ban"),
    ("PostgreSQL",  "postgresql"),
]


# ── Helpers ───────────────────────────────────────────────────────────────────

def run(cmd):
    try:
        return subprocess.check_output(cmd, shell=True, stderr=subprocess.DEVNULL, timeout=3).decode().strip()
    except Exception:
        return ""


def service_status(name):
    out = run(f"systemctl is-active {name} 2>/dev/null")
    return out == "active"


def cpu_temp():
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return float(f.read().strip()) / 1000
    except Exception:
        return 0.0


def cpu_percent():
    try:
        line = run("top -bn1 | grep 'Cpu(s)'")
        idle = float(line.split(',')[3].split()[0])
        return round(100 - idle, 1)
    except Exception:
        return 0.0


def ram_info():
    try:
        out = run("free -m | grep Mem")
        parts = out.split()
        total = int(parts[1])
        used  = int(parts[2])
        pct   = round(used / total * 100, 1)
        return used, total, pct
    except Exception:
        return 0, 0, 0


def disk_info():
    try:
        out = run("df -h / | tail -1")
        parts = out.split()
        return parts[2], parts[1], parts[4]  # used, total, percent
    except Exception:
        return "?", "?", "?"


def db_stats():
    if not os.path.exists(DB_PATH):
        return {}
    try:
        conn = sqlite3.connect(DB_PATH, timeout=2)
        conn.row_factory = sqlite3.Row

        midnight = int(datetime.datetime.combine(
            datetime.date.today(), datetime.time.min
        ).timestamp() * 1000)

        today = conn.execute("""
            SELECT COUNT(*) as cnt, COALESCE(SUM(total_amount),0) as rev,
                   COALESCE(SUM(tip_amount),0) as tips
            FROM sales WHERE timestamp_ms >= ?
        """, (midnight,)).fetchone()

        total_sales = conn.execute("SELECT COUNT(*) FROM sales").fetchone()[0]
        inv_count   = conn.execute("SELECT COUNT(*) FROM inventory").fetchone()[0]
        low_stock   = conn.execute("SELECT COUNT(*) FROM inventory WHERE stock <= 5").fetchone()[0]
        out_stock   = conn.execute("SELECT COUNT(*) FROM inventory WHERE stock = 0").fetchone()[0]
        pending_scans = conn.execute("SELECT COUNT(*) FROM scan_queue WHERE processed=0").fetchone()[0]

        conn.close()
        return {
            "today_sales": today["cnt"],
            "today_rev":   today["rev"],
            "today_tips":  today["tips"],
            "total_sales": total_sales,
            "inv_count":   inv_count,
            "low_stock":   low_stock,
            "out_stock":   out_stock,
            "pending_scans": pending_scans,
        }
    except Exception:
        return {}


def ping_url(url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "HanryxVault-Monitor/1.0"})
        start = time.time()
        with urllib.request.urlopen(req, timeout=4) as r:
            ms = int((time.time() - start) * 1000)
            return r.status, ms
    except Exception as e:
        return 0, 0


def tail_log():
    if not os.path.exists(LOG_PATH):
        return "Log file not found.\nMake sure the server has run at least once."
    try:
        return run(f"tail -n {LOG_LINES} {LOG_PATH}")
    except Exception:
        return ""


def wg_peers():
    try:
        out = run("wg show wg0 peers 2>/dev/null | wc -l")
        return int(out) if out else 0
    except Exception:
        return 0


# ── Main App ──────────────────────────────────────────────────────────────────

class HanryxMonitor(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("HanryxVault Pi Monitor")
        self.configure(bg=BG)
        self.geometry("1200x780")
        self.minsize(900, 600)

        self._build_ui()
        self._refresh()

    # ── UI Build ──────────────────────────────────────────────────────────────

    def _build_ui(self):
        # Header
        hdr = tk.Frame(self, bg=BG, pady=10)
        hdr.pack(fill="x", padx=16)
        tk.Label(hdr, text="HanryxVault", font=("Helvetica", 22, "bold"),
                 fg=GOLD, bg=BG).pack(side="left")
        tk.Label(hdr, text="  Pi Monitor", font=("Helvetica", 16),
                 fg=GREY, bg=BG).pack(side="left")
        self.lbl_time = tk.Label(hdr, text="", font=("Helvetica", 12),
                                  fg=GREY, bg=BG)
        self.lbl_time.pack(side="right")

        # Notebook tabs
        style = ttk.Style(self)
        style.theme_use("default")
        style.configure("TNotebook",       background=BG, borderwidth=0)
        style.configure("TNotebook.Tab",   background=BG3, foreground=GREY,
                         padding=[16, 8], font=("Helvetica", 11))
        style.map("TNotebook.Tab",
                  background=[("selected", BG2)],
                  foreground=[("selected", GOLD)])
        style.configure("TFrame", background=BG)

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=8, pady=4)

        self.tab_dash    = ttk.Frame(nb)
        self.tab_system  = ttk.Frame(nb)
        self.tab_sites   = ttk.Frame(nb)
        self.tab_logs    = ttk.Frame(nb)

        nb.add(self.tab_dash,   text="  Dashboard  ")
        nb.add(self.tab_system, text="  System     ")
        nb.add(self.tab_sites,  text="  Sites & VPN")
        nb.add(self.tab_logs,   text="  Live Logs  ")

        self._build_dashboard()
        self._build_system()
        self._build_sites()
        self._build_logs()
        self._build_actions()

    def _card(self, parent, row, col, label, value="—", color=GOLD, colspan=1):
        f = tk.Frame(parent, bg=BG3, padx=16, pady=12,
                     highlightbackground=BORDER, highlightthickness=1)
        f.grid(row=row, column=col, columnspan=colspan,
               sticky="nsew", padx=6, pady=6)
        tk.Label(f, text=label.upper(), font=("Helvetica", 9),
                 fg=GREY, bg=BG3, anchor="w").pack(anchor="w")
        lbl = tk.Label(f, text=value, font=("Helvetica", 26, "bold"),
                       fg=color, bg=BG3, anchor="w")
        lbl.pack(anchor="w", pady=(2, 0))
        return lbl

    def _status_row(self, parent, row, name):
        tk.Label(parent, text=name, font=("Helvetica", 12),
                 fg=WHITE, bg=BG3, anchor="w", width=20).grid(
            row=row, column=0, sticky="w", padx=12, pady=6)
        dot = tk.Label(parent, text="●", font=("Helvetica", 14),
                       fg=GREY, bg=BG3)
        dot.grid(row=row, column=1, padx=8, pady=6)
        lbl = tk.Label(parent, text="checking…", font=("Helvetica", 11),
                       fg=GREY, bg=BG3, anchor="w", width=16)
        lbl.grid(row=row, column=2, sticky="w", padx=4, pady=6)
        return dot, lbl

    # ── Dashboard tab ─────────────────────────────────────────────────────────

    def _build_dashboard(self):
        p = self.tab_dash
        p.configure(style="TFrame")

        # Sales cards
        sf = tk.Frame(p, bg=BG)
        sf.pack(fill="x", padx=8, pady=8)
        for i in range(6):
            sf.columnconfigure(i, weight=1)

        self.c_today_sales = self._card(sf, 0, 0, "Today's Sales",   "—", GREEN)
        self.c_today_rev   = self._card(sf, 0, 1, "Revenue Today",   "—", GOLD)
        self.c_today_tips  = self._card(sf, 0, 2, "Tips Today",      "—", GOLD)
        self.c_total_sales = self._card(sf, 0, 3, "All-Time Sales",  "—", WHITE)
        self.c_inv_count   = self._card(sf, 0, 4, "Products",        "—", WHITE)
        self.c_pending     = self._card(sf, 0, 5, "Pending Scans",   "—", ORANGE)

        # Stock alerts
        af = tk.Frame(p, bg=BG3, padx=16, pady=12,
                      highlightbackground=BORDER, highlightthickness=1)
        af.pack(fill="x", padx=14, pady=4)
        tk.Label(af, text="STOCK ALERTS", font=("Helvetica", 9),
                 fg=GREY, bg=BG3).pack(anchor="w")
        row2 = tk.Frame(af, bg=BG3)
        row2.pack(anchor="w", pady=(4, 0))
        self.lbl_low_stock = tk.Label(row2, text="Low (≤5): —",
                                       font=("Helvetica", 13), fg=ORANGE, bg=BG3)
        self.lbl_low_stock.pack(side="left", padx=(0, 24))
        self.lbl_out_stock = tk.Label(row2, text="Out of stock: —",
                                       font=("Helvetica", 13), fg=RED, bg=BG3)
        self.lbl_out_stock.pack(side="left")

        # Services status
        svc_frame = tk.Frame(p, bg=BG3, padx=0, pady=0,
                             highlightbackground=BORDER, highlightthickness=1)
        svc_frame.pack(fill="x", padx=14, pady=8)
        tk.Label(svc_frame, text="SERVICES", font=("Helvetica", 9),
                 fg=GREY, bg=BG3, padx=12, pady=8).grid(
            row=0, column=0, columnspan=3, sticky="w")

        self.svc_dots = {}
        self.svc_lbls = {}
        for i, (name, svc) in enumerate(SERVICES):
            dot, lbl = self._status_row(svc_frame, i + 1, name)
            self.svc_dots[svc] = dot
            self.svc_lbls[svc] = lbl

        # Server ping
        ping_f = tk.Frame(p, bg=BG3, padx=16, pady=10,
                          highlightbackground=BORDER, highlightthickness=1)
        ping_f.pack(fill="x", padx=14, pady=4)
        tk.Label(ping_f, text="SERVER PING", font=("Helvetica", 9),
                 fg=GREY, bg=BG3).pack(anchor="w")
        self.lbl_ping = tk.Label(ping_f, text="—",
                                  font=("Helvetica", 13), fg=GREEN, bg=BG3)
        self.lbl_ping.pack(anchor="w", pady=(4, 0))

    # ── System tab ────────────────────────────────────────────────────────────

    def _build_system(self):
        p = self.tab_system
        for i in range(4):
            p.columnconfigure(i, weight=1)

        self.c_cpu   = self._card(p, 0, 0, "CPU Usage", "—%")
        self.c_temp  = self._card(p, 0, 1, "CPU Temp",  "—°C",
                                   color=ORANGE)
        self.c_ram   = self._card(p, 0, 2, "RAM Used",  "—%")
        self.c_disk  = self._card(p, 0, 3, "Disk Used", "—")

        # Progress bars
        bars = tk.Frame(p, bg=BG)
        bars.grid(row=1, column=0, columnspan=4, sticky="ew", padx=8, pady=4)
        bars.columnconfigure(0, weight=1)

        def bar_row(parent, row, label):
            tk.Label(parent, text=label, font=("Helvetica", 10),
                     fg=GREY, bg=BG, width=8, anchor="e").grid(
                row=row, column=0, padx=(0, 8), pady=4)
            pb = ttk.Progressbar(parent, length=400, maximum=100)
            pb.grid(row=row, column=1, sticky="ew", pady=4)
            lbl = tk.Label(parent, text="", font=("Helvetica", 10),
                           fg=WHITE, bg=BG, width=10, anchor="w")
            lbl.grid(row=row, column=2, padx=8, pady=4)
            return pb, lbl

        bars.columnconfigure(1, weight=1)
        self.pb_cpu,  self.pb_cpu_lbl  = bar_row(bars, 0, "CPU")
        self.pb_ram,  self.pb_ram_lbl  = bar_row(bars, 1, "RAM")
        self.pb_disk, self.pb_disk_lbl = bar_row(bars, 2, "Disk")

        # DB file info
        db_f = tk.Frame(p, bg=BG3, padx=16, pady=12,
                        highlightbackground=BORDER, highlightthickness=1)
        db_f.grid(row=2, column=0, columnspan=4, sticky="ew", padx=14, pady=8)
        tk.Label(db_f, text="DATABASE", font=("Helvetica", 9),
                 fg=GREY, bg=BG3).pack(anchor="w")
        self.lbl_db_info = tk.Label(db_f, text="Loading…",
                                     font=("Helvetica", 12), fg=WHITE, bg=BG3)
        self.lbl_db_info.pack(anchor="w", pady=(4, 0))

    # ── Sites & VPN tab ───────────────────────────────────────────────────────

    def _build_sites(self):
        p = self.tab_sites

        tk.Label(p, text="WEBSITES", font=("Helvetica", 9),
                 fg=GREY, bg=BG, pady=8).pack(anchor="w", padx=14)

        self.site_rows = {}
        sites_f = tk.Frame(p, bg=BG3, padx=8, pady=8,
                           highlightbackground=BORDER, highlightthickness=1)
        sites_f.pack(fill="x", padx=14, pady=4)

        for i, (name, url) in enumerate(WEBSITES):
            row_f = tk.Frame(sites_f, bg=BG3)
            row_f.pack(fill="x", padx=8, pady=6)
            tk.Label(row_f, text=name, font=("Helvetica", 13, "bold"),
                     fg=GOLD, bg=BG3, width=22, anchor="w").pack(side="left")
            dot = tk.Label(row_f, text="●", font=("Helvetica", 14),
                           fg=GREY, bg=BG3)
            dot.pack(side="left", padx=8)
            lbl = tk.Label(row_f, text="checking…", font=("Helvetica", 11),
                           fg=GREY, bg=BG3, width=20, anchor="w")
            lbl.pack(side="left")
            ms_lbl = tk.Label(row_f, text="", font=("Helvetica", 11),
                              fg=GREY, bg=BG3)
            ms_lbl.pack(side="left")
            self.site_rows[name] = (dot, lbl, ms_lbl)

        # VPN
        tk.Label(p, text="WIREGUARD VPN", font=("Helvetica", 9),
                 fg=GREY, bg=BG, pady=8).pack(anchor="w", padx=14)

        vpn_f = tk.Frame(p, bg=BG3, padx=16, pady=12,
                         highlightbackground=BORDER, highlightthickness=1)
        vpn_f.pack(fill="x", padx=14, pady=4)
        row1 = tk.Frame(vpn_f, bg=BG3)
        row1.pack(fill="x")
        tk.Label(row1, text="Status:", font=("Helvetica", 12),
                 fg=GREY, bg=BG3, width=12, anchor="w").pack(side="left")
        self.vpn_dot = tk.Label(row1, text="●", font=("Helvetica", 14),
                                 fg=GREY, bg=BG3)
        self.vpn_dot.pack(side="left", padx=6)
        self.vpn_lbl = tk.Label(row1, text="checking…", font=("Helvetica", 12),
                                 fg=GREY, bg=BG3)
        self.vpn_lbl.pack(side="left")
        row2 = tk.Frame(vpn_f, bg=BG3)
        row2.pack(fill="x", pady=(8, 0))
        tk.Label(row2, text="Connected clients:", font=("Helvetica", 12),
                 fg=GREY, bg=BG3, width=20, anchor="w").pack(side="left")
        self.vpn_peers = tk.Label(row2, text="—", font=("Helvetica", 12, "bold"),
                                   fg=GOLD, bg=BG3)
        self.vpn_peers.pack(side="left")

    # ── Logs tab ──────────────────────────────────────────────────────────────

    def _build_logs(self):
        p = self.tab_logs

        ctrl = tk.Frame(p, bg=BG)
        ctrl.pack(fill="x", padx=8, pady=6)
        tk.Label(ctrl, text="Service:", fg=GREY, bg=BG,
                 font=("Helvetica", 11)).pack(side="left", padx=(0, 6))
        self.log_svc = tk.StringVar(value="hanryxvault")
        for svc, _ in SERVICES[:3]:
            svc_name = [s for n, s in SERVICES if n == svc][0]
            tk.Radiobutton(ctrl, text=svc, variable=self.log_svc,
                           value=svc_name, bg=BG, fg=WHITE,
                           selectcolor=BG3, activebackground=BG,
                           font=("Helvetica", 11),
                           command=self._refresh_logs).pack(side="left", padx=6)
        tk.Button(ctrl, text="Refresh", command=self._refresh_logs,
                  bg=BG3, fg=GOLD, relief="flat", font=("Helvetica", 11),
                  padx=12, pady=4).pack(side="right", padx=4)

        self.log_box = scrolledtext.ScrolledText(
            p, bg="#060606", fg="#c0c0c0",
            font=("Courier", 10), relief="flat",
            wrap="none", state="disabled"
        )
        self.log_box.pack(fill="both", expand=True, padx=8, pady=(0, 8))

    # ── Action bar ────────────────────────────────────────────────────────────

    def _build_actions(self):
        bar = tk.Frame(self, bg=BG2, pady=8,
                       highlightbackground=BORDER, highlightthickness=1)
        bar.pack(fill="x", side="bottom")

        def btn(label, cmd, color=BG3, fg=GOLD):
            tk.Button(bar, text=label, command=cmd,
                      bg=color, fg=fg, relief="flat",
                      font=("Helvetica", 11, "bold"),
                      padx=14, pady=6,
                      cursor="hand2").pack(side="left", padx=6, pady=4)

        btn("Restart Server",    self._restart_server)
        btn("Sync Inventory",    self._sync_inventory)
        btn("Open Admin",        self._open_admin)
        btn("Add VPN Client",    self._add_vpn_client)
        btn("Backup DB",         self._backup_db)
        btn("Quit", self.destroy, color="#1a0000", fg=RED)

    # ── Refresh ───────────────────────────────────────────────────────────────

    def _refresh(self):
        self.lbl_time.config(
            text=datetime.datetime.now().strftime("  %A %d %b %Y  %H:%M:%S")
        )
        threading.Thread(target=self._bg_refresh, daemon=True).start()
        self.after(REFRESH_MS, self._refresh)

    def _bg_refresh(self):
        stats   = db_stats()
        cpu     = cpu_percent()
        temp    = cpu_temp()
        ram_u, ram_t, ram_pct = ram_info()
        disk_u, disk_t, disk_pct = disk_info()

        # Ping POS server
        status_code, ping_ms = ping_url(f"{SERVER_URL}/health")

        # Ping websites (background, don't block)
        site_results = {}
        for name, url in WEBSITES:
            sc, ms = ping_url(url)
            site_results[name] = (sc, ms)

        # VPN
        vpn_on    = service_status("wg-quick@wg0")
        vpn_count = wg_peers()

        # Services
        svc_states = {svc: service_status(svc) for _, svc in SERVICES}

        # DB file size
        db_size = ""
        if os.path.exists(DB_PATH):
            sz = os.path.getsize(DB_PATH) / 1024
            db_size = f"{DB_PATH}   {sz:.1f} KB"
        else:
            db_size = f"{DB_PATH}   (not found)"

        # Schedule UI updates on main thread
        self.after(0, self._update_ui,
                   stats, cpu, temp, ram_u, ram_t, ram_pct,
                   disk_u, disk_t, disk_pct,
                   ping_ms, status_code,
                   site_results, vpn_on, vpn_count, svc_states, db_size)

    def _update_ui(self, stats, cpu, temp, ram_u, ram_t, ram_pct,
                   disk_u, disk_t, disk_pct,
                   ping_ms, status_code,
                   site_results, vpn_on, vpn_count, svc_states, db_size):

        # Dashboard cards
        self.c_today_sales.config(text=str(stats.get("today_sales", "—")))
        self.c_today_rev.config(text=f"${stats.get('today_rev', 0):.2f}")
        self.c_today_tips.config(text=f"${stats.get('today_tips', 0):.2f}")
        self.c_total_sales.config(text=str(stats.get("total_sales", "—")))
        self.c_inv_count.config(text=str(stats.get("inv_count", "—")))

        pending = stats.get("pending_scans", 0)
        self.c_pending.config(text=str(pending),
                               fg=ORANGE if pending > 0 else GREEN)

        low  = stats.get("low_stock",  0)
        out  = stats.get("out_stock",  0)
        self.lbl_low_stock.config(text=f"Low (≤5): {low}",
                                   fg=ORANGE if low > 0 else GREEN)
        self.lbl_out_stock.config(text=f"Out of stock: {out}",
                                   fg=RED if out > 0 else GREEN)

        # Services
        for _, svc in SERVICES:
            on = svc_states.get(svc, False)
            self.svc_dots[svc].config(fg=GREEN if on else RED)
            self.svc_lbls[svc].config(
                text="running" if on else "stopped",
                fg=GREEN if on else RED
            )

        # Server ping
        if ping_ms > 0:
            color = GREEN if ping_ms < 100 else (ORANGE if ping_ms < 300 else RED)
            self.lbl_ping.config(
                text=f"✓  {ping_ms} ms  (HTTP {status_code})", fg=color)
        else:
            self.lbl_ping.config(text="✗  No response", fg=RED)

        # System
        cpu_color = RED if cpu > 80 else (ORANGE if cpu > 60 else GREEN)
        self.c_cpu.config(text=f"{cpu}%", fg=cpu_color)
        temp_color = RED if temp > 75 else (ORANGE if temp > 65 else GREEN)
        self.c_temp.config(text=f"{temp:.1f}°C", fg=temp_color)
        self.c_ram.config(text=f"{ram_pct}%",
                           fg=RED if ram_pct > 85 else WHITE)
        self.c_disk.config(text=f"{disk_u}/{disk_t}")

        self.pb_cpu["value"]  = cpu
        self.pb_cpu_lbl.config(text=f"{cpu}%")
        self.pb_ram["value"]  = ram_pct
        self.pb_ram_lbl.config(text=f"{ram_u}/{ram_t} MB")

        disk_pct_num = int(disk_pct.replace("%", "")) if "%" in str(disk_pct) else 0
        self.pb_disk["value"] = disk_pct_num
        self.pb_disk_lbl.config(text=f"{disk_u}/{disk_t}")

        self.lbl_db_info.config(text=db_size)

        # Sites
        for name, _ in WEBSITES:
            sc, ms = site_results.get(name, (0, 0))
            dot, lbl, ms_lbl = self.site_rows[name]
            if sc in (200, 301, 302):
                dot.config(fg=GREEN)
                lbl.config(text="online", fg=GREEN)
                ms_lbl.config(text=f"  {ms} ms", fg=GREY)
            else:
                dot.config(fg=RED)
                lbl.config(text="offline / unreachable", fg=RED)
                ms_lbl.config(text="")

        # VPN
        self.vpn_dot.config(fg=GREEN if vpn_on else RED)
        self.vpn_lbl.config(text="running" if vpn_on else "stopped",
                             fg=GREEN if vpn_on else RED)
        self.vpn_peers.config(text=str(vpn_count) if vpn_on else "—")

    def _refresh_logs(self):
        svc = self.log_svc.get()
        try:
            out = subprocess.check_output(
                f"journalctl -u {svc} -n {LOG_LINES} --no-pager "
                f"--output=short-iso 2>/dev/null",
                shell=True, stderr=subprocess.DEVNULL, timeout=4
            ).decode()
        except Exception:
            out = tail_log()

        self.log_box.config(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.insert("end", out)
        self.log_box.see("end")
        self.log_box.config(state="disabled")

    # ── Actions ───────────────────────────────────────────────────────────────

    def _run_sudo(self, cmd, success_msg):
        try:
            subprocess.run(f"sudo {cmd}", shell=True, check=True, timeout=10)
            messagebox.showinfo("Done", success_msg)
        except subprocess.CalledProcessError as e:
            messagebox.showerror("Error", str(e))

    def _restart_server(self):
        if messagebox.askyesno("Restart Server",
                               "Restart the HanryxVault POS server?"):
            self._run_sudo("systemctl restart hanryxvault",
                           "Server restarted successfully.")

    def _sync_inventory(self):
        def do_sync():
            try:
                req = urllib.request.Request(
                    f"{SERVER_URL}/admin/sync-from-cloud?force=1",
                    data=b"", method="POST"
                )
                with urllib.request.urlopen(req, timeout=30) as r:
                    result = json.loads(r.read())
                self.after(0, lambda: messagebox.showinfo(
                    "Sync Complete",
                    f"Upserted: {result.get('upserted', 0)} products\n"
                    f"Skipped: {result.get('skipped', 0)}"
                ))
            except Exception as e:
                self.after(0, lambda: messagebox.showerror("Sync Failed", str(e)))
        threading.Thread(target=do_sync, daemon=True).start()

    def _open_admin(self):
        import webbrowser
        webbrowser.open(f"{SERVER_URL}/admin")

    def _add_vpn_client(self):
        win = tk.Toplevel(self, bg=BG)
        win.title("Add VPN Client")
        win.geometry("400x200")
        tk.Label(win, text="Device name:", fg=WHITE, bg=BG,
                 font=("Helvetica", 12)).pack(pady=(20, 4))
        entry = tk.Entry(win, font=("Helvetica", 13), bg=BG3, fg=WHITE,
                         relief="flat", insertbackground=WHITE)
        entry.pack(padx=20, fill="x")
        entry.focus()

        def do_add():
            name = entry.get().strip()
            if not name:
                return
            win.destroy()
            self._run_sudo(
                f"bash /home/pi/pi-setup/scripts/add-vpn-client.sh '{name}'",
                f"VPN client '{name}' added!\nScan the QR code from the terminal."
            )

        tk.Button(win, text="Add Client", command=do_add,
                  bg=GOLD, fg="#000", font=("Helvetica", 12, "bold"),
                  relief="flat", padx=16, pady=8).pack(pady=16)

    def _backup_db(self):
        ts  = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        dst = os.path.expanduser(f"~/vault_pos_backup_{ts}.db")
        try:
            import shutil
            shutil.copy2(DB_PATH, dst)
            messagebox.showinfo("Backup Complete", f"Saved to:\n{dst}")
        except Exception as e:
            messagebox.showerror("Backup Failed", str(e))


# ── Entry ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = HanryxMonitor()
    app.mainloop()
