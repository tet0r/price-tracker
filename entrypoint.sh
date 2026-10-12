#!/bin/sh
set -e

# The app loads arbitrary web pages in a browser, so it must not run as root.
# Root is used only to hand the data volume to the unprivileged user.
if [ "$(id -u)" = "0" ]; then
    chown -R pwuser:pwuser "$DATA_DIR"
    exec setpriv --reuid=pwuser --regid=pwuser --init-groups "$@"
fi
exec "$@"
