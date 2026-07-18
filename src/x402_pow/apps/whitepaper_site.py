from __future__ import annotations

"""
Demo Site: Bitcoin white paper gated by PoW.

Enrolls with the Pool over the public HTTP API (same path a real Site uses),
so ledger / dashboard tracking is independent of the meme vault on the apex domain.
"""

import json
from pathlib import Path

import httpx
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from x402_pow.config import DATA_DIR, ROOT, get_settings
from x402_pow.facilitator.settle import SettleError, settle_receipt
from x402_pow.publishers import service as publishers
from x402_pow.scheme.media_tokens import issue_media_token, load_media_token
from x402_pow.scheme.payment import encode_payment_required, payment_required
from x402_pow.web.jinja import make_templates

TEMPLATES = make_templates()
PAPER_DIR = ROOT / "apps" / "whitepaper_site" / "static"
PAPER_FILE = "bitcoin.pdf"
CRED_PATH = DATA_DIR / "whitepaper_publisher.json"

app = FastAPI(title="x402-pow white paper demo Site")

_pub_id: str | None = None
_api_key: str | None = None


def api_base() -> str:
    return get_settings().public_api_base.rstrip("/")


def leading_zero_bits_from_le_hex(digest_le_hex: str) -> int:
    try:
        be = bytes.fromhex(digest_le_hex)[::-1]
    except ValueError:
        return 0
    value = int.from_bytes(be, "big")
    if value == 0:
        return 256
    return 256 - value.bit_length()


def ensure_whitepaper_publisher() -> tuple[str, str]:
    """Load or enroll this Site via POST /v1/publishers (public Pool API)."""
    global _pub_id, _api_key
    if _pub_id and _api_key:
        return _pub_id, _api_key

    settings = get_settings()
    if CRED_PATH.exists():
        data = json.loads(CRED_PATH.read_text())
        _pub_id, _api_key = data["publisherId"], data["apiKey"]
        return _pub_id, _api_key

    origin = f"https://demo.{settings.domain}"
    r = httpx.post(
        f"{api_base()}/v1/publishers",
        json={
            "origin": origin,
            "display_name": "Bitcoin White Paper",
            "access_zero_bits": settings.access_zero_bits,
            "contact": "demo-whitepaper@spammingbitcoin.com",
        },
        timeout=30.0,
    )
    r.raise_for_status()
    body = r.json()
    _pub_id, _api_key = body["publisherId"], body["apiKey"]
    CRED_PATH.parent.mkdir(parents=True, exist_ok=True)
    CRED_PATH.write_text(
        json.dumps(
            {
                "publisherId": _pub_id,
                "apiKey": _api_key,
                "portalUrl": body.get("portalUrl"),
                "origin": origin,
                "stratum": body.get("stratum"),
            },
            indent=2,
        )
        + "\n"
    )
    return _pub_id, _api_key


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


def unlocked_context(receipt, *, elapsed_sec=None, hashes=None, hashrate=None, job_source=None) -> dict:
    token = issue_media_token(file=PAPER_FILE, receipt_id=receipt.receipt_id)
    achieved = leading_zero_bits_from_le_hex(receipt.digest_hex)
    return {
        "title": "White paper unlocked",
        "paper_url": f"/paper/file/{token}",
        "solution": {
            "receipt_id": receipt.receipt_id,
            "job_id": receipt.job_id,
            "nonce": receipt.nonce,
            "digest": receipt.digest_hex,
            "target_zero_bits": receipt.zero_bits,
            "achieved_zero_bits": achieved,
            "elapsed_sec": elapsed_sec,
            "hashes": hashes,
            "hashrate": hashrate,
            "source": job_source or "reseller",
        },
    }


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    settings = get_settings()
    pub_id, _ = ensure_whitepaper_publisher()
    pub = publishers.get_by_id(pub_id)
    return TEMPLATES.TemplateResponse(
        request,
        "whitepaper_site/index.html",
        {
            "title": "Bitcoin White Paper — PoW demo",
            "domain": settings.domain,
            "publisher_id": pub_id,
            "portal_url": publishers.portal_url(pub) if pub else "",
            "access_bits": pub.access_zero_bits if pub else settings.access_zero_bits,
            "github_repo": settings.public_github_repo.rstrip("/"),
        },
    )


@app.get("/paper", response_class=HTMLResponse)
async def paper(request: Request):
    pub_id, api_key = ensure_whitepaper_publisher()
    resource = f"{request.url.scheme}://{request.url.netloc}/paper"

    token = request.headers.get("PAYMENT-SIGNATURE")
    if token:
        try:
            receipt = settle_receipt(token, expected_publisher=pub_id, expected_resource=resource)
            return TEMPLATES.TemplateResponse(
                request,
                "whitepaper_site/unlocked.html",
                unlocked_context(receipt),
            )
        except SettleError:
            pass

    pub = publishers.get_by_id(pub_id)
    assert pub is not None
    job = create_job_via_api(pub_id, resource, capability="cpu")
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
        "whitepaper_site/paywall.html",
        {
            "title": "Payment Required — Bitcoin white paper",
            "payment": body,
            "payment_header": header_b64,
            "job": job_view,
            "api_base": api_base(),
            "publisher_id": pub_id,
            "resource": resource,
            "api_key": api_key,
            "access_max_seconds": settings.access_max_seconds,
            "job_endpoint": "/paper/job",
            "mine_endpoint": "/paper/mine-browser",
            "unlock_action": "/paper/unlock",
        },
        status_code=402,
        headers={"PAYMENT-REQUIRED": header_b64},
    )


@app.post("/paper/unlock", response_class=HTMLResponse)
async def unlock(
    request: Request,
    receipt_token: str = Form(...),
    elapsed_sec: float | None = Form(default=None),
    hashes: int | None = Form(default=None),
    hashrate: float | None = Form(default=None),
    job_source: str | None = Form(default=None),
):
    pub_id, _ = ensure_whitepaper_publisher()
    try:
        receipt = settle_receipt(receipt_token, expected_publisher=pub_id)
    except SettleError as e:
        return HTMLResponse(f"<h1>Settle failed</h1><pre>{e}</pre>", status_code=402)

    return TEMPLATES.TemplateResponse(
        request,
        "whitepaper_site/unlocked.html",
        unlocked_context(
            receipt,
            elapsed_sec=elapsed_sec,
            hashes=hashes,
            hashrate=hashrate,
            job_source=job_source,
        ),
    )


@app.get("/paper/file/{token}")
async def paper_file(token: str):
    try:
        data = load_media_token(token)
    except ValueError as e:
        raise HTTPException(401, str(e)) from e

    if data["file"] != PAPER_FILE:
        raise HTTPException(404, "not found")

    path = (PAPER_DIR / PAPER_FILE).resolve()
    root = PAPER_DIR.resolve()
    if not str(path).startswith(str(root)) or not path.is_file():
        raise HTTPException(404, "not found")

    return FileResponse(
        path,
        media_type="application/pdf",
        filename="bitcoin.pdf",
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": 'inline; filename="bitcoin.pdf"',
        },
    )


@app.post("/paper/mine-browser")
async def mine_browser(request: Request):
    data = await request.json()
    pub_id, _ = ensure_whitepaper_publisher()
    resource = f"{request.url.scheme}://{request.url.netloc}/paper"
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


@app.post("/paper/job")
async def mint_job(request: Request):
    data = await request.json()
    pub_id, _ = ensure_whitepaper_publisher()
    resource = f"{request.url.scheme}://{request.url.netloc}/paper"
    capability = str(data.get("capability") or "cpu")
    job = create_job_via_api(pub_id, resource, capability=capability)
    return {"ok": True, **job_view_from_api(job), "accessTargetHex": job["accessTarget"]}


def main() -> None:
    import uvicorn

    get_settings()
    ensure_whitepaper_publisher()
    uvicorn.run("x402_pow.apps.whitepaper_site:app", host="0.0.0.0", port=8103, reload=False)
