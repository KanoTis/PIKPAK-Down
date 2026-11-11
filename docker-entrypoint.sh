#!/bin/sh
set -e

mkdir -p /data /downloads /cache

if [ "$(id -u)" = "0" ]; then
  chown -R app:app /data /downloads /cache 2>/dev/null || true
  if command -v gosu >/dev/null 2>&1; then
    exec gosu app "$@"
  elif command -v su-exec >/dev/null 2>&1; then
    exec su-exec app "$@"
  else
    echo "no privilege-drop helper (gosu/su-exec) found" >&2
    exit 1
  fi
fi

exec "$@"
