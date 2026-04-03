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

preload_app = False


def on_starting(server):
    """Initialise the database schema once before workers are forked."""
    try:
        from server import init_db, _load_tokens_from_db
        init_db()
        _load_tokens_from_db()
        server.log.info("DB schema initialised successfully")
    except Exception as _e:
        server.log.warning("DB init error (will retry on first request): %s", _e)


def post_fork(server, worker):
    """Reset DB pool and start background threads after fork.

    psycopg2 connections are not fork-safe. With preload_app=True the pool
    is created in the master process; each worker must discard it and open
    fresh connections of its own.
    """
    # ── Reset the inherited connection pool so workers get fresh connections ──
    try:
        import server as _srv
        if _srv._pg_pool is not None:
            try:
                _srv._pg_pool.closeall()
            except Exception:
                pass
            _srv._pg_pool = None
    except Exception as _e:
        server.log.warning("post_fork pool reset error: %s", _e)

    # ── Start per-worker background threads ───────────────────────────────────
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
        # Pre-warm only in first worker to avoid hammering eBay from every worker
        if worker.age == 1:
            threading.Thread(
                target=_prewarm_all_pricing_bg, daemon=True, name="pricing-prewarm"
            ).start()
            threading.Thread(
                target=_prewarm_lang_all_bg, daemon=True, name="lang-prewarm"
            ).start()
    except Exception as _e:
        server.log.warning("post_fork startup thread error: %s", _e)
