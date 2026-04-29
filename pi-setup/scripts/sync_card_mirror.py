#!/usr/bin/env python3
"""
sync_card_mirror.py — populate the offline card mirror on the USB drive.

The kiosk needs to keep working without WiFi at trade shows. This script
builds a complete offline image + data mirror of every Pokémon TCG source
the POS knows about, into a single USB-drive directory tree that
unified/local_images.py knows how to look up.

Three phases (run all of them on a fresh drive, run any subset later):

  Phase A  git clone (or git pull) every Ngansen image-bearing fork into
           <ROOT>/<repo-name>/. Idempotent — re-run is just `git pull`.
  Phase B  Walk Korean card-data JSONs for `cardImgURL` and download each
           image into <ROOT>/ptcg-kr-db/card_img/<basename>. The KR fork
           ships JSON only — the image folder is intentionally empty in
           git so we keep the repo small.
  Phase C  Walk cards_master.image_url_alt for every candidate where
           local == "" and the URL is HTTP/HTTPS, download into
           <ROOT>/cdn/<host><path>. Covers JP CDN images (jp_pokell,
           jp_pcc), TCGdex art, and any other future source.

Idempotent + resumable: every download checks for the file first and skips
if size > 0. Ctrl-C is safe — partial files are deleted. Failed downloads
are logged but never abort the whole run.

Usage:
    MIRROR_ROOT=/mnt/ugreen/cards \\
    DATABASE_URL=postgresql://hanryx:...@db:5432/hanryx \\
        python3 sync_card_mirror.py [--phase A|B|C|all] [--limit N]

Env vars:
    MIRROR_ROOT   Root of the USB mirror (default /mnt/cards). Must already
                  exist and be writable. The script never creates the mount.
    DATABASE_URL  Postgres URL — only required for Phase C.
    GH_TOKEN      Optional GitHub token to avoid rate limits during clone.

Disk budget on a fresh sync (rough):
    Phase A     ~7 GB    (CHS + Pocket repos dominate)
    Phase B     ~1-3 GB  (Korean card images, ~1700 files)
    Phase C    ~5-25 GB  (depends on how many JP/EN candidates exist)
    Total      ~15-35 GB on 1 TB drive — plenty of headroom.

Run nightly via cron on the Pi while WiFi is available; the kiosk will
pick up the new local files on the next request (the /card/image
resolver re-checks disk on every hit, no consolidator restart needed).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import unquote, urlparse

# urllib over requests so the script has zero pip dependencies — runs on
# the bare Pi host without needing the docker container's venv.
import urllib.error
import urllib.request

log = logging.getLogger("sync_card_mirror")

MIRROR_ROOT = Path(os.environ.get("MIRROR_ROOT", "/mnt/cards"))

# (owner/repo, default-branch) — every Ngansen fork that contains card
# data or images. Order matters: small metadata-only repos first so users
# see progress quickly, then the big image repos.
REPOS: list[tuple[str, str]] = [
    ("Ngansen/pokemon-card-jp-database",  "main"),
    ("Ngansen/pokemon-tcg-tracker",       "master"),
    ("Ngansen/pokemon-tcg-pocket-database", "main"),
    ("Ngansen/cards-database",            "master"),   # TCGdex
    ("Ngansen/ptcg-kr-db",                "main"),
    ("Ngansen/pokemon-tcg-pocket-cards",  "main"),     # ~1 GB
    ("Ngansen/PTCG-CHS-Datasets",         "main"),     # ~5.7 GB
]

USER_AGENT = "HanryxVault-mirror/1.0 (+https://github.com/Ngansen)"

# ── interrupt handling ────────────────────────────────────────────────
_interrupted = False
def _on_sigint(_sig, _frame):
    global _interrupted
    _interrupted = True
    log.warning("[sync] Interrupt received — finishing current file then exiting…")
signal.signal(signal.SIGINT, _on_sigint)


# ── small utilities ───────────────────────────────────────────────────

def _human_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _dir_size(p: Path) -> int:
    if not p.is_dir():
        return 0
    total = 0
    try:
        for root, _dirs, files in os.walk(p):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
    except OSError:
        pass
    return total


def _safe_run(cmd: list[str], cwd: Optional[Path] = None) -> tuple[int, str]:
    """Run a subprocess, return (rc, combined output). Never raises."""
    try:
        out = subprocess.run(
            cmd, cwd=cwd, check=False, capture_output=True, text=True,
        )
        return out.returncode, (out.stdout or "") + (out.stderr or "")
    except FileNotFoundError as e:
        return 127, f"command not found: {e}"


def _download(url: str, dest: Path, *, timeout: int = 30,
              min_size: int = 256) -> tuple[bool, str]:
    """
    Download `url` to `dest`. Returns (ok, status). Atomic: writes to
    dest.tmp first, fsyncs, renames. Files smaller than `min_size` bytes
    are treated as failed (CDNs sometimes return 1-byte error stubs).
    """
    if dest.exists() and dest.stat().st_size >= min_size:
        return True, "skip-exists"
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if r.status >= 400:
                return False, f"http-{r.status}"
            with open(tmp, "wb") as f:
                shutil.copyfileobj(r, f, length=64 * 1024)
                f.flush()
                os.fsync(f.fileno())
        if tmp.stat().st_size < min_size:
            tmp.unlink(missing_ok=True)
            return False, "too-small"
        os.replace(tmp, dest)
        return True, "ok"
    except urllib.error.HTTPError as e:
        tmp.unlink(missing_ok=True)
        return False, f"http-{e.code}"
    except Exception as e:
        tmp.unlink(missing_ok=True)
        return False, f"err-{type(e).__name__}"


# ── Phase A: git clone/pull ───────────────────────────────────────────

def phase_a() -> None:
    log.info("=" * 60)
    log.info("Phase A — sync %d Ngansen forks into %s", len(REPOS), MIRROR_ROOT)
    log.info("=" * 60)
    if not MIRROR_ROOT.is_dir():
        log.error("MIRROR_ROOT does not exist: %s", MIRROR_ROOT)
        sys.exit(2)

    rc, _ = _safe_run(["git", "--version"])
    if rc != 0:
        log.error("git not installed on this host — install with `sudo apt install git`")
        sys.exit(2)

    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    for slug, branch in REPOS:
        if _interrupted:
            log.warning("[A] interrupted before %s", slug)
            break
        owner, name = slug.split("/", 1)
        dest = MIRROR_ROOT / name
        url = (f"https://{token}@github.com/{slug}.git"
               if token else f"https://github.com/{slug}.git")
        t0 = time.time()
        if (dest / ".git").is_dir():
            log.info("[A] pull  %s", slug)
            rc, out = _safe_run(["git", "-C", str(dest), "pull",
                                 "--ff-only", "--quiet"])
        else:
            log.info("[A] clone %s → %s", slug, dest.name)
            rc, out = _safe_run(["git", "clone", "--depth=1",
                                 "--branch", branch, "--quiet",
                                 url, str(dest)])
        size = _dir_size(dest)
        if rc == 0:
            log.info("[A] ok    %s (%s, %.1fs)",
                     slug, _human_bytes(size), time.time() - t0)
        else:
            log.error("[A] FAIL  %s rc=%d\n%s", slug, rc, out.strip()[-400:])


# ── Phase B: KR images via cardImgURL walk ────────────────────────────

def _iter_kr_card_urls(kr_root: Path) -> Iterable[tuple[Path, str]]:
    """Yield (json_path, cardImgURL) for every KR card JSON that has one."""
    for json_path in kr_root.rglob("*.json"):
        try:
            with open(json_path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue
        url = (data.get("cardImgURL") or data.get("card_img_url")
               or data.get("imageUrl") or "").strip()
        if url and url.startswith(("http://", "https://")):
            yield json_path, url


def phase_b() -> None:
    log.info("=" * 60)
    log.info("Phase B — download KR card images via cardImgURL")
    log.info("=" * 60)
    kr_root = MIRROR_ROOT / "ptcg-kr-db"
    if not kr_root.is_dir():
        log.error("KR repo not cloned yet — run Phase A first")
        return
    img_root = kr_root / "card_img"
    img_root.mkdir(exist_ok=True)

    n_total = n_ok = n_skip = n_fail = 0
    t0 = time.time()
    for json_path, url in _iter_kr_card_urls(kr_root):
        if _interrupted:
            log.warning("[B] interrupted at #%d", n_total)
            break
        n_total += 1
        base = os.path.basename(unquote(urlparse(url).path)) or f"unknown-{n_total}.jpg"
        dest = img_root / base
        ok, status = _download(url, dest)
        if ok and status == "skip-exists":
            n_skip += 1
        elif ok:
            n_ok += 1
            if n_ok % 50 == 0:
                log.info("[B] %d new, %d skipped, %d failed (%.1fs)",
                         n_ok, n_skip, n_fail, time.time() - t0)
        else:
            n_fail += 1
            log.debug("[B] fail %s → %s (%s)", json_path.name, base, status)

    log.info("[B] done — %d total, %d new, %d skipped, %d failed (%s on disk)",
             n_total, n_ok, n_skip, n_fail, _human_bytes(_dir_size(img_root)))


# ── Phase C: CDN mirror from cards_master ─────────────────────────────

def _iter_cdn_candidates(database_url: str, limit: Optional[int]) -> Iterable[tuple[str, str]]:
    """
    Yield (source_id, url) for every cards_master image_url_alt candidate
    where local == "" and url is http(s). De-duped by URL.
    """
    import psycopg2  # lazy — Phase A/B don't need postgres
    seen: set[str] = set()
    yielded = 0
    conn = psycopg2.connect(database_url)
    try:
        cur = conn.cursor(name="cdn_walk")  # server-side cursor
        cur.itersize = 1000
        cur.execute("SELECT image_url_alt FROM cards_master "
                    "WHERE image_url_alt::text <> '[]'")
        for (alt,) in cur:
            if isinstance(alt, str):
                try:    alt = json.loads(alt)
                except: continue
            if not isinstance(alt, list):
                continue
            for c in alt:
                if not isinstance(c, dict):
                    continue
                if (c.get("local") or "").strip():
                    continue   # already mirrored elsewhere
                url = (c.get("url") or "").strip()
                if not url.startswith(("http://", "https://")):
                    continue
                if url in seen:
                    continue
                seen.add(url)
                yield (c.get("src") or "?", url)
                yielded += 1
                if limit and yielded >= limit:
                    return
    finally:
        conn.close()


def _cdn_dest(url: str) -> Path:
    """<MIRROR_ROOT>/cdn/<host>/<path>. Falls back to a sha256 leaf when
    the URL has no usable path (rare CDN edge case)."""
    p = urlparse(url)
    host = p.netloc.lower() or "unknown"
    rel = unquote(p.path).lstrip("/")
    if not rel:
        rel = hashlib.sha256(url.encode()).hexdigest()[:32] + ".bin"
    return MIRROR_ROOT / "cdn" / host / rel


def phase_c(limit: Optional[int]) -> None:
    log.info("=" * 60)
    log.info("Phase C — mirror cards_master CDN URLs (limit=%s)", limit)
    log.info("=" * 60)
    db_url = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")
    if not db_url:
        log.error("DATABASE_URL not set — skipping Phase C")
        return
    cdn_root = MIRROR_ROOT / "cdn"
    cdn_root.mkdir(parents=True, exist_ok=True)

    n_total = n_ok = n_skip = n_fail = 0
    by_src: dict[str, int] = {}
    t0 = time.time()
    for src, url in _iter_cdn_candidates(db_url, limit):
        if _interrupted:
            log.warning("[C] interrupted at #%d", n_total)
            break
        n_total += 1
        dest = _cdn_dest(url)
        ok, status = _download(url, dest)
        if ok and status == "skip-exists":
            n_skip += 1
        elif ok:
            n_ok += 1
            by_src[src] = by_src.get(src, 0) + 1
            if n_ok % 100 == 0:
                log.info("[C] %d new, %d skipped, %d failed (%.1fs)",
                         n_ok, n_skip, n_fail, time.time() - t0)
        else:
            n_fail += 1
            log.debug("[C] fail src=%s %s (%s)", src, url, status)

    log.info("[C] done — %d total, %d new, %d skipped, %d failed (%s on disk)",
             n_total, n_ok, n_skip, n_fail, _human_bytes(_dir_size(cdn_root)))
    if by_src:
        log.info("[C] new files by source: %s",
                 ", ".join(f"{k}={v}" for k, v in sorted(by_src.items())))


# ── main ──────────────────────────────────────────────────────────────

def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--phase", choices=["A", "B", "C", "all"], default="all")
    ap.add_argument("--limit", type=int, default=None,
                    help="Phase C: stop after N new downloads (smoke-test)")
    args = ap.parse_args()

    log.info("Mirror root : %s (%s free)", MIRROR_ROOT,
             _human_bytes(shutil.disk_usage(MIRROR_ROOT).free)
             if MIRROR_ROOT.is_dir() else "?")
    if args.phase in ("A", "all"): phase_a()
    if args.phase in ("B", "all") and not _interrupted: phase_b()
    if args.phase in ("C", "all") and not _interrupted: phase_c(args.limit)
    log.info("Mirror root final size: %s", _human_bytes(_dir_size(MIRROR_ROOT)))


if __name__ == "__main__":
    main()
