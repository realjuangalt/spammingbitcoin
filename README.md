# spammingbitcoin (x402-pow)

Bitcoin **PoW-only** x402 **Pool**. Agents pay with SHA-256d compute; **Sites** enroll and accrue custodial share balances. The Pool resells hashrate to an **Upstream pool**. No USDC. No local `bitcoind`.

Live demo: [spammingbitcoin.com](https://spammingbitcoin.com) · Source: [github.com/realjuangalt/spammingbitcoin](https://github.com/realjuangalt/spammingbitcoin)

## Language (locked)

| Term | Meaning |
|---|---|
| **Pool** | Us (Spamming Bitcoin) — jobs, Stratum, receipts, ledger |
| **Site** | Website connected to the Pool |
| **Agent** | Miner talking to a Site (bot or human) |
| **Upstream pool** | Mining pool we forward hashrate to (Xaxa, Braiins, …) |

## Layout

```
apps/demo_site/static/memes/   ← drop / swap meme images here + manifest.json
src/x402_pow/                  ← Python package
templates/                     ← Jinja for demo + signup
scripts/fetch_memes.py         ← scrape Phneep gallery
```

## Quick start

```bash
git clone https://github.com/realjuangalt/spammingbitcoin.git
cd spammingbitcoin
cp .env.example .env   # edit secrets locally — never commit .env

uv sync
python scripts/fetch_memes.py   # optional meme gallery

# four processes (or use scripts/run_dev.sh)
uv run uvicorn x402_pow.apps.pool_api:app --host 127.0.0.1 --port 8100
uv run uvicorn x402_pow.apps.demo_site:app --host 127.0.0.1 --port 8101
uv run uvicorn x402_pow.apps.whitepaper_site:app --host 127.0.0.1 --port 8103
uv run uvicorn x402_pow.apps.pool_signup:app --host 127.0.0.1 --port 8102
# Stratum edge starts with the API when UPSTREAM_MODE=reseller
```

- API: http://127.0.0.1:8100/health  
- Meme vault: http://127.0.0.1:8101/meme  
- White paper Site: http://127.0.0.1:8103/paper  
- Pool signup: http://127.0.0.1:8102/
- Stratum (Bitaxe / Agents): `pool.spammingbitcoin.com:3333`

## Agent pay flow

1. `GET /meme` → `402` + `PAYMENT-REQUIRED` / JSON with `minerHint`
2. `curl` the miner script, grind until access target, `POST /v1/shares/submit`
3. Retry with `PAYMENT-SIGNATURE: <receiptToken>` or POST receipt to `/meme/unlock`

## Site owners

- Live signup + guide: https://signup.spammingbitcoin.com/
- Docs: [docs/site-owners.md](docs/site-owners.md) (also `/docs.md` on signup)

## Deploy

Point DNS A records for `@`, `www`, `api`, `signup`, `demo`, `pool`, `stats` at your server. Reverse proxy HTTPS with [deploy/Caddyfile](deploy/Caddyfile) (see `scripts/install_caddy_service.sh`).

## Env

Copy `.env.example` → `.env`. Never commit `.env` or `data/` (SQLite ledger, publisher keys, wallets).

Important keys: `RECEIPT_SECRET`, `PUBLIC_API_BASE`, `ACCESS_ZERO_BITS`, `UPSTREAM_*`, `STRATUM_LISTEN_PORT`, `PUBLIC_GITHUB_REPO`.
