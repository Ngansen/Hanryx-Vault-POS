import multiprocessing
import threading

workers          = 4
worker_class     = "gevent"
worker_connections = 1000

bind             = "0.0.0.0:8080"

timeout          = 120
keepalive        = 65

max_requests       = 2000
max_requests_jitter = 200

loglevel   = "info"
accesslog  = "-"
errorlog   = "-"

preload_app = True


def post_fork(server, worker):
    """Start background threads inside each worker process after fork().

    Threads don't survive fork(), so any thread that must run in workers
    (not just the gunicorn master) must be started here.

    The bulk pricing pre-warm is intentionally started only in the first
    worker (arbiter ID 1) to avoid hammering eBay from every worker at boot.
    Low-stock checker, cloud sync, and scanner warm-up run in all workers
    because they are lightweight and idempotent.
    """
    try:
        from server import (
            sync_inventory_from_cloud,
            _warmup_smart_scanner,
            _run_low_stock_checker,
            _prewarm_all_pricing_bg,
            _prewarm_lang_all_bg,
        )
        threading.Thread(target=sync_inventory_from_cloud,  daemon=True).start()
        threading.Thread(target=_warmup_smart_scanner,      daemon=True).start()
        threading.Thread(target=_run_low_stock_checker,     daemon=True).start()
        # Both pre-warm daemons only in the first worker to serialise eBay calls
        if worker.age == 1:
            threading.Thread(
                target=_prewarm_all_pricing_bg, daemon=True, name="pricing-prewarm"
            ).start()
            threading.Thread(
                target=_prewarm_lang_all_bg, daemon=True, name="lang-prewarm"
            ).start()
    except Exception as _e:
        server.log.warning("post_fork startup thread error: %s", _e)
