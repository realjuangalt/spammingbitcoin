from __future__ import annotations

import base64
import json
from typing import Any

from x402_pow.config import get_settings


def b64url_json(obj: dict[str, Any]) -> str:
    raw = json.dumps(obj, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def payment_required(
    *,
    resource_url: str,
    publisher_id: str,
    access_zero_bits: int,
    job_id: str,
    prefix76_hex: str,
    access_target: int,
    description: str = "Bitcoin PoW required",
    capability: str = "cpu",
) -> dict[str, Any]:
    settings = get_settings()
    miner_url = f"{settings.public_api_base.rstrip('/')}/static/mine_share.py"
    return {
        "x402Version": 2,
        "error": description,
        "resource": {
            "url": resource_url,
            "description": "OG Bitcoin meme (random on unlock)",
            "mimeType": "text/html",
        },
        "accepts": [
            {
                "scheme": "pool-share",
                "network": settings.bitcoin_network_id,
                "amount": str(access_zero_bits),
                "asset": "share",
                "payTo": publisher_id,
                "maxTimeoutSeconds": settings.access_max_seconds + 60,
                "extra": {
                    "stratumScheme": "reseller-http" if settings.upstream_mode == "reseller" else "local-http",
                    "stratumHost": settings.public_stratum_host,
                    "stratumPort": settings.public_stratum_port,
                    "jobId": job_id,
                    "prefix76": prefix76_hex,
                    "accessTarget": hex(access_target),
                    "zeroBits": access_zero_bits,
                    "capability": capability,
                    "facilitator": settings.public_api_base.rstrip("/"),
                    "resourceBinding": resource_url,
                    "minerHint": {
                        "kind": "shell",
                        "summary": "Fetch miner; try GPU (hard job) then CPU fallback; print receipt JSON",
                        "commands": [
                            f"curl -fsSL {miner_url} -o /tmp/mine_share.py",
                            f"python3 /tmp/mine_share.py --api {settings.public_api_base.rstrip('/')} "
                            f"--publisher {publisher_id} --resource {resource_url} --capability auto --receipt-out -",
                        ],
                    },
                },
            }
        ],
        "extensions": {},
    }


def encode_payment_required(body: dict[str, Any]) -> str:
    return b64url_json(body)
