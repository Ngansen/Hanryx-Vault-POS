#!/bin/sh
# Fix /data ownership so the hanryx user can write kiosk + satellite files
chown -R hanryx:hanryx /data 2>/dev/null || true
exec su -s /bin/sh hanryx -c 'exec gunicorn -c gunicorn.conf.py server:app'
