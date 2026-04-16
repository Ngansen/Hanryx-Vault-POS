#!/usr/bin/env python3
"""
HanryxVault WiFi & Network Manager
Desktop GUI for the trade show Pi — scan networks, connect, and monitor sync.

Run directly:  python3 /opt/hanryxvault/wifi_manager.py
Or via the desktop shortcut installed by install-wifi-manager.sh
"""

import os
import re
import sys
import json
import sqlite3
import subprocess
import threading
import time
import datetime
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog

# ── Paths ──────────────────────────────────────────────────────────────────────
BASE_DIR  = os.environ.get("HANRYXVAULT_DIR", "/opt/hanryxvault")
DB_PATH   = os.path.join(BASE_DIR, "vault_pos.db")
CONF_PATH = os.path.join(BASE_DIR, "satellite.conf")
SYNC_BIN  = os.path.join(BASE_DIR, "venv", "bin", "python3")
SYNC_SCR  = os.path.join(BASE_DIR, "satellite_sync.py")

# ── Colours ────────────────────────────────────────────────────────────────────
BG        = "#1a1a2e"   # deep navy
PANEL     = "#16213e"   # slightly lighter panel
ACCENT    = "#0f3460"   # accent blue
GREEN     = "#00b894"
RED       = "#d63031"
YELLOW    = "#fdcb6e"
TEXT      = "#dfe6e9"
SUBTEXT   = "#636e72"
WHITE     = "#ffffff"
HIGHLIGHT = "#e17055"

FONT_HEAD = ("Inter", 14, "bold")
FONT_BODY = ("Inter", 11)
FONT_SMALL= ("Inter", 9)
FONT_MONO = ("Courier", 10)


# ═══════════════════════════════════════════════════════════════════════════════
#  nmcli helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _run(cmd: list, timeout: int = 15) -> tuple[int, str, str]:
    """Run a command, return (returncode, stdout, stderr)."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"
    except Exception as e:
        return -1, "", str(e)


def get_wifi_networks() -> list[dict]:
    """Return list of visible WiFi networks sorted by signal strength."""
    rc, out, _ = _run([
        "nmcli", "-t",
        "-f", "IN-USE,SSID,SIGNAL,SECURITY,BSSID",
        "device", "wifi", "list", "--rescan", "yes"
    ], timeout=20)

    networks = []
    seen_ssid = set()
    if rc != 0:
        return networks

    for line in out.splitlines():
        parts = line.split(":")
        if len(parts) < 5:
            continue
        in_use   = parts[0].strip() == "*"
        ssid     = ":".join(parts[1:-3]).strip()   # SSID may contain colons
        signal   = parts[-3].strip()
        security = parts[-2].strip()
        # bssid  = parts[-1] (ignored)

        if not ssid or ssid == "--":
            continue
        if ssid in seen_ssid:
            continue
        seen_ssid.add(ssid)

        try:
            sig_pct = int(signal)
        except ValueError:
            sig_pct = 0

        networks.append({
            "ssid":     ssid,
            "signal":   sig_pct,
            "security": security if security and security != "--" else "",
            "in_use":   in_use,
            "bars":     _signal_bars(sig_pct),
            "bar_color":_signal_color(sig_pct),
        })

    networks.sort(key=lambda n: (-n["in_use"], -n["signal"]))
    return networks


def get_active_connection() -> dict | None:
    """Return details about the active WiFi connection, or None."""
    rc, out, _ = _run([
        "nmcli", "-t", "-f", "NAME,DEVICE,TYPE",
        "connection", "show", "--active"
    ])
    if rc != 0:
        return None

    wifi_dev = None
    wifi_name = None
    for line in out.splitlines():
        parts = line.split(":")
        if len(parts) >= 3 and parts[2] == "802-11-wireless":
            wifi_name = parts[0]
            wifi_dev  = parts[1]
            break

    if not wifi_dev:
        return None

    # Get IP and gateway
    _, ip_out, _   = _run(["nmcli", "-g", "IP4.ADDRESS", "device", "show", wifi_dev])
    _, gw_out, _   = _run(["nmcli", "-g", "IP4.GATEWAY", "device", "show", wifi_dev])
    _, dns_out, _  = _run(["nmcli", "-g", "IP4.DNS", "device", "show", wifi_dev])

    return {
        "name":    wifi_name or "",
        "device":  wifi_dev or "",
        "ip":      ip_out.split("\n")[0].split("/")[0] if ip_out else "—",
        "gateway": gw_out.split("\n")[0] if gw_out else "—",
        "dns":     dns_out.split("\n")[0] if dns_out else "—",
    }


def get_active_interface_summary() -> list[dict]:
    """Return all active network interfaces with their type and IP."""
    rc, out, _ = _run([
        "nmcli", "-t", "-f", "DEVICE,TYPE,STATE,IP4.ADDRESS",
        "device", "show"
    ])
    ifaces = []
    current = {}
    for line in out.splitlines():
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip(); val = val.strip()
        if key == "GENERAL.DEVICE":
            if current.get("device") and current.get("state") == "connected":
                ifaces.append(current)
            current = {"device": val}
        elif key == "GENERAL.TYPE":
            current["type"] = val
        elif key == "GENERAL.STATE":
            current["state"] = "connected" if "connected" in val.lower() else val
        elif key == "IP4.ADDRESS[1]":
            current["ip"] = val.split("/")[0]
    if current.get("device") and current.get("state") == "connected":
        ifaces.append(current)
    return ifaces


def connect_wifi(ssid: str, password: str | None) -> tuple[bool, str]:
    """Connect to a WiFi network. Returns (success, message)."""
    cmd = ["sudo", "nmcli", "device", "wifi", "connect", ssid]
    if password:
        cmd += ["password", password]
    rc, out, err = _run(cmd, timeout=30)
    if rc == 0:
        # Set preferred metric immediately
        _run(["sudo", "nmcli", "connection", "modify", ssid,
              "ipv4.route-metric", "50", "ipv6.route-metric", "50"])
        return True, f"Connected to {ssid}"
    return False, err or out or "Connection failed"


def disconnect_wifi(device: str) -> tuple[bool, str]:
    rc, out, err = _run(["sudo", "nmcli", "device", "disconnect", device])
    return rc == 0, err or out


def forget_connection(name: str) -> tuple[bool, str]:
    rc, out, err = _run(["sudo", "nmcli", "connection", "delete", name])
    return rc == 0, err or out


def _signal_bars(pct: int) -> str:
    if pct >= 80: return "▂▄▆█"
    if pct >= 60: return "▂▄▆░"
    if pct >= 40: return "▂▄░░"
    if pct >= 20: return "▂░░░"
    return "░░░░"


def _signal_color(pct: int) -> str:
    if pct >= 70: return GREEN
    if pct >= 45: return YELLOW
    return HIGHLIGHT


# ═══════════════════════════════════════════════════════════════════════════════
#  Sync / DB helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _read_conf() -> dict:
    conf = {"home_pi_url": "http://10.10.0.1:8080"}
    if os.path.exists(CONF_PATH):
        with open(CONF_PATH) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    conf[k.strip()] = v.strip()
    return conf


def get_sync_status() -> dict:
    """Query local DB for pending sale count and last sync time."""
    status = {"pending_sales": 0, "pending_ded": 0, "last_sync": None, "db_ok": False}
    if not os.path.exists(DB_PATH):
        return status
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA busy_timeout=2000")
        row = conn.execute(
            "SELECT value FROM server_state WHERE key='last_satellite_sync'"
        ).fetchone()
        last_ms = int(row[0]) if row else 0
        status["last_sync"] = last_ms

        status["pending_sales"] = conn.execute(
            "SELECT COUNT(*) FROM sales WHERE received_at > ?", (last_ms,)
        ).fetchone()[0]
        status["pending_ded"] = conn.execute(
            "SELECT COUNT(*) FROM stock_deductions WHERE deducted_at > ?", (last_ms,)
        ).fetchone()[0]
        conn.close()
        status["db_ok"] = True
    except Exception:
        pass
    return status


def check_home_pi(url: str) -> bool:
    try:
        import urllib.request
        req = urllib.request.Request(
            f"{url}/health",
            headers={"User-Agent": "HanryxVaultWifiManager/1.0"},
        )
        with urllib.request.urlopen(req, timeout=4) as r:
            return r.status == 200
    except Exception:
        return False


def _ago(ts_ms: int) -> str:
    if not ts_ms:
        return "never"
    diff = time.time() - ts_ms / 1000
    if diff < 60:    return f"{int(diff)}s ago"
    if diff < 3600:  return f"{int(diff/60)}m ago"
    if diff < 86400: return f"{int(diff/3600)}h ago"
    return f"{int(diff/86400)}d ago"


# ═══════════════════════════════════════════════════════════════════════════════
#  Main Application Window
# ═══════════════════════════════════════════════════════════════════════════════

class WifiManagerApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("HanryxVault — WiFi & Network Manager")
        self.configure(bg=BG)
        self.resizable(True, True)
        self.minsize(820, 560)

        # Try to set a fixed startup size
        self.geometry("940x640")

        self._networks     = []
        self._selected     = None
        self._scanning     = False
        self._syncing      = False
        self._conf         = _read_conf()
        self._active_con   = None

        self._build_ui()
        self._start_refresh_loop()
        self.after(200, self._scan_networks)

    # ── UI construction ────────────────────────────────────────────────────────

    def _build_ui(self):
        # ── Title bar ─────────────────────────────────────────────────────────
        title_frame = tk.Frame(self, bg=ACCENT, padx=16, pady=10)
        title_frame.pack(fill=tk.X)

        tk.Label(title_frame, text="HanryxVault", font=("Inter", 16, "bold"),
                 bg=ACCENT, fg=WHITE).pack(side=tk.LEFT)
        tk.Label(title_frame, text="  WiFi & Network Manager",
                 font=("Inter", 13), bg=ACCENT, fg=TEXT).pack(side=tk.LEFT)

        self._status_dot = tk.Label(title_frame, text="●", font=("Inter", 18),
                                    bg=ACCENT, fg=SUBTEXT)
        self._status_dot.pack(side=tk.RIGHT, padx=(0, 4))
        self._status_lbl = tk.Label(title_frame, text="Checking...",
                                    font=FONT_SMALL, bg=ACCENT, fg=TEXT)
        self._status_lbl.pack(side=tk.RIGHT)

        # ── Main two-column layout ─────────────────────────────────────────────
        body = tk.Frame(self, bg=BG)
        body.pack(fill=tk.BOTH, expand=True, padx=12, pady=10)
        body.columnconfigure(0, weight=2, minsize=300)
        body.columnconfigure(1, weight=3)
        body.rowconfigure(0, weight=1)

        self._build_left_panel(body)
        self._build_right_panel(body)

    def _build_left_panel(self, parent):
        frame = tk.Frame(parent, bg=PANEL, bd=0, relief=tk.FLAT)
        frame.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        frame.rowconfigure(1, weight=1)

        # Header
        hdr = tk.Frame(frame, bg=ACCENT, padx=10, pady=8)
        hdr.grid(row=0, column=0, sticky="ew")
        tk.Label(hdr, text="Available Networks", font=FONT_HEAD,
                 bg=ACCENT, fg=WHITE).pack(side=tk.LEFT)
        self._scan_btn = tk.Button(hdr, text="⟳ Scan", font=FONT_SMALL,
                                   bg=BG, fg=TEXT, relief=tk.FLAT, padx=8,
                                   activebackground=HIGHLIGHT,
                                   command=self._scan_networks)
        self._scan_btn.pack(side=tk.RIGHT)

        # Network listbox
        list_frame = tk.Frame(frame, bg=PANEL)
        list_frame.grid(row=1, column=0, sticky="nsew", padx=6, pady=6)
        list_frame.rowconfigure(0, weight=1)
        list_frame.columnconfigure(0, weight=1)

        self._net_list = tk.Listbox(
            list_frame, bg=PANEL, fg=TEXT,
            selectbackground=ACCENT, selectforeground=WHITE,
            font=FONT_BODY, relief=tk.FLAT, bd=0,
            activestyle="none", cursor="hand2",
        )
        scrollbar = tk.Scrollbar(list_frame, command=self._net_list.yview,
                                 bg=BG, troughcolor=BG, relief=tk.FLAT)
        self._net_list.configure(yscrollcommand=scrollbar.set)
        self._net_list.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        self._net_list.bind("<<ListboxSelect>>", self._on_network_select)
        self._net_list.bind("<Double-Button-1>", lambda e: self._connect())

        self._scan_status = tk.Label(frame, text="", font=FONT_SMALL,
                                     bg=PANEL, fg=SUBTEXT)
        self._scan_status.grid(row=2, column=0, pady=(0, 6))

        frame.columnconfigure(0, weight=1)

    def _build_right_panel(self, parent):
        frame = tk.Frame(parent, bg=PANEL)
        frame.grid(row=0, column=1, sticky="nsew")
        frame.rowconfigure(1, weight=1)
        frame.columnconfigure(0, weight=1)

        # ── Connection details ─────────────────────────────────────────────────
        con_hdr = tk.Frame(frame, bg=ACCENT, padx=10, pady=8)
        con_hdr.grid(row=0, column=0, sticky="ew")
        tk.Label(con_hdr, text="Connection Details", font=FONT_HEAD,
                 bg=ACCENT, fg=WHITE).pack(side=tk.LEFT)

        con_body = tk.Frame(frame, bg=PANEL, padx=14, pady=10)
        con_body.grid(row=1, column=0, sticky="nsew")
        con_body.columnconfigure(1, weight=1)

        self._detail_vars = {}
        rows = [
            ("Network",  "ssid"),
            ("Security", "security"),
            ("Device",   "device"),
            ("IP",       "ip"),
            ("Gateway",  "gateway"),
            ("DNS",      "dns"),
        ]
        for i, (label, key) in enumerate(rows):
            tk.Label(con_body, text=label, font=FONT_BODY, bg=PANEL,
                     fg=SUBTEXT, anchor="w", width=10).grid(
                row=i, column=0, sticky="w", pady=3)
            var = tk.StringVar(value="—")
            self._detail_vars[key] = var
            tk.Label(con_body, textvariable=var, font=FONT_BODY,
                     bg=PANEL, fg=TEXT, anchor="w").grid(
                row=i, column=1, sticky="w", padx=(10, 0))

        # Action buttons
        btn_row = tk.Frame(con_body, bg=PANEL)
        btn_row.grid(row=len(rows), column=0, columnspan=2, pady=(14, 4), sticky="w")

        self._connect_btn = self._make_btn(btn_row, "Connect", GREEN,
                                           self._connect, state=tk.DISABLED)
        self._connect_btn.pack(side=tk.LEFT, padx=(0, 8))

        self._disconnect_btn = self._make_btn(btn_row, "Disconnect", YELLOW,
                                              self._disconnect, state=tk.DISABLED)
        self._disconnect_btn.pack(side=tk.LEFT, padx=(0, 8))

        self._forget_btn = self._make_btn(btn_row, "Forget", RED,
                                          self._forget, state=tk.DISABLED)
        self._forget_btn.pack(side=tk.LEFT)

        # ── Divider ───────────────────────────────────────────────────────────
        tk.Frame(frame, bg=ACCENT, height=2).grid(
            row=2, column=0, sticky="ew", pady=0)

        # ── Active interfaces ──────────────────────────────────────────────────
        iface_hdr = tk.Frame(frame, bg=ACCENT, padx=10, pady=6)
        iface_hdr.grid(row=3, column=0, sticky="ew")
        tk.Label(iface_hdr, text="Active Interfaces", font=FONT_HEAD,
                 bg=ACCENT, fg=WHITE).pack(side=tk.LEFT)

        self._iface_frame = tk.Frame(frame, bg=PANEL, padx=14, pady=8)
        self._iface_frame.grid(row=4, column=0, sticky="ew")

        # ── Divider ───────────────────────────────────────────────────────────
        tk.Frame(frame, bg=ACCENT, height=2).grid(
            row=5, column=0, sticky="ew")

        # ── Sync status ───────────────────────────────────────────────────────
        sync_hdr = tk.Frame(frame, bg=ACCENT, padx=10, pady=6)
        sync_hdr.grid(row=6, column=0, sticky="ew")
        tk.Label(sync_hdr, text="Home Pi Sync Status", font=FONT_HEAD,
                 bg=ACCENT, fg=WHITE).pack(side=tk.LEFT)

        sync_body = tk.Frame(frame, bg=PANEL, padx=14, pady=10)
        sync_body.grid(row=7, column=0, sticky="ew")
        sync_body.columnconfigure(1, weight=1)

        self._sync_vars = {}
        sync_rows = [
            ("Home Pi",   "reachable"),
            ("Pending",   "pending"),
            ("Last Sync", "last_sync"),
        ]
        for i, (label, key) in enumerate(sync_rows):
            tk.Label(sync_body, text=label, font=FONT_BODY, bg=PANEL,
                     fg=SUBTEXT, anchor="w", width=10).grid(
                row=i, column=0, sticky="w", pady=3)
            var = tk.StringVar(value="—")
            self._sync_vars[key] = var
            self._sync_lbl = tk.Label(sync_body, textvariable=var, font=FONT_BODY,
                                      bg=PANEL, fg=TEXT, anchor="w")
            self._sync_lbl.grid(row=i, column=1, sticky="w", padx=(10, 0))

        sync_btn_row = tk.Frame(sync_body, bg=PANEL)
        sync_btn_row.grid(row=len(sync_rows), column=0, columnspan=2,
                          pady=(12, 0), sticky="w")
        self._sync_now_btn = self._make_btn(sync_btn_row, "Sync Now", GREEN,
                                            self._sync_now)
        self._sync_now_btn.pack(side=tk.LEFT)

    def _make_btn(self, parent, text, color, command, state=tk.NORMAL):
        return tk.Button(
            parent, text=text, font=FONT_BODY,
            bg=color, fg=WHITE if color not in (YELLOW,) else BG,
            activebackground=BG, activeforeground=TEXT,
            relief=tk.FLAT, padx=14, pady=6,
            cursor="hand2", command=command, state=state,
            bd=0,
        )

    # ── Network list rendering ─────────────────────────────────────────────────

    def _populate_network_list(self):
        self._net_list.delete(0, tk.END)
        for net in self._networks:
            icon = "🔒 " if net["security"] else "   "
            mark = "✓ " if net["in_use"] else "  "
            row  = f"{mark}{net['bars']} {icon}{net['ssid']}  ({net['signal']}%)"
            self._net_list.insert(tk.END, row)

        # Colour each row by signal strength
        for i, net in enumerate(self._networks):
            if net["in_use"]:
                self._net_list.itemconfig(i, fg=GREEN)
            else:
                self._net_list.itemconfig(i, fg=net["bar_color"])

    def _on_network_select(self, event=None):
        sel = self._net_list.curselection()
        if not sel:
            return
        net = self._networks[sel[0]]
        self._selected = net

        # Fill detail panel with selected network info
        active = self._active_con
        if net["in_use"] and active:
            self._detail_vars["ssid"].set(active["name"])
            self._detail_vars["security"].set(net["security"] or "Open")
            self._detail_vars["device"].set(active["device"])
            self._detail_vars["ip"].set(active["ip"])
            self._detail_vars["gateway"].set(active["gateway"])
            self._detail_vars["dns"].set(active["dns"])
        else:
            self._detail_vars["ssid"].set(net["ssid"])
            self._detail_vars["security"].set(net["security"] or "Open")
            self._detail_vars["device"].set("—")
            self._detail_vars["ip"].set("—")
            self._detail_vars["gateway"].set("—")
            self._detail_vars["dns"].set("—")

        # Update button states
        self._connect_btn.config(
            state=tk.DISABLED if net["in_use"] else tk.NORMAL)
        self._disconnect_btn.config(
            state=tk.NORMAL if net["in_use"] else tk.DISABLED)
        self._forget_btn.config(state=tk.NORMAL)

    # ── Actions ───────────────────────────────────────────────────────────────

    def _scan_networks(self):
        if self._scanning:
            return
        self._scanning = True
        self._scan_btn.config(state=tk.DISABLED, text="Scanning...")
        self._scan_status.config(text="Scanning for networks...", fg=YELLOW)

        def _do():
            nets = get_wifi_networks()
            active = get_active_connection()
            self.after(0, self._on_scan_done, nets, active)

        threading.Thread(target=_do, daemon=True).start()

    def _on_scan_done(self, networks, active):
        self._networks   = networks
        self._active_con = active
        self._scanning   = False
        self._scan_btn.config(state=tk.NORMAL, text="⟳ Scan")
        self._populate_network_list()

        count = len(networks)
        now = datetime.datetime.now().strftime("%H:%M:%S")
        self._scan_status.config(
            text=f"{count} network{'s' if count != 1 else ''} found · {now}",
            fg=SUBTEXT)

        # Update title bar status
        if active:
            self._status_dot.config(fg=GREEN)
            self._status_lbl.config(text=f"Connected: {active['name']}")
        else:
            self._status_dot.config(fg=RED)
            self._status_lbl.config(text="Not connected to WiFi")

    def _connect(self):
        if not self._selected:
            return
        net  = self._selected
        ssid = net["ssid"]
        pwd  = None

        if net["security"]:
            pwd = simpledialog.askstring(
                "WiFi Password",
                f'Enter password for "{ssid}":  ',
                show="•",
                parent=self,
            )
            if pwd is None:   # user cancelled
                return

        self._scan_btn.config(state=tk.DISABLED)
        self._connect_btn.config(state=tk.DISABLED, text="Connecting...")
        self._scan_status.config(text=f"Connecting to {ssid}...", fg=YELLOW)

        def _do():
            ok, msg = connect_wifi(ssid, pwd if pwd else None)
            self.after(0, self._on_connect_done, ok, msg, ssid)

        threading.Thread(target=_do, daemon=True).start()

    def _on_connect_done(self, ok, msg, ssid):
        self._scan_btn.config(state=tk.NORMAL)
        self._connect_btn.config(state=tk.NORMAL, text="Connect")
        if ok:
            self._scan_status.config(text=f"Connected to {ssid} ✓", fg=GREEN)
            self._scan_networks()
        else:
            self._scan_status.config(text="Connection failed", fg=RED)
            messagebox.showerror("Connection Failed",
                                 f'Could not connect to "{ssid}":\n\n{msg}',
                                 parent=self)

    def _disconnect(self):
        active = self._active_con
        if not active:
            return
        ok, msg = disconnect_wifi(active["device"])
        if ok:
            self._scan_status.config(text="Disconnected", fg=YELLOW)
            self.after(1500, self._scan_networks)
        else:
            messagebox.showerror("Error", msg, parent=self)

    def _forget(self):
        if not self._selected:
            return
        ssid = self._selected["ssid"]
        if not messagebox.askyesno(
            "Forget Network",
            f'Remove saved password for "{ssid}"?\n\nYou will need to enter it again to reconnect.',
            parent=self,
        ):
            return
        ok, msg = forget_connection(ssid)
        if ok:
            self._scan_status.config(text=f"Forgot {ssid}", fg=YELLOW)
            self._selected = None
            self.after(500, self._scan_networks)
        else:
            messagebox.showerror("Error", msg, parent=self)

    def _sync_now(self):
        if self._syncing:
            return
        self._syncing = True
        self._sync_now_btn.config(state=tk.DISABLED, text="Syncing...")

        def _do():
            ok = False
            # Gracefully handle missing sync infrastructure (satellite-only Pi)
            if not os.path.exists(SYNC_BIN) or not os.path.exists(SYNC_SCR):
                self.after(0, self._on_sync_missing)
                return
            try:
                result = subprocess.run(
                    [SYNC_BIN, SYNC_SCR],
                    capture_output=True, text=True, timeout=60
                )
                ok = result.returncode == 0
            except Exception:
                ok = False
            self.after(0, self._on_sync_done, ok)

        threading.Thread(target=_do, daemon=True).start()

    def _on_sync_missing(self):
        self._syncing = False
        self._sync_now_btn.config(state=tk.NORMAL, text="Sync Now")
        messagebox.showinfo(
            "Sync unavailable",
            "Sync script not installed on this device.\n\n"
            "This Pi is WiFi-only — sales sync runs on the Home Pi.",
            parent=self,
        )

    def _on_sync_done(self, ok):
        self._syncing = False
        self._sync_now_btn.config(state=tk.NORMAL, text="Sync Now")
        self._refresh_sync_panel()

    # ── Periodic refresh ──────────────────────────────────────────────────────

    def _start_refresh_loop(self):
        """Refresh the sync panel and interfaces every 15 seconds."""
        self._refresh_sync_panel()
        self._refresh_iface_panel()
        self.after(15_000, self._start_refresh_loop)

    def _refresh_sync_panel(self):
        status = get_sync_status()
        conf   = _read_conf()
        url    = conf.get("home_pi_url", "http://10.10.0.1:8080")

        def _check():
            reachable = check_home_pi(url)
            self.after(0, self._update_sync_vars, status, reachable)

        threading.Thread(target=_check, daemon=True).start()

    def _update_sync_vars(self, status, reachable):
        if reachable:
            self._sync_vars["reachable"].set("✓ Reachable")
        else:
            self._sync_vars["reachable"].set("✗ Unreachable")

        ps  = status["pending_sales"]
        pd  = status["pending_ded"]
        tot = ps + pd
        if tot == 0:
            self._sync_vars["pending"].set("None — all synced ✓")
        else:
            self._sync_vars["pending"].set(
                f"{ps} sale{'s' if ps != 1 else ''}"
                + (f" + {pd} deduction{'s' if pd != 1 else ''}" if pd else "")
                + " waiting"
            )

        last = status.get("last_sync") or 0
        self._sync_vars["last_sync"].set(_ago(last) if last else "Never")

    def _refresh_iface_panel(self):
        def _check():
            ifaces = get_active_interface_summary()
            self.after(0, self._update_iface_panel, ifaces)
        threading.Thread(target=_check, daemon=True).start()

    def _update_iface_panel(self, ifaces):
        for w in self._iface_frame.winfo_children():
            w.destroy()

        if not ifaces:
            tk.Label(self._iface_frame, text="No active interfaces",
                     font=FONT_BODY, bg=PANEL, fg=SUBTEXT).pack(anchor="w")
            return

        type_labels = {
            "802-11-wireless": "WiFi",
            "802-3-ethernet":  "Ethernet / USB",
            "wireguard":       "WireGuard VPN",
            "tun":             "VPN Tunnel",
        }
        priority_icons = {
            "802-11-wireless": "★",
            "802-3-ethernet":  "↕",
            "wireguard":       "🔒",
        }

        for iface in ifaces:
            t    = iface.get("type", "")
            lbl  = type_labels.get(t, t.replace("-", " ").title())
            icon = priority_icons.get(t, "•")
            ip   = iface.get("ip", "—")
            dev  = iface.get("device", "")
            color = GREEN if t == "802-11-wireless" else TEXT

            row = tk.Frame(self._iface_frame, bg=PANEL)
            row.pack(fill=tk.X, pady=2)
            tk.Label(row, text=f"{icon} {lbl}", font=FONT_BODY,
                     bg=PANEL, fg=color, width=22, anchor="w").pack(side=tk.LEFT)
            tk.Label(row, text=f"{dev}  {ip}", font=FONT_MONO,
                     bg=PANEL, fg=SUBTEXT).pack(side=tk.LEFT)


# ═══════════════════════════════════════════════════════════════════════════════
#  Entry point
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    app = WifiManagerApp()
    app.mainloop()
