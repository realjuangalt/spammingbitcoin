# Site owner guide — gate content with Spamming Bitcoin (x402 PoW)

You are a **Site**: a website that enrolls with the **Pool**. **Agents** (browsers, bots, Bitaxe) unlock your gated URLs by doing SHA-256d work. No USDC. No accounts for Agents.

| Term | Meaning |
|---|---|
| **Pool** | Spamming Bitcoin — jobs, receipts, ledger, Stratum |
| **Site** | Your website (this guide) |
| **Agent** | Whoever is mining to unlock your content |
| **Upstream** | Mining pool we resell hashrate to (testnet or mainnet) |

**Live endpoints**

| Role | URL |
|---|---|
| Pool API | `https://api.spammingbitcoin.com` |
| Enroll (humans) | `https://signup.spammingbitcoin.com/` |
| Enroll (Agents / scripts) | `POST https://api.spammingbitcoin.com/v1/publishers` |
| Dashboard | `https://api.spammingbitcoin.com/dashboard` |
| Meme vault (apex) | `https://spammingbitcoin.com/meme` |
| White paper demo Site | `https://demo.spammingbitcoin.com/paper` |

---

## 1. Enroll

### Option A — API (no UI)

```bash
curl -sS https://api.spammingbitcoin.com/v1/publishers \
  -H 'content-type: application/json' \
  -d '{
    "origin": "https://your-site.example",
    "display_name": "Your Site",
    "access_zero_bits": 18
  }'
```

Response (save `apiKey` — shown once):

```json
{
  "publisherId": "pub_…",
  "apiKey": "sk_…",
  "stratum": {
    "host": "pool.spammingbitcoin.com",
    "port": 3333,
    "user": "pub_….default",
    "pass": "x"
  },
  "facilitatorBaseUrl": "https://api.spammingbitcoin.com",
  "network": "bitcoin:testnet4",
  "defaults": { "scheme": "pool-share", "accessZeroBits": 18, "asset": "share" }
}
```

### Option B — Form

Open [signup.spammingbitcoin.com](https://signup.spammingbitcoin.com/) and submit the form.

### Env you should keep

```bash
X402_POW_POOL_URL=https://api.spammingbitcoin.com
X402_POW_PUBLISHER_ID=pub_…
X402_POW_API_KEY=sk_…
X402_POW_ACCESS_ZERO_BITS=18
```

`access_zero_bits` ≈ expected hashes `2^bits`. At ~40 kH/s in-browser, **17–18** is a few seconds. Higher = harder / more valuable share.

---

## 2. Gate a URL (happy path)

```
Agent GET /secret
        │
        ▼
Site: create job at Pool  →  respond 402 + PAYMENT-REQUIRED
        │
        ▼
Agent: mine (script / browser / Stratum) → receiptToken
        │
        ▼
Agent retries with PAYMENT-SIGNATURE: <receiptToken>
   or POSTs receipt to your unlock endpoint
        │
        ▼
Site: settle with Pool (optional but recommended) → serve content
```

Your Site never verifies hashes itself for the access path — the **Pool** issues jobs and receipts. You bind each job to a **resource URL** so a receipt for `/premium` cannot unlock `/admin`.

---

## 3. Create a job

```bash
curl -sS "$X402_POW_POOL_URL/v1/jobs" \
  -H 'content-type: application/json' \
  -d "{
    \"publisher_id\": \"$X402_POW_PUBLISHER_ID\",
    \"resource\": \"https://your-site.example/premium\",
    \"access_zero_bits\": 18
  }"
```

Returns `jobId`, `prefix76`, `accessTarget`, `zeroBits`, `expiresAt`.

Use the **exact same** `resource` string later when the Agent submits the share and when you settle.

---

## 4. Return HTTP 402 Payment Required

Build a body like the Pool’s scheme (or copy from the demo). Minimum Agent-facing fields:

- Header: `PAYMENT-REQUIRED: <base64url(JSON)>`
- Body: JSON with `x402Version`, `accepts[]`, and `extra.minerHint`

`accepts[0]` should include:

| Field | Value |
|---|---|
| `scheme` | `pool-share` |
| `network` | from enroll (`bitcoin:testnet4` or `bitcoin:mainnet`) |
| `amount` | zero-bits as string |
| `asset` | `share` |
| `payTo` | your `publisherId` |
| `extra.jobId` / `prefix76` / `accessTarget` | from `/v1/jobs` |
| `extra.facilitator` | `https://api.spammingbitcoin.com` |
| `extra.resourceBinding` | same resource URL |
| `extra.minerHint.commands` | curl miner + run `mine_share.py` |

Pinned miner script:

`https://api.spammingbitcoin.com/static/mine_share.py`

Example miner command (Agent side):

```bash
curl -fsSL https://api.spammingbitcoin.com/static/mine_share.py -o /tmp/mine_share.py
python3 /tmp/mine_share.py \
  --api https://api.spammingbitcoin.com \
  --job-id JOB_ID \
  --prefix76 PREFIX76 \
  --target 0x… \
  --publisher pub_… \
  --resource https://your-site.example/premium \
  --receipt-out -
```

That prints JSON with `receiptToken`. The script already calls `POST /v1/shares/submit` on the Pool.

---

## 5. Accept the receipt and unlock

### Pattern A — header on retry

Agent retries:

```http
GET /premium HTTP/1.1
PAYMENT-SIGNATURE: <receiptToken>
```

On your Site:

1. Read `PAYMENT-SIGNATURE`
2. Optionally `POST /v1/settle` with your API key (marks receipt spent at the Pool)
3. Or call settle locally if you trust the token HMAC and your own spent-store
4. Serve the gated content

Settle via Pool (recommended):

```bash
curl -sS "$X402_POW_POOL_URL/v1/settle" \
  -H "authorization: Bearer $X402_POW_API_KEY" \
  -H 'content-type: application/json' \
  -d "{
    \"receipt_token\": \"…\",
    \"publisher_id\": \"$X402_POW_PUBLISHER_ID\",
    \"resource\": \"https://your-site.example/premium\"
  }"
```

### Pattern B — form / JSON unlock endpoint

Same as the demo: `POST /premium/unlock` with `receipt_token=…`. Easier for browsers.

---

## 6. Minimal Python (FastAPI-shaped) sketch

```python
import os, httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

POOL = os.environ["X402_POW_POOL_URL"].rstrip("/")
PUB = os.environ["X402_POW_PUBLISHER_ID"]
KEY = os.environ["X402_POW_API_KEY"]
BITS = int(os.environ.get("X402_POW_ACCESS_ZERO_BITS", "18"))

app = FastAPI()

def create_job(resource: str) -> dict:
    r = httpx.post(
        f"{POOL}/v1/jobs",
        json={"publisher_id": PUB, "resource": resource, "access_zero_bits": BITS},
        timeout=20,
    )
    r.raise_for_status()
    return r.json()

def payment_required_body(resource: str, job: dict) -> dict:
    miner = f"{POOL}/static/mine_share.py"
    target = job["accessTarget"]
    return {
        "x402Version": 2,
        "error": "Bitcoin PoW required",
        "resource": {"url": resource, "description": "Gated", "mimeType": "text/html"},
        "accepts": [{
            "scheme": "pool-share",
            "network": "bitcoin:testnet4",  # or bitcoin:mainnet
            "amount": str(job["zeroBits"]),
            "asset": "share",
            "payTo": PUB,
            "maxTimeoutSeconds": 120,
            "extra": {
                "jobId": job["jobId"],
                "prefix76": job["prefix76"],
                "accessTarget": target,
                "zeroBits": job["zeroBits"],
                "facilitator": POOL,
                "resourceBinding": resource,
                "minerHint": {
                    "kind": "shell",
                    "commands": [
                        f"curl -fsSL {miner} -o /tmp/mine_share.py",
                        f"python3 /tmp/mine_share.py --api {POOL} "
                        f"--job-id {job['jobId']} --prefix76 {job['prefix76']} "
                        f"--target {target} --publisher {PUB} "
                        f"--resource {resource} --receipt-out -",
                    ],
                },
            },
        }],
    }

def encode_b64url(obj: dict) -> str:
    import base64, json
    raw = json.dumps(obj, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")

@app.get("/premium")
def premium(request: Request, payment_signature: str | None = Header(default=None, alias="PAYMENT-SIGNATURE")):
    resource = str(request.url.replace(query=""))
    if payment_signature:
        r = httpx.post(
            f"{POOL}/v1/settle",
            headers={"authorization": f"Bearer {KEY}"},
            json={"receipt_token": payment_signature, "publisher_id": PUB, "resource": resource},
            timeout=20,
        )
        if r.status_code == 200:
            return {"ok": True, "secret": "🎉 gated content"}
        raise HTTPException(402, r.text)

    job = create_job(resource)
    body = payment_required_body(resource, job)
    header = encode_b64url(body)
    return JSONResponse(body, status_code=402, headers={"PAYMENT-REQUIRED": header})
```

Reference implementations:
- Meme vault (apex): `src/x402_pow/apps/demo_site.py`
- White paper Site (`demo.`): `src/x402_pow/apps/whitepaper_site.py` — enrolls via `POST /v1/publishers` like a real Site

---

## 7. Check your balance

```bash
curl -sS "$X402_POW_POOL_URL/v1/publishers/me" \
  -H "authorization: Bearer $X402_POW_API_KEY"
```

Or [signup.spammingbitcoin.com/balance](https://signup.spammingbitcoin.com/balance).

Each accepted access share credits **1 share unit** to your Site on the Pool ledger. PROP / sats payout from the treasury is separate (custodial for now).

---

## 8. Optional — Stratum hardware

Agents can also mine on `pool.spammingbitcoin.com:3333` with user `pub_….worker` / pass `x`. That credits your Site for edge shares and may forward pool-grade work Upstream. HTTP access gating still uses `/v1/jobs` + receipts as above.

---

## 9. Security checklist

- Keep `apiKey` secret (settle + identity).
- Bind every job to a stable absolute `resource` URL; never reuse a receipt across resources.
- Prefer Pool `/v1/settle` so receipts are single-use globally.
- Do not expose raw gated files under a public static path.
- Rotate keys by enrolling a new Site if compromised (PoC has no rotate endpoint yet).

---

## 10. Agent enroll (no human)

Agents that *are* Sites can enroll themselves with §1 Option A, store the JSON, and gate their own endpoints — no browser required.
