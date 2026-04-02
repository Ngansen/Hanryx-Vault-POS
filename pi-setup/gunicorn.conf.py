import multiprocessing

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
