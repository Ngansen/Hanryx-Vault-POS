import threading

workers      = 2
worker_class = "gthread"
threads      = 4

bind      = "0.0.0.0:8080"

timeout          = 120
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
            threading.Thread(target=sync_inventory_from_cloud,  daemon=True, name="cloud-sync").start()
            threading.Thread(target=_warmup_smart_scanner,      daemon=True, name="smart-scan-warm").start()
            threading.Thread(target=_run_low_stock_checker,     daemon=True, name="low-stock").start()
            threading.Thread(target=_prewarm_all_pricing_bg,    daemon=True, name="pricing-prewarm").start()
            threading.Thread(target=_prewarm_lang_all_bg,       daemon=True, name="lang-prewarm").start()
        except Exception as _e:
            server.log.warning("post_fork startup thread error: %s", _e)
