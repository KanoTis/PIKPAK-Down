#!/bin/sh
set -e

mkdir -p /data /downloads /cache

if [ "$(id -u)" = "0" ]; then
  chown -R app:app /data /downloads /cache 2>/dev/null || true
  exec gosu app "$@"
fi

exec "$@"
