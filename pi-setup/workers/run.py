#!/usr/bin/env python3
"""
workers/run.py — CLI launcher for HanryxVault background helpers.

Usage
-----
  # Drain the queue once (suitable for cron):
  python3 -m workers.run image_health --once

  # Seed the queue from current cards_master state, then drain:
  python3 -m workers.run image_health --seed --once

  # Stay resident — process any work as it appears:
  python3 -m workers.run image_health --loop

  # Cap loop runs (testing): exit after N idle passes
  python3 -m workers.run image_health --loop --max-idle 3

  # Override batch size:
  python3 -m workers.run image_health --once --batch-size 200

  # List registered workers:
  python3 -m workers.run --list

Environment:
  DATABASE_URL — required for any actual work

Each helper module registers itself in WORKERS below. New helpers
can be added in three lines:
  1. import the class
  2. add it to WORKERS
  3. add an arg-passing block in build_worker() if it takes custom kwargs

Designed so cron entries like
    */15 * * * *  /usr/bin/docker compose exec -T server python3 \\
                  -m workers.run image_health --seed --once
are the entire deployment story.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))

import psycopg2  # noqa: E402

from workers.base import Worker  # noqa: E402
from workers.image_health import ImageHealthWorker  # noqa: E402
from workers.language_helper import LanguageEnrichWorker  # noqa: E402
from workers.data_analyst import DataAnalystWorker  # noqa: E402
from workers.clip_embedder import ClipEmbedderWorker  # noqa: E402
from workers.ocr_indexer import OcrIndexerWorker  # noqa: E402
from workers.price_refresh import PriceRefreshWorker  # noqa: E402
from workers.image_mirror import ImageMirrorWorker  # noqa: E402
from workers.image_thumbnailer import ImageThumbnailerWorker  # noqa: E402
from workers.kr_set_audit import KrSetAuditWorker  # noqa: E402
from workers.cross_region_aliaser import CrossRegionAliaserWorker  # noqa: E402
from workers.zh_set_audit import ZhSetAuditWorker  # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("workers.run")

# Registry. To add a new helper: import its class and add an entry here.
# The string key is what the operator types on the CLI.
WORKERS: dict[str, type[Worker]] = {
    "image_health":    ImageHealthWorker,
    "lang_enrich":     LanguageEnrichWorker,
    "data_analysis":   DataAnalystWorker,
    "clip_embed":      ClipEmbedderWorker,
    "ocr_index":       OcrIndexerWorker,
    "price_refresh":   PriceRefreshWorker,
    "image_mirror":    ImageMirrorWorker,
    "image_thumbnail": ImageThumbnailerWorker,
    "kr_set_audit":    KrSetAuditWorker,
    "cross_region_alias": CrossRegionAliaserWorker,
    "zh_set_audit":    ZhSetAuditWorker,
}


def build_worker(name: str, conn, args) -> Worker:
    """Instantiate the named worker. Each helper that takes custom
    kwargs (recheck_after_days, etc.) gets its own block here."""
    cls = WORKERS[name]
    common = {}
    if args.batch_size is not None:
        common["batch_size"] = args.batch_size

    # All three concrete workers share the same `recheck_after_days`
    # semantic — the only differences are their defaults, which the
    # classes themselves own. Pass-through when the operator overrides.
    recheck_s = (args.recheck_after_days * 86400
                 if args.recheck_after_days is not None else None)

    if name == "image_health":
        return ImageHealthWorker(conn, recheck_after_s=recheck_s, **common)
    if name == "lang_enrich":
        return LanguageEnrichWorker(conn, recheck_after_s=recheck_s, **common)
    if name == "data_analysis":
        return DataAnalystWorker(conn, recheck_after_s=recheck_s, **common)
    if name == "clip_embed":
        return ClipEmbedderWorker(conn,
                                  recheck_after_s=recheck_s,
                                  model_path=args.clip_model_path,
                                  model_id=args.clip_model_id,
                                  **common)
    if name == "ocr_index":
        return OcrIndexerWorker(conn,
                                recheck_after_s=recheck_s,
                                model_id=args.ocr_model_id,
                                lang_hint=args.ocr_lang_hint,
                                models_dir=args.ocr_models_dir,
                                **common)
    if name == "price_refresh":
        # Price worker has THREE recheck cadences (one per priority
        # tier), so the single --recheck-after-days flag doesn't
        # apply. Use the dedicated --price-*-recheck-days flags.
        def _days_to_s(v):
            return None if v is None else int(v) * 86400
        return PriceRefreshWorker(
            conn,
            inventory_recheck_s=_days_to_s(args.price_inventory_recheck_days),
            scanned_recheck_s=_days_to_s(args.price_scanned_recheck_days),
            catalogue_recheck_s=_days_to_s(args.price_catalogue_recheck_days),
            source=args.price_source,
            condition=args.price_condition,
            **common,
        )

    return cls(conn, **common)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("worker", nargs="?",
                    help="Worker type to run (see --list)")
    ap.add_argument("--list", action="store_true",
                    help="List registered workers and exit")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true",
                      help="Process one batch then exit (cron mode)")
    mode.add_argument("--loop", action="store_true",
                      help="Stay resident and keep polling (default)")
    ap.add_argument("--seed", action="store_true",
                    help="Run the worker's seed() before processing")
    ap.add_argument("--seed-only", action="store_true",
                    help="Run seed() and exit without processing")
    ap.add_argument("--batch-size", type=int,
                    help="Override Worker.BATCH_SIZE")
    ap.add_argument("--max-idle", type=int,
                    help="Loop mode: exit after this many empty passes "
                         "(useful for tests / drain-and-exit invocations)")
    ap.add_argument("--recheck-after-days", type=int,
                    help="re-check tasks whose last successful run was more "
                         "than N days ago (default per-worker: image_health=7, "
                         "lang_enrich=30, data_analysis=1, clip_embed=90, "
                         "ocr_index=90)")
    ap.add_argument("--clip-model-path", type=str, default=None,
                    help="clip_embed: override path to clip-vit-b32.onnx "
                         "(default: $CLIP_MODEL_PATH or /mnt/cards/models/"
                         "clip-vit-b32.onnx)")
    ap.add_argument("--clip-model-id", type=str, default=None,
                    help="clip_embed: override model_id tag stored alongside "
                         "embeddings (default: clip-vit-b32-onnx-1.0)")
    ap.add_argument("--ocr-model-id", type=str, default=None,
                    help="ocr_index: override model_id tag (default: "
                         "paddleocr-ppocrv4-1.0)")
    # Deliberately NOT constrained with argparse choices=. The
    # canonical list lives in workers/ocr_indexer.py::PADDLE_LANG_MAP
    # and the worker constructor validates the hint against that map
    # at startup, raising ValueError with the full list if rejected.
    # Hard-coding choices here would have left zh-cht and zh-sim
    # silently unreachable from CLI even after PADDLE_LANG_MAP grew
    # them in ZH-5 (zh_full_sync.sh would fail at argparse before
    # the worker ever ran). Keeping the list in one place avoids
    # that drift.
    ap.add_argument("--ocr-lang-hint", type=str, default=None,
                    help="ocr_index: pin every task to this language instead "
                         "of auto-picking per card (KR > JP > CHS > EN). "
                         "Valid values are the keys of PADDLE_LANG_MAP "
                         "(currently: kr, jp, chs, zh-sim, zh-cht, en). "
                         "zh-sim is an alias of chs (same Simplified pack) "
                         "indexed under its own lang_hint in card_ocr. "
                         "Useful for back-fill passes (e.g. JP overlay text "
                         "on a Korean print, or running both Chinese passes "
                         "against the ZH mirror).")
    ap.add_argument("--ocr-models-dir", type=str, default=None,
                    help="ocr_index: root dir for PaddleOCR per-language "
                         "model caches (default: $OCR_MODELS_DIR or "
                         "/mnt/cards/models/paddleocr). One subdir per "
                         "PaddleOCR language is created (korean/, japan/, "
                         "ch/, chinese_cht/, en/) each holding det/ and "
                         "rec/ model files. Note these are PaddleOCR's "
                         "internal lang names (the values of "
                         "PADDLE_LANG_MAP), NOT the lang_hint keys — so "
                         "both --ocr-lang-hint chs and --ocr-lang-hint "
                         "zh-sim load from the same ch/ subdir. Pass an "
                         "empty string to fall back to PaddleOCR's "
                         "~/.paddleocr default if the drive is unavailable.")
    ap.add_argument("--price-source", type=str, default=None,
                    help="price_refresh: pin every fan-out to a single "
                         "upstream client (e.g. 'ebay_sold', 'tcgplayer', "
                         "'tcgpl'). Default: hit every available source. "
                         "Useful for back-fill passes when one source has "
                         "drifted and you want to re-sync against another.")
    ap.add_argument("--price-condition", type=str, default=None,
                    help="price_refresh: condition tier to quote (NM/LP/MP/"
                         "HP/DM). Default: NM (matches the price_quotes "
                         "cache baseline; other tiers are derived from it "
                         "via condition multipliers at display time).")
    ap.add_argument("--price-inventory-recheck-days", type=int, default=None,
                    help="price_refresh: days between price refreshes for "
                         "cards currently in inventory (default 7 — weekly).")
    ap.add_argument("--price-scanned-recheck-days", type=int, default=None,
                    help="price_refresh: days between price refreshes for "
                         "cards scanned in the last 30 days but not in "
                         "inventory (default 14 — bi-weekly).")
    ap.add_argument("--price-catalogue-recheck-days", type=int, default=None,
                    help="price_refresh: days between price refreshes for "
                         "the long tail of cards_master that's neither in "
                         "inventory nor recently scanned (default 90 — "
                         "quarterly, to stay well under upstream API quotas).")
    args = ap.parse_args()

    if args.list:
        for name, cls in sorted(WORKERS.items()):
            print(f"  {name:14s}  {cls.__module__}.{cls.__name__}")
        return 0

    if not args.worker:
        ap.print_help()
        return 2

    if args.worker not in WORKERS:
        log.error("Unknown worker %r. Use --list to see registered helpers.",
                  args.worker)
        return 2

    url = os.environ.get("DATABASE_URL")
    if not url:
        log.error("DATABASE_URL is not set")
        return 1

    with psycopg2.connect(url) as conn:
        worker = build_worker(args.worker, conn, args)

        if args.seed or args.seed_only:
            n = worker.seed()
            log.info("[%s] seed() enqueued %d task(s)", args.worker, n)
            if args.seed_only:
                print(json.dumps({"seeded": n}, indent=2))
                return 0

        if args.once:
            stats = worker.run_once()
            print(json.dumps({"once": stats}, indent=2))
            return 0

        # Default = loop
        log.info("[%s] entering loop (max_idle=%s)",
                 args.worker, args.max_idle)
        try:
            totals = worker.run_forever(max_idle_passes=args.max_idle)
        except KeyboardInterrupt:
            log.info("[%s] interrupted by user", args.worker)
            return 130
        print(json.dumps({"loop": totals}, indent=2))
        return 0


if __name__ == "__main__":
    sys.exit(main())
