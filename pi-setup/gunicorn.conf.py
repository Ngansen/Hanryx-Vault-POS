import threading

# IMPORTANT: do NOT import psycogreen / gevent at the top of this config file.
# Doing so transitively imports ssl, redis, urllib3 BEFORE gevent's worker
# class runs monkey.patch_all(), leaving those modules un-patched. Outbound
# TLS / Redis calls then block the hub forever (SSE never completes, /health
# hangs). psycogreen.patch_psycopg() runs in post_worker_init below, AFTER
# gevent has patched the stdlib.

workers             = 1      # single worker so SSE subscribers share memory with cart updates
worker_class        = "gevent"
worker_connections  = 500   # gevent handles hundreds of concurrent SSE + API connections

bind      = "0.0.0.0:8080"

timeout          = 300   # longer timeout for SSE streams
keepalive        = 65

max_requests       = 2000
max_requests_jitter = 200

loglevel  = "info"
accesslog = "-"
errorlog  = "-"

preload_app = False


# NOTE: do NOT define on_starting here — importing `server` in the master
# process pulls in urllib3 / redis / jwt BEFORE gevent's monkey.patch_all
# runs in each worker, leaving them un-patched and causing routes that do
# HTTPS / JWT verification (e.g. /api/v1/checkout) to block the hub forever.
# All initialisation now happens in post_worker_init below.


def post_worker_init(worker):
    """Run AFTER gevent's monkey.patch_all so HTTPS/Redis/JWT are cooperative."""
    try:
        from psycogreen.gevent import patch_psycopg
        patch_psycopg()
        worker.log.info("[psycogreen] psycopg patched for gevent")
    except Exception as _e:
        worker.log.warning("[psycogreen] patch failed: %s", _e)
    try:
        from server import init_db, _load_tokens_from_db
        init_db()
        _load_tokens_from_db()
        worker.log.info("DB schema initialised successfully (worker)")
    except Exception as _e:
        worker.log.warning("DB init error (will retry on first request): %s", _e)


def post_fork(server, worker):
    """Start background threads after fork."""
    import os
    # Only start background threads in worker 1 to avoid duplication
    if worker.age == 1:
        try:
            from server import (
                sync_inventory_from_cloud,
                _warmup_smart_scanner,
                _run_low_stock_checker,
                _prewarm_all_pricing_bg,
                _prewarm_lang_all_bg,
            )
            # Each thread can be disabled independently via env var so we can
            # kill the ones that block gevent's hub and cause WORKER TIMEOUT.
            # Set DISABLE_BG_<NAME>=1 in the pos service env to turn one off.
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
