#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# HanryxVault Kiosk — YouTube playlist downloader
# Downloads (and incrementally updates) the kiosk idle-screen playlist into
# /opt/hanryxvault/kiosk/videos/ as MP4 files.  Run by the
# hanryxvault-kiosk-videos.timer once a week, or manually any time.
#
# Configure the playlist by editing /etc/default/hanryxvault-kiosk and setting
#   KIOSK_PLAYLIST_URL=https://www.youtube.com/playlist?list=PLxxxxxxxx
# (a full video URL with &list=… also works).
#
# The downloader uses --download-archive so videos already on disk are skipped
# on subsequent runs — only new playlist additions are fetched.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

# Load config (KIOSK_PLAYLIST_URL, KIOSK_VIDEOS_DIR, KIOSK_VIDEO_HEIGHT)
if [ -r /etc/default/hanryxvault-kiosk ]; then
    # shellcheck disable=SC1091
    . /etc/default/hanryxvault-kiosk
fi

PLAYLIST_URL="${KIOSK_PLAYLIST_URL:-https://www.youtube.com/playlist?list=PLo60BvbiWBuqUwSRFou3pbPV2IAWaP0rg}"
VIDEOS_DIR="${KIOSK_VIDEOS_DIR:-/opt/hanryxvault/kiosk/videos}"
MAX_HEIGHT="${KIOSK_VIDEO_HEIGHT:-480}"
MAX_COUNT="${KIOSK_VIDEO_MAX_COUNT:-40}"
ARCHIVE="$VIDEOS_DIR/.yt-dlp-archive.txt"

mkdir -p "$VIDEOS_DIR"

if ! command -v yt-dlp >/dev/null 2>&1; then
    echo "[playlist] yt-dlp is not installed.  Install with:" >&2
    echo "           sudo apt-get install -y yt-dlp ffmpeg" >&2
    echo "  or:      sudo pip3 install --break-system-packages -U yt-dlp" >&2
    exit 1
fi

echo "[playlist] Source : $PLAYLIST_URL"
echo "[playlist] Target : $VIDEOS_DIR  (≤ ${MAX_HEIGHT}p, mp4, max ${MAX_COUNT} clips)"

# Build optional --playlist-end flag (0 = unlimited)
END_FLAG=()
if [ "$MAX_COUNT" -gt 0 ] 2>/dev/null; then
    END_FLAG=(--playlist-end "$MAX_COUNT")
fi

# -f         : prefer pre-muxed mp4 ≤ MAX_HEIGHT, fall back to bestvideo+bestaudio
# --merge-output-format mp4 : remux if we had to fetch separate tracks
# --download-archive : skip any video ID already recorded
# --ignore-errors    : keep going if one video is private/region-locked
# --yes-playlist     : force playlist mode even if URL also has v=...
# --no-overwrites    : safety
# --restrict-filenames : ASCII-only filenames so the web player URL is clean
# --playlist-end N   : cap to first N items (saves SD-card space)
yt-dlp \
    -f "bv*[height<=${MAX_HEIGHT}][ext=mp4]+ba[ext=m4a]/b[height<=${MAX_HEIGHT}][ext=mp4]/b[height<=${MAX_HEIGHT}]" \
    --merge-output-format mp4 \
    --download-archive "$ARCHIVE" \
    --ignore-errors \
    --yes-playlist \
    --no-overwrites \
    --restrict-filenames \
    "${END_FLAG[@]}" \
    --output "$VIDEOS_DIR/%(playlist_index)03d-%(id)s-%(title).80s.%(ext)s" \
    "$PLAYLIST_URL"

# Tidy: remove any non-mp4 leftovers (e.g. partial .webm/.m4a if a merge failed)
find "$VIDEOS_DIR" -maxdepth 1 -type f \
    \! -name '*.mp4' \! -name '.yt-dlp-archive.txt' \
    -print -delete || true

count=$(find "$VIDEOS_DIR" -maxdepth 1 -name '*.mp4' | wc -l)
total_size=$(du -sh "$VIDEOS_DIR" | cut -f1)
echo "[playlist] Done.  $count videos, $total_size on disk."
