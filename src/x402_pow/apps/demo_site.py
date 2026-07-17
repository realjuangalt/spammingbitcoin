from __future__ import annotations

import json
import random

import httpx
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from x402_pow.config import DATA_DIR, MEMES_DIR, ROOT, get_settings
from x402_pow.facilitator.settle import SettleError, settle_receipt
from x402_pow.publishers import service as publishers
from x402_pow.scheme.media_tokens import issue_media_token, load_media_token
from x402_pow.scheme.payment import encode_payment_required, payment_required
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
    manifest = MEMES_DIR / "manifest.json"
    if not manifest.exists():
        return []
    return json.loads(manifest.read_text()).get("memes", [])


def pick_meme() -> dict | None:
    memes = load_meme_manifest()
    if not memes:
        return None
    return random.choice(memes)


def create_job_via_api(publisher_id: str, resource: str, zero_bits: int) -> dict:
    r = httpx.post(
        f"{api_base()}/v1/jobs",
        json={
            "publisher_id": publisher_id,
            "resource": resource,
            "access_zero_bits": zero_bits,
        },
        timeout=30.0,
    )
    r.raise_for_status()
    return r.json()


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


def unlocked_context(
    meme_item: dict | None,
    receipt,
    *,
    elapsed_sec: float | None = None,
    hashes: int | None = None,
    hashrate: float | None = None,
    job_source: str | None = None,
) -> dict:
    media_url = None
    if meme_item and meme_item.get("file"):
        token = issue_media_token(file=meme_item["file"], receipt_id=receipt.receipt_id)
        media_url = f"/meme/media/{token}"

    achieved = leading_zero_bits_from_le_hex(receipt.digest_hex)
    solution = {
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
    }
    return {
        "title": "Unlocked",
        "meme": meme_item,
        "media_url": media_url,
        "solution": solution,
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
    job = create_job_via_api(pub_id, resource, pub.access_zero_bits)
    job_view = {
        "job_id": job["jobId"],
        "prefix76_hex": job["prefix76"],
        "access_target": int(job["accessTarget"], 0),
        "zero_bits": job["zeroBits"],
        "source": job.get("source", "local"),
    }
    body = payment_required(
        resource_url=resource,
        publisher_id=pub_id,
        access_zero_bits=job_view["zero_bits"],
        job_id=job_view["job_id"],
        prefix76_hex=job_view["prefix76_hex"],
        access_target=job_view["access_target"],
    )
    header_b64 = encode_payment_required(body)
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
    except SettleError as e:
        return HTMLResponse(f"<h1>Settle failed</h1><pre>{e}</pre>", status_code=402)

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


def main() -> None:
    import uvicorn

    get_settings()
    ensure_demo_publisher()
    uvicorn.run("x402_pow.apps.demo_site:app", host="0.0.0.0", port=8101, reload=False)
