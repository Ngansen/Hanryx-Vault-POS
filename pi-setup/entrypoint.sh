#!/bin/sh
# Fix /data ownership so the hanryx user can write kiosk + satellite files
chown -R hanryx:hanryx /data 2>/dev/null || true

# ---------------------------------------------------------------------------
# USB printer access.
#
# The container is started by docker-compose with `group_add: ["7"]`, which
# adds gid 7 (the host's `lp` group, owner of /dev/usb/lp0) to the container's
# *root* process. We then `su` to the hanryx user to run gunicorn, which drops
# those supplementary groups. To preserve printer access, we explicitly create
# the lp group with gid 7 inside the container (if missing) and add hanryx to
# it before switching users. Without this, gunicorn workers running as hanryx
# get EACCES on /dev/usb/lp0 and /print/status falls through to "cups".
# ---------------------------------------------------------------------------
if [ -e /dev/usb/lp0 ] || [ -e /dev/lp0 ]; then
    getent group lp >/dev/null 2>&1 || groupadd -g 7 lp 2>/dev/null || addgroup -g 7 lp 2>/dev/null || true
    usermod -a -G lp hanryx 2>/dev/null || adduser hanryx lp 2>/dev/null || gpasswd -a hanryx lp 2>/dev/null || true
fi

exec su -s /bin/sh hanryx -c 'exec gunicorn -c gunicorn.conf.py server:app'
