from __future__ import annotations

import json
import time

import httpx
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from x402_pow import memes as meme_catalog
from x402_pow.config import DATA_DIR, MEMES_DIR, ROOT, get_settings
from x402_pow.facilitator.settle import SettleError, settle_receipt
from x402_pow.ledger.db import meme_scores, record_vote
from x402_pow.pow.meme_votes import normalize_direction, vote_weight_from_bits, work_from_bits
from x402_pow.publishers import service as publishers
from x402_pow.scheme.media_tokens import issue_media_token, load_media_token
from x402_pow.scheme.payment import encode_payment_required, payment_required
from x402_pow.scheme.vote_tokens import issue_vote_token, load_vote_token
from x402_pow.web.jinja import make_templates

TEMPLATES = make_templates()

app = FastAPI(title="x402-pow demo meme site")
# Memes are NOT mounted publicly — only via /meme/media/{token} after PoW.

_demo_pub_id: str | None = None
_demo_api_key: str | None = None


def api_base() -> str:
    return get_settings().public_api_base.rstrip("/")


def ensure_demo_publisher() -> tuple[str, str]:
    global _demo_pub_id, _demo_api_key
    if _demo_pub_id and _demo_api_key:
        return _demo_pub_id, _demo_api_key
    settings = get_settings()
    cred_path = ROOT / "data" / "demo_publisher.json"
    if cred_path.exists():
        data = json.loads(cred_path.read_text())
        _demo_pub_id, _demo_api_key = data["publisherId"], data["apiKey"]
        return _demo_pub_id, _demo_api_key
    pub = publishers.enroll_publisher(
        origin=f"https://{settings.domain}",
        display_name="Spamming Bitcoin Memes",
        access_zero_bits=settings.access_zero_bits,
    )
    _demo_pub_id, _demo_api_key = pub.id, pub.api_key
    cred_path.parent.mkdir(parents=True, exist_ok=True)
    cred_path.write_text(json.dumps({"publisherId": pub.id, "apiKey": pub.api_key}, indent=2))
    return _demo_pub_id, _demo_api_key


def load_meme_manifest() -> list[dict]:
    return meme_catalog.load_manifest()


def pick_meme() -> dict | None:
    return meme_catalog.pick_random()


def create_job_via_api(
    publisher_id: str,
    resource: str,
    *,
    capability: str = "cpu",
    zero_bits: int | None = None,
) -> dict:
    payload = {"publisher_id": publisher_id, "resource": resource, "capability": capability}
    if zero_bits is not None:
        payload["access_zero_bits"] = zero_bits
    r = httpx.post(f"{api_base()}/v1/jobs", json=payload, timeout=30.0)
    r.raise_for_status()
    return r.json()


def job_view_from_api(job: dict) -> dict:
    return {
        "job_id": job["jobId"],
        "prefix76_hex": job["prefix76"],
        "access_target": int(job["accessTarget"], 0),
        "zero_bits": job["zeroBits"],
        "source": job.get("source", "local"),
        "capability": job.get("capability", "cpu"),
    }


def leading_zero_bits_from_le_hex(digest_le_hex: str) -> int:
    """Count leading zero bits of the big-endian digest (Bitcoin hash display is LE)."""
    try:
        be = bytes.fromhex(digest_le_hex)[::-1]
    except ValueError:
        return 0
    value = int.from_bytes(be, "big")
    if value == 0:
        return 256
    return 256 - value.bit_length()


MINER_ENDPOINTS = {
    "job_endpoint": "/meme/job",
    "mine_endpoint": "/meme/mine-browser",
    "next_endpoint": "/meme/next",
    "unlock_action": "/meme/unlock",
    "vote_endpoint": "/meme/vote",
    "leaderboard_endpoint": "/meme/leaderboard",
}


def unlock_payload(
    meme_item: dict | None,
    receipt,
    *,
    elapsed_sec: float | None = None,
    hashes: int | None = None,
    hashrate: float | None = None,
    job_source: str | None = None,
) -> dict:
    """Core unlock result (meme + media URL + solution) shared by HTML and JSON."""
    media_url = None
    vote_token = None
    if meme_item and meme_item.get("file"):
        token = issue_media_token(file=meme_item["file"], receipt_id=receipt.receipt_id)
        media_url = f"/meme/media/{token}"

    achieved = leading_zero_bits_from_le_hex(receipt.digest_hex)

    if meme_item and meme_item.get("id"):
        vote_token = issue_vote_token(
            receipt_id=receipt.receipt_id,
            meme_id=str(meme_item["id"]),
            achieved_bits=achieved,
            publisher_id=receipt.publisher_id,
            resource=receipt.resource,
        )

    solution = {
        "receipt_id": receipt.receipt_id,
        "job_id": receipt.job_id,
        "nonce": receipt.nonce,
        "digest": receipt.digest_hex,
        "target_zero_bits": receipt.zero_bits,
        "achieved_zero_bits": achieved,
        "vote_weight": round(vote_weight_from_bits(achieved), 2),
        "elapsed_sec": elapsed_sec,
        "hashes": hashes,
        "hashrate": hashrate,
        "source": job_source or "reseller",
    }
    return {
        "meme": meme_item,
        "media_url": media_url,
        "vote_token": vote_token,
        "solution": solution,
    }


def unlocked_context(
    meme_item: dict | None,
    receipt,
    *,
    elapsed_sec: float | None = None,
    hashes: int | None = None,
    hashrate: float | None = None,
    job_source: str | None = None,
) -> dict:
    payload = unlock_payload(
        meme_item,
        receipt,
        elapsed_sec=elapsed_sec,
        hashes=hashes,
        hashrate=hashrate,
        job_source=job_source,
    )
    settings = get_settings()
    return {
        "title": "Unlocked",
        "access_max_seconds": settings.access_max_seconds,
        "window_seconds": settings.meme_window_seconds,
        **MINER_ENDPOINTS,
        **payload,
    }


def unlock_json(payload: dict) -> dict:
    """Slim, safe JSON body for the in-place New-meme swap (never leaks the raw file path)."""
    meme = payload.get("meme") or None
    meme_public = None
    if meme:
        meme_public = {
            "title": meme.get("title"),
            "credit": meme.get("credit"),
            "sourceUrl": meme.get("sourceUrl"),
        }
    return {
        "ok": True,
        "meme": meme_public,
        "media_url": payload.get("media_url"),
        "vote_token": payload.get("vote_token"),
        "solution": payload.get("solution"),
    }


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    settings = get_settings()
    return TEMPLATES.TemplateResponse(
        request,
        "demo_site/index.html",
        {
            "title": "Spamming Bitcoin — PoW meme vault",
            "domain": settings.domain,
            "github_repo": settings.public_github_repo.rstrip("/"),
        },
    )


@app.get("/bench", response_class=HTMLResponse)
async def bench(request: Request):
    """Client-side SHA-256d benchmark to calibrate access difficulty per machine."""
    from x402_pow.pow.access_tiers import resolve_access_zero_bits

    settings = get_settings()
    cpu_bits = settings.access_zero_bits
    return TEMPLATES.TemplateResponse(
        request,
        "demo_site/bench.html",
        {
            "title": "PoW benchmark — Spamming Bitcoin",
            "cpu_bits": cpu_bits,
            "gpu_bits": resolve_access_zero_bits(cpu_bits, "gpu"),
            "max_seconds": settings.access_max_seconds,
        },
    )


@app.post("/bench/log")
async def bench_log(request: Request):
    """Append a benchmark result to logs/bench.log (and the server log)."""
    try:
        data = await request.json()
    except Exception:
        data = {}

    keep = (
        "event",
        "duration_s",
        "lanes",
        "single_hps",
        "accel_hps",
        "speedup",
        "target_s",
        "rec_cpu_bits",
        "rec_gpu_bits",
        "cores",
        "device_memory",
        "webgpu",
        "webgpu_ok",
        "solve_bits",
        "samples",
        "results",
        "user_agent",
    )
    entry = {k: data.get(k) for k in keep if k in data}
    entry["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    entry["ip"] = request.client.host if request.client else None
    if "user_agent" not in entry:
        entry["user_agent"] = request.headers.get("user-agent")
    log_path = ROOT / "logs" / "bench.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(entry, ensure_ascii=False)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    print("[bench]", line, flush=True)
    return {"ok": True}


@app.get("/meme", response_class=HTMLResponse)
async def meme(request: Request):
    pub_id, api_key = ensure_demo_publisher()
    resource = f"{request.url.scheme}://{request.url.netloc}/meme"

    token = request.headers.get("PAYMENT-SIGNATURE")
    if token:
        try:
            receipt = settle_receipt(token, expected_publisher=pub_id, expected_resource=resource)
            meme_item = pick_meme()
            return TEMPLATES.TemplateResponse(
                request,
                "demo_site/unlocked.html",
                unlocked_context(meme_item, receipt),
            )
        except SettleError:
            pass

    pub = publishers.get_by_id(pub_id)
    assert pub is not None
    job = create_job_via_api(
        pub_id, resource, capability="cpu", zero_bits=get_settings().meme_unlock_zero_bits
    )
    job_view = job_view_from_api(job)
    body = payment_required(
        resource_url=resource,
        publisher_id=pub_id,
        access_zero_bits=job_view["zero_bits"],
        job_id=job_view["job_id"],
        prefix76_hex=job_view["prefix76_hex"],
        access_target=job_view["access_target"],
        capability=job_view["capability"],
    )
    header_b64 = encode_payment_required(body)
    settings = get_settings()
    return TEMPLATES.TemplateResponse(
        request,
        "demo_site/paywall.html",
        {
            "title": "Payment Required — Bitcoin PoW",
            "payment": body,
            "payment_header": header_b64,
            "job": job_view,
            "api_base": api_base(),
            "publisher_id": pub_id,
            "resource": resource,
            "api_key": api_key,
            "access_max_seconds": settings.access_max_seconds,
            "window_seconds": settings.meme_window_seconds,
            "job_endpoint": "/meme/job",
            "mine_endpoint": "/meme/mine-browser",
            "unlock_action": "/meme/unlock",
            "leaderboard_endpoint": "/meme/leaderboard",
        },
        status_code=402,
        headers={"PAYMENT-REQUIRED": header_b64},
    )


@app.post("/meme/unlock", response_class=HTMLResponse)
async def unlock(
    request: Request,
    receipt_token: str = Form(...),
    elapsed_sec: float | None = Form(default=None),
    hashes: int | None = Form(default=None),
    hashrate: float | None = Form(default=None),
    job_source: str | None = Form(default=None),
):
    pub_id, _ = ensure_demo_publisher()
    try:
        receipt = settle_receipt(receipt_token, expected_publisher=pub_id)
    except SettleError:
        return RedirectResponse(url="/", status_code=303)

    meme_item = pick_meme()
    return TEMPLATES.TemplateResponse(
        request,
        "demo_site/unlocked.html",
        unlocked_context(
            meme_item,
            receipt,
            elapsed_sec=elapsed_sec,
            hashes=hashes,
            hashrate=hashrate,
            job_source=job_source,
        ),
    )


@app.post("/meme/next")
async def next_meme(request: Request):
    """Settle a fresh receipt and return the next meme as JSON (in-place swap).

    Same security as /meme/unlock: the receipt is verified and marked single-use
    server-side, and media is only reachable via a per-receipt signed token.
    """
    data = await request.json()
    token = str(data.get("receipt_token") or "")
    if not token:
        return {"ok": False, "error": "missing receipt_token"}

    pub_id, _ = ensure_demo_publisher()
    try:
        receipt = settle_receipt(token, expected_publisher=pub_id)
    except SettleError as e:
        return {"ok": False, "error": str(e)}

    def _num(key):
        val = data.get(key)
        if isinstance(val, (int, float)):
            return val
        return None

    meme_item = pick_meme()
    payload = unlock_payload(
        meme_item,
        receipt,
        elapsed_sec=_num("elapsed_sec"),
        hashes=_num("hashes"),
        hashrate=_num("hashrate"),
        job_source=str(data["job_source"]) if data.get("job_source") else None,
    )
    return unlock_json(payload)


@app.get("/meme/media/{token}")
async def meme_media(token: str):
    """Serve a meme only with a short-lived token issued after successful PoW settle."""
    try:
        data = load_media_token(token)
    except ValueError as e:
        raise HTTPException(401, str(e)) from e

    path = (MEMES_DIR / data["file"]).resolve()
    memes_root = MEMES_DIR.resolve()
    if not str(path).startswith(str(memes_root)) or not path.is_file():
        raise HTTPException(404, "meme not found")

    suffix = path.suffix.lower()
    media = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }.get(suffix, "application/octet-stream")
    return FileResponse(
        path,
        media_type=media,
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.post("/meme/job")
async def mint_job(request: Request):
    """Mint a fixed-window meme job. Unlock target is easy + uniform; the browser
reports its declared capability only so we can label the faster hasher path."""
    data = await request.json()
    pub_id, _ = ensure_demo_publisher()
    resource = f"{request.url.scheme}://{request.url.netloc}/meme"
    capability = str(data.get("capability") or "cpu")
    job = create_job_via_api(
        pub_id, resource, capability=capability, zero_bits=get_settings().meme_unlock_zero_bits
    )
    return {"ok": True, **job_view_from_api(job), "accessTargetHex": job["accessTarget"]}


@app.post("/meme/mine-browser")
async def mine_browser(request: Request):
    data = await request.json()
    pub_id, _api_key = ensure_demo_publisher()
    resource = f"{request.url.scheme}://{request.url.netloc}/meme"
    r = httpx.post(
        f"{api_base()}/v1/shares/submit",
        json={
            "job_id": data["job_id"],
            "nonce": int(data["nonce"]),
            "publisher_id": pub_id,
            "resource": resource,
        },
        timeout=30.0,
    )
    if r.status_code >= 400:
        return {"ok": False, "error": r.text}
    body = r.json()
    return {"ok": True, "receiptToken": body["receiptToken"]}


@app.post("/meme/vote")
async def cast_vote(request: Request):
    """Cast one PoW-weighted vote on the human board using the unlock's vote token."""
    data = await request.json()
    token = str(data.get("vote_token") or "")
    if not token:
        return {"ok": False, "error": "missing vote_token"}
    try:
        claim = load_vote_token(token)
    except ValueError as e:
        return {"ok": False, "error": str(e)}

    from x402_pow.pow.meme_votes import normalize_board

    direction = normalize_direction(data.get("direction"))
    board = normalize_board(data.get("board"))
    achieved = int(claim["achieved_bits"])
    weight = vote_weight_from_bits(achieved)
    ok = record_vote(
        meme_id=claim["meme_id"],
        board=board,
        direction=direction,
        achieved_bits=achieved,
        work=work_from_bits(achieved),
        weight=weight,
        receipt_id=claim["receipt_id"],
        publisher_id=claim.get("publisher_id"),
        resource=claim.get("resource"),
    )
    if not ok:
        return {"ok": False, "error": "this unlock already voted"}
    return {
        "ok": True,
        "meme_id": claim["meme_id"],
        "board": board,
        "direction": direction,
        "weight": round(weight, 2),
    }


@app.get("/meme/leaderboard", response_class=HTMLResponse)
async def leaderboard(request: Request):
    """Two boards: what humans mine for vs what agents mine for."""

    def rows(board: str) -> list[dict]:
        out = []
        for r in meme_scores(board=board, limit=25):
            meme = meme_catalog.meme_by_id(r["meme_id"]) or {}
            out.append(
                {
                    **r,
                    "score": round(r["score"], 1),
                    "up_weight": round(r["up_weight"], 1),
                    "down_weight": round(r["down_weight"], 1),
                    "title": meme.get("title") or r["meme_id"],
                    "credit": meme.get("credit"),
                    "sourceUrl": meme.get("sourceUrl"),
                }
            )
        return out

    settings = get_settings()
    return TEMPLATES.TemplateResponse(
        request,
        "demo_site/leaderboard.html",
        {
            "title": "Meme leaderboards — PoW meritocracy",
            "domain": settings.domain,
            "github_repo": settings.public_github_repo.rstrip("/"),
            "human_board": rows("human"),
            "agent_board": rows("agent"),
            "vote_cap": settings.vote_weight_cap,
        },
    )


def main() -> None:
    import uvicorn

    get_settings()
    ensure_demo_publisher()
    uvicorn.run("x402_pow.apps.demo_site:app", host="0.0.0.0", port=8101, reload=False)
