# CRITICAL: monkey.patch_all() must run BEFORE any other import that touches
# ssl / socket / select. Otherwise urllib3, jwt.jwks_client, redis.asyncio
# stay un-patched and any HTTPS / Redis call from a route blocks the gevent
# hub forever (e.g. /api/v1/checkout times out at 5 s).
from gevent import monkey
monkey.patch_all()

from psycogreen.gevent import patch_psycopg
patch_psycopg()

import threading

workers             = 1
worker_class        = "gevent"
worker_connections  = 500

bind      = "0.0.0.0:8080"

timeout          = 300
keepalive        = 65

max_requests       = 2000
max_requests_jitter = 200

loglevel  = "info"
accesslog = "-"
errorlog  = "-"

preload_app = False


def on_starting(server):
    try:
        from server import init_db, _load_tokens_from_db
        init_db()
        _load_tokens_from_db()
        server.log.info("DB schema initialised successfully")
    except Exception as _e:
        server.log.warning("DB init error (will retry on first request): %s", _e)


def post_fork(server, worker):
    import os
    if worker.age == 1:
        try:
            from server import (
                sync_inventory_from_cloud,
                _warmup_smart_scanner,
                _run_low_stock_checker,
                _prewarm_all_pricing_bg,
                _prewarm_lang_all_bg,
            )
            def _maybe(name, fn, thread_name):
                if os.environ.get(f"DISABLE_BG_{name}") == "1":
                    server.log.info("[bg] %s disabled via env", thread_name)
                    return
                threading.Thread(target=fn, daemon=True, name=thread_name).start()

            _maybe("CLOUD_SYNC",      sync_inventory_from_cloud,  "cloud-sync")
            _maybe("SMART_SCAN_WARM", _warmup_smart_scanner,      "smart-scan-warm")
            _maybe("LOW_STOCK",       _run_low_stock_checker,     "low-stock")
            _maybe("PRICING_PREWARM", _prewarm_all_pricing_bg,    "pricing-prewarm")
            _maybe("LANG_PREWARM",    _prewarm_lang_all_bg,       "lang-prewarm")
        except Exception as _e:
            server.log.warning("post_fork startup thread error: %s", _e)
