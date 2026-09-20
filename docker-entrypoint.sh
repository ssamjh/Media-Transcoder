#!/bin/sh
# Start as root, move the standardisarr user onto PUID/PGID, take ownership of
# the directories the app writes to, then drop to that user for good.
#
# This is the linuxserver.io pattern, and it is here for one reason: a bind
# mount keeps the host directory's ownership, so a container that runs as a
# fixed uid can only write to host directories that happen to be owned by it.
# Matching the container's uid to the host's is what makes that work without
# anyone having to chown their media tree by hand.
set -e

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

if [ "$(id -u)" -ne 0 ]; then
    # Already unprivileged - compose set `user:`, or this is a rootless
    # runtime. Nothing to align and nothing to chown; the startup preflight
    # will say so if a directory turns out to be read-only.
    exec python -m app "$@"
fi

groupmod -o -g "$PGID" standardisarr
usermod -o -u "$PUID" -g "$PGID" standardisarr

# The scratch directory is a config setting, so ask the app where it is
# rather than duplicating the default here.
TEMP_DIR="$(python -c 'import os; from app.config import load; print(load(os.environ.get("STANDARDISARR_CONFIG")).output.temp_dir)' 2>/dev/null)" || TEMP_DIR=""
[ -n "$TEMP_DIR" ] || TEMP_DIR=/tmp/standardisarr

mkdir -p /config "$TEMP_DIR"
chown -R "$PUID:$PGID" /config "$TEMP_DIR"

echo "standardisarr: starting as ${PUID}:${PGID} (scratch: ${TEMP_DIR})"
exec setpriv --reuid "$PUID" --regid "$PGID" --init-groups -- python -m app "$@"
