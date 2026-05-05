#!/usr/bin/env bash
# =============================================================================
# update-satellite-target.sh — point the node-kiosk Prometheus job at a new IP
#
# Run on the MAIN Pi after the satellite Pi has been (re)provisioned.
#
# Usage:
#   bash update-satellite-target.sh <new-ip>          # IP only, port stays 9100
#   bash update-satellite-target.sh 100.x.x.x:9100    # full target
#
# Example:
#   bash update-satellite-target.sh 100.99.123.45
#
# Then reload Prometheus without restarting:
#   docker kill -s HUP prometheus
# =============================================================================
set -euo pipefail

NEW="${1:-}"
if [ -z "$NEW" ]; then
    echo "Usage: $0 <satellite-ip-or-host[:port]>" >&2
    exit 1
fi

# Append :9100 if no port given
if [[ "$NEW" != *:* ]]; then
    NEW="${NEW}:9100"
fi

PROM_FILE="$(dirname "$0")/prometheus.yml"
if [ ! -f "$PROM_FILE" ]; then
    echo "[!] prometheus.yml not found at $PROM_FILE" >&2
    exit 1
fi

BACKUP="${PROM_FILE}.bak.$(date +%s)"
cp "$PROM_FILE" "$BACKUP"
echo "[i] Backup → $BACKUP"

# Replace the targets line inside the node-kiosk job. Match any host:port
# value between single quotes that follows a "node-kiosk" job_name within
# ~10 lines (sed range).
python3 - "$PROM_FILE" "$NEW" <<'PY'
import re, sys, pathlib
path, new = sys.argv[1], sys.argv[2]
text = pathlib.Path(path).read_text()

# Find the node-kiosk block then rewrite the first targets line within it
pattern = re.compile(
    r"(- job_name:\s*['\"]?node-kiosk['\"]?.*?targets:\s*\[\s*')"
    r"[^']+"
    r"('\s*\])",
    re.DOTALL,
)
new_text, n = pattern.subn(rf"\g<1>{new}\g<2>", text)
if n == 0:
    print("[!] node-kiosk job not found — no changes made", file=sys.stderr)
    sys.exit(2)
pathlib.Path(path).write_text(new_text)
print(f"[ok] Rewrote node-kiosk target → {new}")
PY

echo ""
echo "[i] Diff:"
diff -u "$BACKUP" "$PROM_FILE" || true

echo ""
echo "[i] Reload Prometheus to pick up the change:"
echo "    docker kill -s HUP prometheus"
