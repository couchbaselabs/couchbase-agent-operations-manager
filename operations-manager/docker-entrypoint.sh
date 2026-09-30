#!/bin/sh
# Serves HTTPS by default, using the TLS cert/key at /app/tls - a
# self-signed pair baked in at build time (see the Dockerfile) unless
# you've mounted your own real certificate over the same two paths.
#
# Set DISABLE_TLS=true to fall back to plain HTTP instead - e.g. when a
# reverse proxy or load balancer already in front of this container
# terminates TLS and re-encrypting here would just be redundant.
set -e

# Ceiling on simultaneous in-flight HTTP requests. Past it uvicorn answers
# 503 immediately instead of queueing, so an overloaded appliance sheds the
# excess and stays reachable - rather than accepting every request, parking
# them all behind a saturated worker pool, and letting nginx turn the whole
# dashboard (login included) into 504s. See config.WORKER_THREADS.
MAX_CONCURRENT_REQUESTS="${MAX_CONCURRENT_REQUESTS:-64}"

TLS_KEY_FILE="${TLS_KEY_FILE:-/app/tls/server.key}"
TLS_CERT_FILE="${TLS_CERT_FILE:-/app/tls/server.crt}"

if [ "${DISABLE_TLS:-false}" = "true" ]; then
  echo "[entrypoint] DISABLE_TLS=true - serving plain HTTP on :8090"
  exec python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8090 \
    --limit-concurrency "$MAX_CONCURRENT_REQUESTS"
fi

if [ ! -f "$TLS_KEY_FILE" ] || [ ! -f "$TLS_CERT_FILE" ]; then
  echo "[entrypoint] TLS cert/key not found at $TLS_CERT_FILE / $TLS_KEY_FILE - falling back to plain HTTP." >&2
  echo "[entrypoint] This shouldn't happen with the bundled image (a fallback cert is baked in) unless a volume mount replaced /app/tls without both files. Set DISABLE_TLS=true to silence this." >&2
  exec python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8090 \
    --limit-concurrency "$MAX_CONCURRENT_REQUESTS"
fi

# $TLS_KEY_FILE lives on the tls-shared volume, which ui's nginx also
# mounts (at /etc/nginx/tls) to serve the same certificate. That volume
# persists across image rebuilds, so a key file left over from before
# this permission scheme existed - or written by an older image - won't
# get fixed just by rebuilding. Re-assert group-root-readable + 0640 on
# every startup (self-heals stale volumes) so nginx's root master
# process (which lost CAP_DAC_OVERRIDE under ui's cap_drop: [ALL]
# hardening in docker-compose.yml) can always read it. See
# _grant_group_root_read() in app/user_auth.py for the same fix applied
# whenever a certificate is installed/reverted at runtime.
chgrp root "$TLS_KEY_FILE" 2>/dev/null || true
chmod 640 "$TLS_KEY_FILE" 2>/dev/null || true

echo "[entrypoint] Serving HTTPS on :8090 (cert: $TLS_CERT_FILE)"
exec python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8090 \
  --limit-concurrency "$MAX_CONCURRENT_REQUESTS" \
  --ssl-keyfile "$TLS_KEY_FILE" \
  --ssl-certfile "$TLS_CERT_FILE"
