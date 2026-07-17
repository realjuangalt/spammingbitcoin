from __future__ import annotations

"""Blink GraphQL client — custodial Lightning payouts from Pool treasury."""

import logging
from typing import Any

import httpx

from x402_pow.config import get_settings

log = logging.getLogger("x402_pow.blink")


class BlinkError(Exception):
    def __init__(self, message: str, *, details: Any = None):
        super().__init__(message)
        self.details = details


def _headers() -> dict[str, str]:
    settings = get_settings()
    key = settings.blink_api_key.strip()
    if not key:
        raise BlinkError("BLINK_API_KEY not set")
    return {
        "Content-Type": "application/json",
        "X-API-KEY": key,
    }


def graphql(query: str, variables: dict | None = None) -> dict:
    settings = get_settings()
    url = settings.blink_api_url.strip() or "https://api.blink.sv/graphql"
    payload = {"query": query, "variables": variables or {}}
    with httpx.Client(timeout=60.0) as client:
        r = client.post(url, headers=_headers(), json=payload)
        r.raise_for_status()
        data = r.json()
    if data.get("errors"):
        raise BlinkError("Blink GraphQL error", details=data["errors"])
    return data.get("data") or {}


def fetch_default_wallet_id() -> str | None:
    """Resolve BTC wallet id when BLINK_WALLET_ID is empty."""
    q = """
    query {
      me {
        defaultAccount {
          wallets {
            id
            walletCurrency
          }
        }
      }
    }
    """
    data = graphql(q)
    wallets = (
        ((data.get("me") or {}).get("defaultAccount") or {}).get("wallets") or []
    )
    for w in wallets:
        if (w.get("walletCurrency") or "").upper() == "BTC":
            return w.get("id")
    return wallets[0]["id"] if wallets else None


def resolve_wallet_id() -> str:
    settings = get_settings()
    wid = settings.blink_wallet_id.strip()
    if wid:
        return wid
    found = fetch_default_wallet_id()
    if not found:
        raise BlinkError("No Blink wallet id — set BLINK_WALLET_ID")
    return found


def send_to_lightning_address(*, ln_address: str, amount_sats: int) -> dict:
    """
    Pay a Lightning Address from the Pool treasury.
    Uses lnAddressPaymentSend (amount in satoshis).
    """
    if amount_sats < 1:
        raise BlinkError("amount_sats must be >= 1")
    addr = (ln_address or "").strip()
    if "@" not in addr:
        raise BlinkError("invalid lightning address")

    wallet_id = resolve_wallet_id()
    mutation = """
    mutation LnAddressPaymentSend($input: LnAddressPaymentSendInput!) {
      lnAddressPaymentSend(input: $input) {
        status
        errors { code message path }
      }
    }
    """
    data = graphql(
        mutation,
        {
            "input": {
                "walletId": wallet_id,
                "lnAddress": addr,
                "amount": int(amount_sats),
            }
        },
    )
    result = data.get("lnAddressPaymentSend") or {}
    errors = result.get("errors") or []
    if errors:
        raise BlinkError(errors[0].get("message") or "payment failed", details=errors)
    status = (result.get("status") or "").upper()
    log.info("Blink payout %s sats → %s status=%s", amount_sats, addr, status)
    return result


def blink_status() -> dict:
    settings = get_settings()
    out = {
        "configured": settings.blink_configured,
        "apiUrl": settings.blink_api_url,
        "walletIdSet": bool(settings.blink_wallet_id.strip()),
        "apiKeySet": bool(settings.blink_api_key.strip()),
        "payoutMinSats": settings.payout_min_sats,
    }
    if not settings.blink_api_key.strip():
        out["ok"] = False
        out["error"] = "BLINK_API_KEY empty"
        return out
    try:
        wid = resolve_wallet_id()
        out["ok"] = True
        out["walletId"] = wid
    except Exception as e:
        out["ok"] = False
        out["error"] = str(e)
    return out
