import threading

# Make psycopg2 cooperative with gevent — without this, every DB call blocks
# the entire event loop including all SSE streams + concurrent API requests,
# eventually causing worker timeouts and 1-2 s outages on every restart.
try:
    from psycogreen.gevent import patch_psycopg
    patch_psycopg()
except ImportError:
    pass

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


def on_starting(server):
    """Initialise the database schema once before workers start."""
    try:
        from server import init_db, _load_tokens_from_db
        init_db()
        _load_tokens_from_db()
        server.log.info("DB schema initialised successfully")
    except Exception as _e:
        server.log.warning("DB init error (will retry on first request): %s", _e)


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
