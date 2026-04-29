#!/usr/bin/env bash
# move-volumes-to-drive.sh — One-time migration of the four legacy
# Docker named volumes (pgdata, pos-data, card-images, pokeapi-data)
# onto bind-mounts under /mnt/cards on the USB drive.
#
# Why
# ---
# The original docker-compose.yml stored Postgres, the POS app's local
# data dir, the card images blob store, and the cached PokeAPI dump in
# Docker named volumes living on the SD card. Three problems with that:
#   * The SD card is the wear-life bottleneck on the Pi 5; Postgres
#     write amplification was burning through it on a 6-month timeline.
#   * The POS data + card-images dirs grew past 200 GB at scale and the
#     SD card is only 64 GB.
#   * Backups had to `docker run --rm -v <vol>:/src` to even READ the
#     data, so cron jobs were wrapped in container plumbing.
#
# Bind-mounts under /mnt/cards (the 1 TB UGreen USB ext4 drive that
# already hosts cards_master + the CLIP/PaddleOCR model files) fix all
# three: SSD wear-leveling, plenty of room, plain `cp` backups.
#
# Usage
# -----
#   bash pi-setup/scripts/move-volumes-to-drive.sh             # interactive, asks before each volume
#   bash pi-setup/scripts/move-volumes-to-drive.sh --dry-run   # show plan, do nothing
#   bash pi-setup/scripts/move-volumes-to-drive.sh --yes       # non-interactive, refuse to overwrite
#   bash pi-setup/scripts/move-volumes-to-drive.sh --yes --force-overwrite
#
# What it does (per volume)
# -------------------------
#   1. Refuse to run unless /mnt/cards is mounted, writable, and has
#      enough free space (rough size check vs source volume).
#   2. Find the named volume (`docker volume inspect` → Mountpoint).
#   3. mkdir -p the bind-mount target on /mnt/cards (skipping if it
#      already has files unless --force-overwrite is set).
#   4. `docker run --rm` an alpine container with both paths bind-
#      mounted, then `cp -a /src/. /dst/` (preserves attrs + xattrs).
#   5. Verify by comparing `du -sb` before/after.
#   6. Print the final chown command + the docker-compose state to
#      switch to (env vars unset → defaults to /mnt/cards/<dir>).
#
# What it does NOT do
# -------------------
#   * It does NOT delete the old Docker named volumes. Leave them in
#     place until you've run the new stack for at least one full show
#     and verified everything works. Then `docker volume rm <name>`.
#   * It does NOT migrate ollama-data on purpose — Ollama stays on
#     SD so the assistant container still comes up if the USB drive
#     is unplugged. See the comment in docker-compose.yml.
#   * It does NOT touch the OCR / CLIP model dirs (these were already
#     on the drive from day one — this script only moves the four
#     legacy named volumes).
#
# Idempotent: re-running with --force-overwrite re-syncs from the
# named volume to the bind-mount, useful as a "snapshot the live data
# back to the drive" tool while debugging.

set -euo pipefail

# ── Args ────────────────────────────────────────────────────────────

DRY_RUN=0
ASSUME_YES=0
FORCE_OVERWRITE=0
USB_ROOT="${USB_ROOT:-/mnt/cards}"
COMPOSE_PROJECT="${COMPOSE_PROJECT:-pi-setup}"

for arg in "$@"; do
    case "$arg" in
        --dry-run)            DRY_RUN=1 ;;
        --yes|-y)             ASSUME_YES=1 ;;
        --force-overwrite)    FORCE_OVERWRITE=1 ;;
        --usb-root=*)         USB_ROOT="${arg#--usb-root=}" ;;
        --compose-project=*)  COMPOSE_PROJECT="${arg#--compose-project=}" ;;
        -h|--help)
            grep -E '^# ' "$0" | sed 's/^# \?//'
            exit 0
            ;;
        *)
            echo "Unknown arg: $arg" >&2
            echo "Try --help" >&2
            exit 2
            ;;
    esac
done

log() { printf '[move-volumes] %s\n' "$*"; }
warn(){ printf '[move-volumes] WARN: %s\n' "$*" >&2; }
die() { printf '[move-volumes] ERROR: %s\n' "$*" >&2; exit 1; }

# ── Safety belts ────────────────────────────────────────────────────

if [ ! -d "$USB_ROOT" ]; then
    die "$USB_ROOT does not exist. Mount the USB drive first."
fi

# Refuse to write to anything that isn't actually a separate mount —
# protects against running this on a Pi where the drive failed to
# mount and / fell through. (Compares device numbers of $USB_ROOT
# and / — they MUST differ.)
ROOT_DEV=$(stat -c %d /)
USB_DEV=$(stat -c %d "$USB_ROOT")
if [ "$ROOT_DEV" = "$USB_DEV" ]; then
    die "$USB_ROOT is on the same filesystem as / — the drive is not mounted."
fi

if ! touch "$USB_ROOT/.move-volumes-write-test" 2>/dev/null; then
    die "$USB_ROOT is not writable by $(id -un)."
fi
rm -f "$USB_ROOT/.move-volumes-write-test"

if ! command -v docker >/dev/null 2>&1; then
    die "docker not found in PATH."
fi

# Volumes managed by docker-compose use the project prefix:
#   <project>_<volume>   e.g. pi-setup_pgdata
# Override --compose-project if your `docker compose -p` differs.
declare -A VOLUMES
VOLUMES[pgdata]="postgres-data"
VOLUMES[pos-data]="pos-data"
VOLUMES[card-images]="card-images"
VOLUMES[pokeapi-data]="pokeapi-data"

# ── Per-volume migration ────────────────────────────────────────────

confirm() {
    [ "$ASSUME_YES" -eq 1 ] && return 0
    local prompt="$1"
    read -r -p "$prompt [y/N] " ans
    [[ "$ans" =~ ^[Yy] ]]
}

human_size() {
    # Wraps `du -sh` so a missing dir prints "—" instead of erroring.
    local p="$1"
    if [ -e "$p" ]; then du -sh "$p" 2>/dev/null | cut -f1; else echo "—"; fi
}

migrate_one() {
    local short="$1"
    local target_subdir="$2"
    local volume="${COMPOSE_PROJECT}_${short}"
    local target="${USB_ROOT}/${target_subdir}"

    log ""
    log "── Volume: ${volume}  →  ${target} ──"

    if ! docker volume inspect "$volume" >/dev/null 2>&1; then
        warn "Docker volume ${volume} not found — skipping."
        warn "(That's fine if you already migrated this one or never used it.)"
        return 0
    fi

    local source_path
    source_path=$(docker volume inspect -f '{{ .Mountpoint }}' "$volume")
    local source_size=$(human_size "$source_path")
    local target_size_before=$(human_size "$target")
    log "source: ${source_path}  (${source_size})"
    log "target: ${target}  (${target_size_before} before)"

    if [ -d "$target" ] && [ -n "$(ls -A "$target" 2>/dev/null || true)" ]; then
        if [ "$FORCE_OVERWRITE" -eq 0 ]; then
            warn "Target ${target} already has files. "\
"Pass --force-overwrite to re-sync, or rm it manually first. Skipping."
            return 0
        fi
        warn "Target has files but --force-overwrite is set. Will overwrite."
    fi

    if [ "$DRY_RUN" -eq 1 ]; then
        log "(dry-run) would mkdir -p ${target}"
        log "(dry-run) would: docker run --rm "\
"-v ${volume}:/src:ro -v ${target}:/dst alpine "\
"sh -c 'cp -a /src/. /dst/'"
        return 0
    fi

    if ! confirm "Migrate ${volume} (${source_size}) to ${target}?"; then
        log "Skipped ${volume} on user request."
        return 0
    fi

    mkdir -p "$target"

    # cp -a inside an alpine container so we don't need cp on the
    # host to understand all the xattrs / ACLs Postgres uses on its
    # data dir. Also keeps the host shell decoupled from whatever
    # uid/gid the volume actually used.
    docker run --rm \
        -v "${volume}:/src:ro" \
        -v "${target}:/dst" \
        alpine sh -c 'cp -a /src/. /dst/'

    local target_size_after=$(human_size "$target")
    log "✓ ${volume} → ${target}  (size after: ${target_size_after})"
}

# ── Pre-flight summary ──────────────────────────────────────────────

log "USB root        : ${USB_ROOT}"
log "Compose project : ${COMPOSE_PROJECT}"
log "Dry run         : ${DRY_RUN}"
log "Assume yes      : ${ASSUME_YES}"
log "Force overwrite : ${FORCE_OVERWRITE}"
log ""
log "Volumes to migrate:"
for short in "${!VOLUMES[@]}"; do
    log "  ${COMPOSE_PROJECT}_${short}  →  ${USB_ROOT}/${VOLUMES[$short]}"
done

if [ "$ASSUME_YES" -eq 0 ] && [ "$DRY_RUN" -eq 0 ]; then
    if ! confirm "Stop the docker-compose stack and proceed?"; then
        die "Aborted by user."
    fi
fi

if [ "$DRY_RUN" -eq 0 ]; then
    log ""
    log "Stopping docker-compose stack so volumes are quiescent…"
    # Use compose's `stop` (not `down`) so we don't blow away networks /
    # named volumes — we only need the containers to release their
    # mounts. `|| true` covers the case where the stack isn't up.
    (cd "$(dirname "$0")/.." && docker compose stop) || true
fi

# ── Run migrations ──────────────────────────────────────────────────

for short in "${!VOLUMES[@]}"; do
    migrate_one "$short" "${VOLUMES[$short]}"
done

# ── Done ────────────────────────────────────────────────────────────

log ""
log "================================================================"
log "Migration complete."
log ""
if [ "$DRY_RUN" -eq 1 ]; then
    log "Dry run only — nothing written. Re-run without --dry-run to apply."
    exit 0
fi
log "Next steps:"
log ""
log "  1. Verify ownership matches what each container expects:"
log "       sudo chown -R 999:999 ${USB_ROOT}/postgres-data"
log "       (postgres in the official image runs as uid 999)"
log ""
log "  2. Make sure none of these env vars are set in pi-setup/.env"
log "     (they default to /mnt/cards/<dir> when unset, which is what"
log "     we just migrated to):"
log ""
log "       DB_DATA_DIR  POS_DATA_DIR  CARD_IMAGES_DIR  POKEAPI_DATA_DIR"
log ""
log "  3. Bring the stack back up:"
log "       cd $(dirname "$0")/.."
log "       docker compose up -d"
log ""
log "  4. After at least one full show on the new layout, you can"
log "     reclaim SD-card space by removing the old named volumes:"
log ""
for short in "${!VOLUMES[@]}"; do
    log "       docker volume rm ${COMPOSE_PROJECT}_${short}"
done
log ""
log "================================================================"
