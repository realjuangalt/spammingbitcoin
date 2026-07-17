from __future__ import annotations

from fastapi import FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from x402_pow.config import ROOT, get_settings
from x402_pow.payouts import list_payouts, try_payout_publisher, unpaid_sats
from x402_pow.publishers import service as publishers

TEMPLATES = Jinja2Templates(directory=str(ROOT / "templates" / "pool_signup"))
DOCS_MD = ROOT / "docs" / "site-owners.md"

app = FastAPI(title="x402-pow pool signup")


def _portal_context(pub, *, flash: str | None = None, flash_err: bool = False) -> dict:
    settings = get_settings()
    token = pub.portal_token or publishers.ensure_portal_token(pub.id) or ""
    # refresh after backfill
    if not pub.portal_token and token:
        pub = publishers.get_by_portal_token(token) or pub
    return {
        "title": f"{pub.display_name} — Site portal",
        "pub": pub,
        "token": token,
        "portal_url": publishers.portal_url(pub) if token else "",
        "api_base": settings.public_api_base.rstrip("/"),
        "stratum_host": settings.public_stratum_host,
        "stratum_port": settings.public_stratum_port,
        "unpaid": unpaid_sats(pub),
        "min_sats": settings.payout_min_sats,
        "payouts": list_payouts(pub.id),
        "flash": flash,
        "flash_err": flash_err,
        "domain": settings.domain,
        "github_repo": settings.public_github_repo.rstrip("/"),
    }


@app.get("/", response_class=HTMLResponse)
async def signup_form(request: Request):
    settings = get_settings()
    return TEMPLATES.TemplateResponse(
        request,
        "signup.html",
        {
            "title": "Enroll — Spamming Bitcoin",
            "default_bits": settings.access_zero_bits,
            "domain": settings.domain,
            "api_base": settings.public_api_base.rstrip("/"),
            "min_sats": settings.payout_min_sats,
            "pool_fee_percent": settings.pool_fee_percent,
            "site_payout_percent": 100 - settings.pool_fee_percent,
            "github_repo": settings.public_github_repo.rstrip("/"),
        },
    )


@app.get("/docs")
async def docs_redirect():
    return RedirectResponse(url="/#guide", status_code=302)


@app.get("/docs.md")
async def docs_markdown():
    if not DOCS_MD.is_file():
        return HTMLResponse("docs missing", status_code=404)
    return FileResponse(DOCS_MD, media_type="text/markdown; charset=utf-8", filename="site-owners.md")


@app.post("/enroll", response_class=HTMLResponse)
async def enroll(
    request: Request,
    origin: str = Form(...),
    display_name: str = Form(...),
    access_zero_bits: int = Form(24),
    lightning_address: str = Form(""),
    payout_address: str = Form(""),
    contact: str = Form(""),
):
    settings = get_settings()
    pub = publishers.enroll_publisher(
        origin=origin,
        display_name=display_name,
        access_zero_bits=access_zero_bits,
        lightning_address=lightning_address or None,
        payout_address=payout_address or None,
        contact=contact or None,
    )
    return TEMPLATES.TemplateResponse(
        request,
        "credentials.html",
        {
            "title": "You're enrolled",
            "pub": pub,
            "portal_url": publishers.portal_url(pub),
            "api_base": settings.public_api_base.rstrip("/"),
            "stratum_host": settings.public_stratum_host,
            "stratum_port": settings.public_stratum_port,
            "domain": settings.domain,
            "min_sats": settings.payout_min_sats,
            "pool_fee_percent": settings.pool_fee_percent,
            "site_payout_percent": 100 - settings.pool_fee_percent,
            "github_repo": settings.public_github_repo.rstrip("/"),
        },
    )


@app.get("/s/{token}", response_class=HTMLResponse)
async def site_portal(request: Request, token: str):
    pub = publishers.get_by_portal_token(token)
    if pub is None:
        return HTMLResponse("<h1>Unknown or invalid portal link</h1>", status_code=404)
    return TEMPLATES.TemplateResponse(request, "portal.html", _portal_context(pub))


@app.post("/s/{token}/ln", response_class=HTMLResponse)
async def site_update_ln(request: Request, token: str, lightning_address: str = Form("")):
    pub = publishers.get_by_portal_token(token)
    if pub is None:
        return HTMLResponse("<h1>Unknown portal link</h1>", status_code=404)
    pub = publishers.update_lightning_address(pub.id, lightning_address) or pub
    return TEMPLATES.TemplateResponse(
        request,
        "portal.html",
        _portal_context(pub, flash="Lightning address saved."),
    )


@app.post("/s/{token}/payout", response_class=HTMLResponse)
async def site_payout(request: Request, token: str):
    pub = publishers.get_by_portal_token(token)
    if pub is None:
        return HTMLResponse("<h1>Unknown portal link</h1>", status_code=404)
    result = try_payout_publisher(pub.id, force=True)
    pub = publishers.get_by_id(pub.id) or pub
    ok = bool(result.get("ok"))
    if ok:
        flash = f"Payout submitted: {result.get('amountSats')} sats → {result.get('lightningAddress')} ({result.get('blinkStatus')})"
    else:
        flash = f"Payout not sent: {result.get('error')}" + (
            f" — {result.get('hint')}" if result.get("hint") else ""
        )
    return TEMPLATES.TemplateResponse(
        request,
        "portal.html",
        _portal_context(pub, flash=flash, flash_err=not ok),
    )


@app.get("/balance", response_class=HTMLResponse)
async def balance_form(request: Request):
    return TEMPLATES.TemplateResponse(request, "balance.html", {"title": "Site balance"})


@app.post("/balance", response_class=HTMLResponse)
async def balance_lookup(request: Request, api_key: str = Form(...)):
    pub = publishers.get_by_api_key(api_key.strip())
    if pub is None:
        # also allow pasting portal token
        pub = publishers.get_by_portal_token(api_key.strip())
    if pub is None:
        return HTMLResponse("<h1>Unknown apiKey / portal token</h1>", status_code=404)
    if pub.portal_token:
        return RedirectResponse(url=f"/s/{pub.portal_token}", status_code=303)
    return TEMPLATES.TemplateResponse(
        request,
        "balance_result.html",
        {"title": "Balance", "pub": pub},
    )


def main() -> None:
    import uvicorn

    get_settings()
    uvicorn.run("x402_pow.apps.pool_signup:app", host="0.0.0.0", port=8102, reload=False)
