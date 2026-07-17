#!/usr/bin/env bash
# Run as: sudo ./scripts/install_caddy_service.sh  (from repo root)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BIN="${CADDY_BIN:-/usr/local/bin/caddy}"
UNIT=/etc/systemd/system/x402-caddy.service

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Re-run with: sudo $0" >&2
  exit 1
fi
if [[ ! -x "$BIN" ]]; then
  echo "Missing caddy at $BIN — install caddy or set CADDY_BIN" >&2
  exit 1
fi

cp "$BIN" /usr/local/bin/caddy
mkdir -p /etc/caddy
cp "$ROOT/deploy/Caddyfile" /etc/caddy/Caddyfile

cat > "$UNIT" <<'UNIT'
[Unit]
Description=x402-pow Caddy (spammingbitcoin.com)
After=network.target

[Service]
User=root
ExecStart=/usr/local/bin/caddy run --config /etc/caddy/Caddyfile --adapter caddyfile
ExecReload=/usr/local/bin/caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile
Restart=on-failure
AmbientCapabilities=CAP_NET_BIND_SERVICE
TimeoutStopSec=5s
LimitNOFILE=1048576

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now x402-caddy.service
systemctl status x402-caddy.service --no-pager
echo "OK — Caddy reloaded from $ROOT/deploy/Caddyfile"
