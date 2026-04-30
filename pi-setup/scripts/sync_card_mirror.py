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
    MIRROR_ROOT=/mnt/cards \\
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

# Sibling module — the persistent failure log helper. Imported eagerly
# (no postgres dep) so the absence of psycopg2 doesn't break Phase A.
from scripts.mirror_failure_log import record_mirror_outcome  # noqa: E402
from scripts import zh_sources  # noqa: E402

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


def _open_failure_log_conn():
    """Best-effort connect to DATABASE_URL for failure-log writes.

    Returns a psycopg2 connection or None when DATABASE_URL is unset
    or psycopg2 isn't importable on this host. The downloader treats
    None as "skip the persistent log, console-debug only" — Phase A
    runs on the bare Pi host without the docker venv and doesn't
    have psycopg2, so this can't be a hard failure.

    autocommit=False — the helper commits per outcome, which is the
    right granularity for an interrupted run (every recorded outcome
    is durable when the next outcome lands)."""
    db_url = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")
    if not db_url:
        log.info("[sync] DATABASE_URL not set — failure log disabled "
                 "(downloads will still run, just no persistent triage)")
        return None
    try:
        import psycopg2  # type: ignore
    except ImportError:
        log.info("[sync] psycopg2 not importable on this host — failure "
                 "log disabled. Install with `pip install psycopg2-binary` "
                 "inside the consolidator container venv to enable.")
        return None
    try:
        conn = psycopg2.connect(db_url)
        return conn
    except Exception as e:
        log.warning("[sync] failure-log connect failed (%s) — proceeding "
                    "without persistent log", e)
        return None


def _record_outcome_safe(conn, *, url: str, src: str, dest: Path,
                         ok: bool, status: str) -> None:
    """Wrap record_mirror_outcome so any DB hiccup is visible but
    NEVER aborts the download loop. The downloader's first job is
    to mirror files; the failure log is observability on top of
    that."""
    if conn is None:
        return
    try:
        record_mirror_outcome(
            conn, url=url, src=src, dest_path=str(dest),
            ok=ok, status=status,
        )
    except Exception as e:
        log.debug("[sync] failure-log write skipped for %s: %s", url, e)


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
              min_size: int = 256, revalidate: bool = True,
              extra_headers: Optional[dict] = None
              ) -> tuple[bool, str]:
    """
    Download `url` to `dest`. Returns (ok, status). Atomic: writes to
    dest.tmp first, fsyncs, renames. Files smaller than `min_size` bytes
    are treated as failed (CDNs sometimes return 1-byte error stubs).

    revalidate=True (default):
        When `dest` already exists, send `If-Modified-Since: <mtime>`
        and treat HTTP 304 as success (status='not-modified', no
        write). On 200, replace the file and stamp its mtime from the
        response Last-Modified header so the next IMS round-trip is
        cheap. The first re-run after a full sync becomes thousands
        of free 304s instead of thousands of zero-cost
        skip-exists shortcuts that hide silent upstream churn.

    revalidate=False:
        Pure resume mode — if `dest` exists with size >= min_size we
        short-circuit with 'skip-exists' and never hit the network.
        Use when there is no upstream worth checking (Phase A is
        already covered by `git pull`; this flag exists so a future
        offline-only refresh can opt out entirely).

    Why a parameter rather than always-revalidate: a fresh Pi spinning
    up at a trade-show venue with flaky WiFi shouldn't be required to
    HEAD every one of ~50k images before serving them locally. The
    operator can flip `revalidate=False` from the cron one-liner for
    the truly-offline case.
    """
    # Lazy import — `email.utils` is stdlib but the imports stay
    # local so the (rare) hostile-environment case where stdlib
    # email is shadowed still loads sync_card_mirror enough for
    # Phase A git work to run.
    from email.utils import formatdate, parsedate_to_datetime

    existing_mtime: float | None = None
    if dest.exists() and dest.stat().st_size >= min_size:
        if not revalidate:
            return True, "skip-exists"
        existing_mtime = dest.stat().st_mtime

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    # extra_headers wins over our defaults so per-source UA from
    # zh_sources can override the mirror-wide USER_AGENT (some sites
    # serve different HTML to obvious bot UAs).
    headers: dict[str, str] = {"User-Agent": USER_AGENT}
    if extra_headers:
        headers.update(extra_headers)
    if existing_mtime is not None:
        # RFC 7232 §3.3 — usegmt=True for the IMF-fixdate format the
        # spec mandates ("Wed, 21 Oct 2015 07:28:00 GMT"). Servers
        # that strict-parse will reject the local-tz form.
        headers["If-Modified-Since"] = formatdate(existing_mtime,
                                                  usegmt=True)
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if r.status == 304:
                # 304 has no body — server confirms our copy is
                # still fresh. No write, no atomicity dance, just
                # bump our local mtime to "now" so the next IMS
                # round trip uses an up-to-date floor.
                try:
                    now = time.time()
                    os.utime(dest, (now, now))
                except OSError:
                    pass
                return True, "not-modified"
            if r.status >= 400:
                return False, f"http-{r.status}"
            # Capture Last-Modified BEFORE we close the response —
            # urllib's response object is single-pass and the headers
            # vanish once .read() returns on some platforms.
            last_modified = r.headers.get("Last-Modified")
            with open(tmp, "wb") as f:
                shutil.copyfileobj(r, f, length=64 * 1024)
                f.flush()
                os.fsync(f.fileno())
        if tmp.stat().st_size < min_size:
            tmp.unlink(missing_ok=True)
            return False, "too-small"
        os.replace(tmp, dest)
        # Stamp the file's mtime from the server's Last-Modified so
        # our next IMS request matches what the upstream thinks the
        # version is. If the header is missing or unparseable we leave
        # the OS-default mtime (= now) which is also fine — the
        # tradeoff is one extra full-body refresh on the next cycle.
        if last_modified:
            try:
                dt = parsedate_to_datetime(last_modified)
                if dt is not None:
                    os.utime(dest, (dt.timestamp(), dt.timestamp()))
            except (TypeError, ValueError, OSError):
                # Bad date or unsupported FS — non-fatal. The file
                # is correctly downloaded; we just lose the IMS
                # optimisation for one cycle.
                pass
        return True, "ok"
    except urllib.error.HTTPError as e:
        # Some servers (notably older nginx defaults) return 304 as
        # an HTTPError rather than a normal response. Treat it the
        # same — 304 is a successful conditional GET regardless of
        # which urllib branch surfaces it.
        if e.code == 304:
            try:
                now = time.time()
                os.utime(dest, (now, now))
            except OSError:
                pass
            tmp.unlink(missing_ok=True)
            return True, "not-modified"
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

    # Optional persistent failure log — None when DATABASE_URL or
    # psycopg2 isn't available (Pi host vs. consolidator container).
    failure_conn = _open_failure_log_conn()
    n_total = n_ok = n_skip = n_fail = 0
    t0 = time.time()
    try:
        for json_path, url in _iter_kr_card_urls(kr_root):
            if _interrupted:
                log.warning("[B] interrupted at #%d", n_total)
                break
            n_total += 1
            base = os.path.basename(unquote(urlparse(url).path)) \
                or f"unknown-{n_total}.jpg"
            dest = img_root / base
            ok, status = _download(url, dest)
            _record_outcome_safe(failure_conn, url=url, src="kr_cardimg",
                                 dest=dest, ok=ok, status=status)
            if ok and status == "skip-exists":
                n_skip += 1
            elif ok:
                n_ok += 1
                if n_ok % 50 == 0:
                    log.info("[B] %d new, %d skipped, %d failed (%.1fs)",
                             n_ok, n_skip, n_fail, time.time() - t0)
            else:
                n_fail += 1
                log.debug("[B] fail %s → %s (%s)",
                          json_path.name, base, status)
    finally:
        if failure_conn is not None:
            try:
                failure_conn.close()
            except Exception:
                pass

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

    # Separate failure-log connection — _iter_cdn_candidates owns a
    # server-side cursor on its own connection, so committing per
    # outcome on that conn would interfere with the streamed read.
    failure_conn = _open_failure_log_conn()
    n_total = n_ok = n_skip = n_fail = 0
    by_src: dict[str, int] = {}
    t0 = time.time()
    try:
        for src, url in _iter_cdn_candidates(db_url, limit):
            if _interrupted:
                log.warning("[C] interrupted at #%d", n_total)
                break
            n_total += 1
            dest = _cdn_dest(url)
            ok, status = _download(url, dest)
            _record_outcome_safe(failure_conn, url=url, src=src,
                                 dest=dest, ok=ok, status=status)
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
    finally:
        if failure_conn is not None:
            try:
                failure_conn.close()
            except Exception:
                pass

    log.info("[C] done — %d total, %d new, %d skipped, %d failed (%s on disk)",
             n_total, n_ok, n_skip, n_fail, _human_bytes(_dir_size(cdn_root)))
    if by_src:
        log.info("[C] new files by source: %s",
                 ", ".join(f"{k}={v}" for k, v in sorted(by_src.items())))


# ── Phase D: ZH (TC + SC) card image walk ────────────────────────────

ZH_DEST_ROOT = MIRROR_ROOT / "zh"

# Canonical set lists ship inside the package; tests can override via
# _zh_canonical_path() so they don't depend on the real JSON files.
_CANONICAL_DIR = Path(__file__).parent / "canonical_sets"


def _zh_canonical_path(lang: zh_sources.Lang) -> Path:
    return _CANONICAL_DIR / f"zh_{lang.value}.json"


def _load_canonical_sets(lang: zh_sources.Lang) -> list[dict]:
    """
    Load canonical set list for `lang`. Returns the `sets` array verbatim.

    Entries with `set_id == "VERIFY"` (or any field literally "VERIFY")
    are still yielded — Phase D walks them but the resulting fetches
    will 404, get logged via mirror_failure_log, and surface in the
    audit. This is intentional: we don't want canonical_sets to silently
    eat a half-curated file.
    """
    p = _zh_canonical_path(lang)
    if not p.exists():
        log.warning("[D] canonical set file missing: %s", p)
        return []
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        log.error("[D] cannot parse %s: %s", p, e)
        return []
    sets = data.get("sets", [])
    if not isinstance(sets, list):
        log.error("[D] %s: 'sets' is not a list", p)
        return []
    return sets


def _zh_dest_path(source: zh_sources.Source, set_id: str,
                  card_num: str, ext: str) -> Path:
    """Canonical destination: /mnt/cards/zh/<lang>/<source>/<set>/<num>.<ext>"""
    return (ZH_DEST_ROOT / source.lang.value / source.name
            / set_id / f"{card_num}.{ext.lstrip('.')}")


def _link_or_copy(src: Path, dest: Path,
                  *, min_size: int = 256) -> tuple[bool, str]:
    """
    Mirror `src` to `dest` cheaply: hardlink if same FS, copy otherwise.

    Atomic: hardlink/copy via tmp + rename. Returns (ok, status).
    Skips if dest already exists with size >= min_size — Phase D is
    idempotent, so this is the common path on re-runs.
    """
    if not src.exists():
        return False, "src-missing"
    if src.stat().st_size < min_size:
        # Source is a stub/error file; treat same as too-small download.
        return False, "src-too-small"
    if dest.exists() and dest.stat().st_size >= min_size:
        return True, "skip-exists"
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    try:
        # Try hardlink first — instant + zero extra disk. Fails with
        # EXDEV across filesystems (e.g. tmpfs in tests, separate USB
        # mounts in prod) and EPERM on some FUSE mounts.
        try:
            if tmp.exists():
                tmp.unlink()
            os.link(src, tmp)
            status = "linked"
        except OSError:
            shutil.copyfile(src, tmp)
            status = "copied"
        os.replace(tmp, dest)
        return True, status
    except OSError as e:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False, f"err-{type(e).__name__}"


def _walk_local_zh_source(source: zh_sources.LocalMirrorSource,
                          sets: list[dict],
                          ) -> tuple[int, int, int, int]:
    """Walk one local-mirror source. Returns (total, ok, skip, fail)."""
    n_total = n_ok = n_skip = n_fail = 0
    repo_root = MIRROR_ROOT / source.repo_dir
    if not (repo_root / ".git").is_dir():
        log.warning("[D] %s: %s not cloned (run Phase A first)",
                    source.name, repo_root)
        return 0, 0, 0, 0
    for set_meta in sets:
        if _interrupted:
            break
        set_id = str(set_meta.get("set_id", ""))
        if not set_id or set_id == "VERIFY":
            continue
        # Walk every file in the source set dir; we don't enforce
        # expected_card_count here — that's ZH-4's job.
        src_set_dir = repo_root / source.image_path_template.split("/{")[0] / set_id
        if not src_set_dir.is_dir():
            log.debug("[D] %s set %s not on disk: %s",
                      source.name, set_id, src_set_dir)
            continue
        for src_file in sorted(src_set_dir.iterdir()):
            if _interrupted:
                break
            if not src_file.is_file():
                continue
            n_total += 1
            card_num = src_file.stem
            dest = _zh_dest_path(source, set_id, card_num, src_file.suffix)
            ok, status = _link_or_copy(src_file, dest)
            if ok and status == "skip-exists":
                n_skip += 1
            elif ok:
                n_ok += 1
            else:
                n_fail += 1
                log.debug("[D] %s %s/%s fail: %s",
                          source.name, set_id, card_num, status)
    return n_total, n_ok, n_skip, n_fail


def _walk_remote_zh_source(source: zh_sources.RemoteWebSource,
                           sets: list[dict],
                           failure_conn,
                           ) -> tuple[int, int, int, int]:
    """Walk one remote-web source. Returns (total, ok, skip, fail)."""
    n_total = n_ok = n_skip = n_fail = 0
    for set_meta in sets:
        if _interrupted:
            break
        set_id = str(set_meta.get("set_id", ""))
        if not set_id or set_id == "VERIFY" or set_id.startswith("VERIFY"):
            continue
        expected = set_meta.get("expected_card_count")
        if not isinstance(expected, int) or expected <= 0:
            log.debug("[D] %s set %s: expected_card_count not set, skipping",
                      source.name, set_id)
            continue
        # ptcg.tw uses zero-padded 3-digit card_num; if a future source
        # uses different padding, override via a per-source padding
        # field. For now hardcoded — only one remote source.
        for n in range(1, expected + 1):
            if _interrupted:
                break
            card_num = f"{n:03d}"
            url = source.url_for(set_id, card_num)
            # Image extension comes from the URL template, not the
            # source — derive once per source-set pair.
            ext = url.rsplit(".", 1)[-1] if "." in url.rsplit("/", 1)[-1] else "jpg"
            dest = _zh_dest_path(source, set_id, card_num, ext)
            n_total += 1
            time.sleep(source.rate_limit_seconds)
            ok, status = _download(url, dest,
                                   extra_headers=source.headers_dict)
            _record_outcome_safe(failure_conn, url=url, src=source.name,
                                 dest=dest, ok=ok, status=status)
            if ok and status in ("skip-exists", "not-modified"):
                n_skip += 1
            elif ok:
                n_ok += 1
                if n_ok % 50 == 0:
                    log.info("[D] %s: %d new, %d skipped, %d failed",
                             source.name, n_ok, n_skip, n_fail)
            else:
                n_fail += 1
    return n_total, n_ok, n_skip, n_fail


def phase_d(*, include_tc: bool, include_sc: bool,
            include_fallback: bool) -> None:
    log.info("=" * 60)
    log.info("Phase D — ZH card walk (tc=%s sc=%s fallback=%s)",
             include_tc, include_sc, include_fallback)
    log.info("=" * 60)
    enabled_langs: list[zh_sources.Lang] = []
    if include_tc:
        enabled_langs.append(zh_sources.Lang.TC)
    if include_sc:
        enabled_langs.append(zh_sources.Lang.SC)
    if not enabled_langs:
        log.info("[D] no langs enabled — pass --include-zh-tc and/or --include-zh-sc")
        return
    if not MIRROR_ROOT.is_dir():
        log.error("[D] MIRROR_ROOT does not exist: %s", MIRROR_ROOT)
        return
    ZH_DEST_ROOT.mkdir(parents=True, exist_ok=True)

    failure_conn = _open_failure_log_conn()
    grand_total = grand_ok = grand_skip = grand_fail = 0
    t0 = time.time()
    try:
        for lang in enabled_langs:
            sets = _load_canonical_sets(lang)
            sources = zh_sources.sources_for(
                lang, include_fallback=include_fallback)
            log.info("[D] %s: %d sets × %d sources",
                     lang.value, len(sets), len(sources))
            for source in sources:
                if _interrupted:
                    break
                if isinstance(source, zh_sources.LocalMirrorSource):
                    t, o, s, f = _walk_local_zh_source(source, sets)
                else:
                    t, o, s, f = _walk_remote_zh_source(
                        source, sets, failure_conn)
                grand_total += t; grand_ok += o
                grand_skip += s; grand_fail += f
                log.info("[D] %-22s done — %d total, %d new, %d skipped, %d failed",
                         source.name, t, o, s, f)
    finally:
        if failure_conn is not None:
            try:
                failure_conn.close()
            except Exception:
                pass

    log.info("[D] phase done — %d total, %d new, %d skipped, %d failed (%.1fs, %s on disk)",
             grand_total, grand_ok, grand_skip, grand_fail,
             time.time() - t0, _human_bytes(_dir_size(ZH_DEST_ROOT)))


# ── main ──────────────────────────────────────────────────────────────

def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--phase", choices=["A", "B", "C", "D", "all"],
                    default="all")
    ap.add_argument("--limit", type=int, default=None,
                    help="Phase C: stop after N new downloads (smoke-test)")
    ap.add_argument("--include-zh-tc", action="store_true", default=True,
                    help="Phase D: walk Traditional Chinese sources (default on)")
    ap.add_argument("--no-zh-tc", dest="include_zh_tc", action="store_false",
                    help="Phase D: skip Traditional Chinese sources")
    ap.add_argument("--include-zh-sc", action="store_true", default=True,
                    help="Phase D: walk Simplified Chinese sources (default on)")
    ap.add_argument("--no-zh-sc", dest="include_zh_sc", action="store_false",
                    help="Phase D: skip Simplified Chinese sources")
    ap.add_argument("--zh-fallback-sources", action="store_true", default=False,
                    help="Phase D: also walk fallback sources (e.g. MyCardArt)")
    args = ap.parse_args()

    log.info("Mirror root : %s (%s free)", MIRROR_ROOT,
             _human_bytes(shutil.disk_usage(MIRROR_ROOT).free)
             if MIRROR_ROOT.is_dir() else "?")
    if args.phase in ("A", "all"): phase_a()
    if args.phase in ("B", "all") and not _interrupted: phase_b()
    if args.phase in ("C", "all") and not _interrupted: phase_c(args.limit)
    if args.phase in ("D", "all") and not _interrupted:
        phase_d(include_tc=args.include_zh_tc,
                include_sc=args.include_zh_sc,
                include_fallback=args.zh_fallback_sources)
    log.info("Mirror root final size: %s", _human_bytes(_dir_size(MIRROR_ROOT)))


if __name__ == "__main__":
    main()
