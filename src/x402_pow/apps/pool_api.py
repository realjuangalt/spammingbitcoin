from __future__ import annotations

import time
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel
from starlette.requests import Request

from x402_pow.config import ROOT, get_settings, mask_upstream_user
from x402_pow.facilitator.settle import SettleError, settle_receipt, submit_share
from x402_pow.pool.metrics import (
    format_difficulty,
    format_hashrate,
    format_uptime,
    metrics,
)
from x402_pow.pow.jobs import job_manager
from x402_pow.publishers import service as publishers
from x402_pow.web.jinja import make_templates

app = FastAPI(title="x402-pow pool API", version="0.1.0")

STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)
TEMPLATES = make_templates()

# Miner script only — memes are gated on the demo site, not mirrored here.
MINE_SCRIPT = ROOT / "src" / "x402_pow" / "scripts" / "mine_share.py"


class EnrollRequest(BaseModel):
    origin: str
    display_name: str
    access_zero_bits: int | None = None
    payout_address: str | None = None
    lightning_address: str | None = None
    contact: str | None = None


class JobRequest(BaseModel):
    publisher_id: str
    resource: str
    access_zero_bits: int | None = None
    capability: str | None = None


class ShareSubmit(BaseModel):
    job_id: str
    nonce: int
    publisher_id: str
    resource: str


class SettleRequest(BaseModel):
    receipt_token: str
    publisher_id: str | None = None
    resource: str | None = None


class VoteRequest(BaseModel):
    vote_token: str
    direction: str | int = "up"
    board: str | None = "agent"


def require_api_key(authorization: str | None = Header(default=None)) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Bearer apiKey required")
    key = authorization.split(" ", 1)[1].strip()
    pub = publishers.get_by_api_key(key)
    if pub is None:
        raise HTTPException(401, "invalid apiKey")
    return pub.id


@app.get("/health")
def health():
    settings = get_settings()
    out: dict = {
        "ok": True,
        "service": "x402-pow-api",
        "role": "pool",
        "network": settings.active_network,
        "bitcoinNetwork": settings.bitcoin_network_id,
        "upstreamMode": settings.upstream_mode,
        "blink": {
            "apiKeySet": bool(settings.blink_api_key.strip()),
            "walletIdSet": bool(settings.blink_wallet_id.strip()),
            "payoutMinSats": settings.payout_min_sats,
        },
    }
    if settings.upstream_mode == "reseller":
        from x402_pow.stratum.edge import stratum_edge
        from x402_pow.stratum.upstream import upstream_client

        out["upstream"] = upstream_client.status
        out["stratum"] = stratum_edge.status
    return out


@app.on_event("startup")
def _startup_pool() -> None:
    import logging

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = get_settings()
    from x402_pow.ledger.db import get_engine

    get_engine()  # migrate publishers columns + payouts table
    metrics.ensure_schema()
    if settings.upstream_mode == "reseller":
        from x402_pow.stratum.edge import stratum_edge
        from x402_pow.stratum.upstream import upstream_client

        upstream_client.start()
        stratum_edge.start()


def _stats_payload() -> dict:
    settings = get_settings()
    live = []
    upstream = None
    stratum = None
    if settings.upstream_mode == "reseller":
        from x402_pow.stratum.edge import stratum_edge
        from x402_pow.stratum.upstream import upstream_client

        stratum = stratum_edge.status
        live = stratum.get("minerDetail") or []
        upstream = upstream_client.status
    snap = metrics.snapshot(live_miners=live, network=settings.active_network)
    snap["upstream"] = upstream
    snap["stratum"] = stratum
    # Prefer settings as source of truth for the live mode banner
    snap["network"] = settings.active_network
    snap["bitcoinNetwork"] = settings.bitcoin_network_id
    snap["assetLabel"] = "BTC sats" if settings.is_mainnet else "tBTC sats"
    up = settings.active_upstream()
    snap["upstreamProfile"] = {
        "network": up["network"],
        "host": f"{up['host']}:{up['port']}",
        # Public JSON — never expose the operator's real upstream account/worker.
        "user": mask_upstream_user(str(up["user"])),
    }
    return snap


@app.get("/v1/stats")
def stats_json():
    return _stats_payload()


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request):
    snap = _stats_payload()
    hr = snap["hashrate"]
    pool = snap.get("pool") or {}
    workers = [
        {
            **w,
            "hashrate_fmt": format_hashrate(w["hashrate1h"]),
            "best_diff_fmt": format_difficulty(w.get("bestDiff") or 0),
        }
        for w in snap.get("workers1h") or []
    ]
    leaderboard = [
        {
            **row,
            "hashrate_fmt": format_hashrate(row["hashrate1h"]),
            "best_diff_fmt": format_difficulty(row.get("bestDiff") or 0),
        }
        for row in snap.get("leaderboard") or []
    ]
    top_diffs = [
        {**row, "diff_fmt": format_difficulty(row.get("difficulty") or 0)}
        for row in snap.get("topDifficulties") or []
    ]
    upstream = snap.get("upstream") or {}
    stratum = snap.get("stratum") or {}
    settings = get_settings()
    edge_diff = float(stratum.get("edgeDifficulty") or settings.stratum_edge_difficulty)
    profile = snap.get("upstreamProfile") or {}
    other = snap.get("otherNetwork") or {}
    user = str(profile.get("user") or "")
    user_disp = (user[:10] + "…" + user[-6:]) if len(user) > 20 else (user or "—")
    return TEMPLATES.TemplateResponse(
        request,
        "pool_api/dashboard.html",
        {
            "generated": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(snap["generatedAt"])),
            "network": settings.active_network,
            "is_mainnet": settings.is_mainnet,
            "bitcoin_network": settings.bitcoin_network_id,
            "network_title": "Mainnet" if settings.is_mainnet else "Testnet4",
            "network_blurb": (
                "Real BTC · Braiins Upstream"
                if settings.is_mainnet
                else "Play money tBTC · Xaxa Testnet4 Upstream"
            ),
            "asset_label": snap.get("assetLabel") or ("BTC sats" if settings.is_mainnet else "tBTC sats"),
            "upstream_host": profile.get("host") or upstream.get("upstream") or "—",
            "upstream_user": user_disp,
            "upstream_diff": upstream.get("difficulty"),
            "upstream_job": upstream.get("jobId") or "—",
            "other_network": other.get("network"),
            "other_share_count": other.get("recentShareCount") or 0,
            "hr_1m": format_hashrate(hr["1m"]),
            "hr_5m": format_hashrate(hr["5m"]),
            "hr_15m": format_hashrate(hr.get("15m") or 0),
            "hr_1h": format_hashrate(hr["1h"]),
            "hr_6h": format_hashrate(hr.get("6h") or 0),
            "hr_1d": format_hashrate(hr.get("1d") or 0),
            "highest_diff": format_difficulty(pool.get("highestDifficulty") or 0),
            "edge_diff": format_difficulty(edge_diff),
            "uptime": format_uptime(pool.get("uptimeSec") or 0),
            "accept_pct": round(100.0 * (pool.get("acceptRate1h") or 0.0), 1),
            "accept_frac": pool.get("acceptRate1h") or 0.0,
            "live_count": len(snap.get("liveMiners") or []),
            "site_count": pool.get("sites") or len(snap.get("sitesLedger") or []),
            "live_miners": snap.get("liveMiners") or [],
            "shares": snap["shares"],
            "workers": workers,
            "leaderboard": leaderboard,
            "top_diffs": top_diffs,
            "history_json": snap.get("history") or [],
            "sites_ledger": snap.get("sitesLedger") or [],
            "recent": snap.get("recent") or [],
            "upstream_ok": bool(upstream.get("authorized")),
            "stratum_ok": bool(stratum.get("listening")),
            "stratum_port": settings.public_stratum_port,
            "github_repo": settings.public_github_repo.rstrip("/"),
        },
    )


@app.get("/", response_class=HTMLResponse)
def root(request: Request):
    # On stats.* this is the landing page; on api.* still useful.
    host = (request.headers.get("host") or "").split(":")[0].lower()
    if host.startswith("stats.") or host.startswith("api."):
        return dashboard(request)
    return HTMLResponse(
        "<p>x402-pow pool API. See <a href='/health'>/health</a>, "
        "<a href='/dashboard'>/dashboard</a>, <a href='/v1/stats'>/v1/stats</a>.</p>"
    )

@app.post("/v1/publishers")
def enroll(body: EnrollRequest):
    pub = publishers.enroll_publisher(
        origin=body.origin,
        display_name=body.display_name,
        access_zero_bits=body.access_zero_bits,
        payout_address=body.payout_address,
        lightning_address=body.lightning_address,
        contact=body.contact,
    )
    settings = get_settings()
    return {
        "publisherId": pub.id,
        "apiKey": pub.api_key,
        "portalUrl": publishers.portal_url(pub),
        "lightningAddress": pub.lightning_address,
        "stratum": {
            "host": settings.public_stratum_host,
            "port": settings.public_stratum_port,
            "user": pub.stratum_user,
            "pass": "x",
        },
        "facilitatorBaseUrl": settings.public_api_base.rstrip("/"),
        "network": settings.bitcoin_network_id,
        "payoutMinSats": settings.payout_min_sats,
        "defaults": {
            "scheme": "pool-share",
            "accessZeroBits": pub.access_zero_bits,
            "asset": "share",
        },
    }


@app.get("/v1/publishers/me")
def me(publisher_id: str = Depends(require_api_key)):
    pub = publishers.get_by_id(publisher_id)
    if pub is None:
        raise HTTPException(404)
    from x402_pow.payouts import unpaid_sats

    if not pub.portal_token:
        publishers.ensure_portal_token(pub.id)
        pub = publishers.get_by_id(publisher_id) or pub
    return {
        "publisherId": pub.id,
        "displayName": pub.display_name,
        "origin": pub.origin,
        "shareUnits": pub.share_units,
        "tbtcSats": pub.tbtc_sats,
        "paidSats": getattr(pub, "paid_sats", 0) or 0,
        "unpaidSats": unpaid_sats(pub),
        "lightningAddress": pub.lightning_address,
        "portalUrl": publishers.portal_url(pub) if pub.portal_token else None,
        "accessZeroBits": pub.access_zero_bits,
        "stratumUser": pub.stratum_user,
        "payoutMinSats": get_settings().payout_min_sats,
    }


@app.post("/v1/jobs")
def create_job(body: JobRequest):
    from x402_pow.pow.access_tiers import normalize_capability, resolve_access_zero_bits

    pub = publishers.get_by_id(body.publisher_id)
    if pub is None:
        raise HTTPException(404, "unknown publisher")
    capability = normalize_capability(body.capability)
    cpu_bits = body.access_zero_bits if body.access_zero_bits is not None else pub.access_zero_bits
    bits = resolve_access_zero_bits(cpu_bits, capability)
    job = job_manager.create_job(
        publisher_id=body.publisher_id,
        resource=body.resource,
        zero_bits=bits,
        capability=capability,
    )
    return {
        "jobId": job.job_id,
        "prefix76": job.prefix76_hex,
        "accessTarget": hex(job.access_target),
        "zeroBits": job.zero_bits,
        "capability": job.capability,
        "expiresAt": job.expires_at,
        "source": job.source,
        "upstreamJobId": job.upstream_meta.get("upstream_job_id"),
    }


@app.post("/v1/shares/submit")
def shares_submit(body: ShareSubmit):
    try:
        receipt = submit_share(
            job_id=body.job_id,
            nonce=body.nonce,
            publisher_id=body.publisher_id,
            resource=body.resource,
        )
    except SettleError as e:
        raise HTTPException(e.status, str(e)) from e
    return {
        "receiptId": receipt.receipt_id,
        "receiptToken": receipt.to_token(),
        "digest": receipt.digest_hex,
        "nonce": receipt.nonce,
        "publisherId": receipt.publisher_id,
        "resource": receipt.resource,
    }


@app.post("/v1/settle")
def settle(body: SettleRequest, publisher_id: str = Depends(require_api_key)):
    try:
        receipt = settle_receipt(
            body.receipt_token,
            expected_publisher=body.publisher_id or publisher_id,
            expected_resource=body.resource,
        )
    except SettleError as e:
        raise HTTPException(e.status, str(e)) from e
    return {"ok": True, "receiptId": receipt.receipt_id, "digest": receipt.digest_hex}


@app.post("/v1/memes/vote")
def memes_vote(body: VoteRequest):
    from x402_pow import memes as meme_catalog
    from x402_pow.ledger.db import record_vote
    from x402_pow.pow.meme_votes import (
        normalize_board,
        normalize_direction,
        vote_weight_from_bits,
        work_from_bits,
    )
    from x402_pow.scheme.vote_tokens import load_vote_token

    try:
        claim = load_vote_token(body.vote_token)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    board = normalize_board(body.board)
    direction = normalize_direction(body.direction)
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
        raise HTTPException(409, "this unlock already voted")
    meme = meme_catalog.meme_by_id(claim["meme_id"]) or {}
    return {
        "ok": True,
        "memeId": claim["meme_id"],
        "title": meme.get("title"),
        "board": board,
        "direction": direction,
        "achievedBits": achieved,
        "weight": round(weight, 2),
    }


@app.get("/v1/memes/leaderboard")
def memes_leaderboard(board: str | None = None, limit: int = 25):
    from x402_pow import memes as meme_catalog
    from x402_pow.ledger.db import meme_scores
    from x402_pow.pow.meme_votes import normalize_board

    b = normalize_board(board) if board else None
    rows = meme_scores(board=b, limit=max(1, min(100, limit)))
    out = []
    for r in rows:
        meme = meme_catalog.meme_by_id(r["meme_id"]) or {}
        out.append(
            {
                "memeId": r["meme_id"],
                "title": meme.get("title"),
                "credit": meme.get("credit"),
                "score": round(r["score"], 2),
                "upWeight": round(r["up_weight"], 2),
                "downWeight": round(r["down_weight"], 2),
                "votes": r["votes"],
            }
        )
    return {"board": b or "all", "memes": out}


@app.get("/static/mine_share.py")
def mine_script():
    # Serve a self-contained copy (stdlib only) for agent fetch-and-run
    path = STATIC_DIR / "mine_share.py"
    if not path.exists():
        # fallback to package script
        path = MINE_SCRIPT
    return FileResponse(path, media_type="text/x-python", filename="mine_share.py")


def main() -> None:
    import uvicorn

    get_settings()  # ensure data dir
    uvicorn.run("x402_pow.apps.pool_api:app", host="0.0.0.0", port=8100, reload=False)


# Ensure package path for uvicorn string import
# Re-export app at module level: x402_pow.apps.pool_api
