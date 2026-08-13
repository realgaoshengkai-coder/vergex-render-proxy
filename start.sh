#!/bin/sh
set -eu

: "${PORT:=10000}"
: "${CLIPROXY_CONFIG_B64:?missing CLIPROXY_CONFIG_B64}"
: "${CLIPROXY_AUTH_B64:?missing CLIPROXY_AUTH_B64}"

mkdir -p /run/cliproxy/auth
printf '%s' "$CLIPROXY_CONFIG_B64" | base64 -d > /run/cliproxy/config.yaml
printf '%s' "$CLIPROXY_AUTH_B64" | base64 -d > /run/cliproxy/auth/codex.json
chmod 0600 /run/cliproxy/config.yaml /run/cliproxy/auth/codex.json

/CLIProxyAPI/CLIProxyAPI -config /run/cliproxy/config.yaml &
cliproxy_pid=$!

trap 'kill "$cliproxy_pid" 2>/dev/null || true; wait "$cliproxy_pid" 2>/dev/null || true' TERM INT EXIT

for _ in 1 2 3 4 5 6 7 8 9 10; do
    if python3 -c 'import socket; s=socket.create_connection(("127.0.0.1",8317),1); s.close()' 2>/dev/null; then
        exec python3 /app/responses_proxy.py
    fi
    sleep 1
done

echo "CLIProxyAPI did not become ready" >&2
exit 1
