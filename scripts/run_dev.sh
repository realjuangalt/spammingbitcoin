#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export PATH="$HOME/.local/bin:$PATH"
uv sync
mkdir -p data apps/demo_site/static/memes apps/whitepaper_site/static

uv run uvicorn x402_pow.apps.pool_api:app --host 127.0.0.1 --port 8100 &
uv run uvicorn x402_pow.apps.demo_site:app --host 127.0.0.1 --port 8101 &
uv run uvicorn x402_pow.apps.pool_signup:app --host 127.0.0.1 --port 8102 &
uv run uvicorn x402_pow.apps.whitepaper_site:app --host 127.0.0.1 --port 8103 &
echo "API :8100  memes :8101  signup :8102  whitepaper :8103"
wait
