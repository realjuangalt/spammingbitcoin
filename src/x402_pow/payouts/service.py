from __future__ import annotations

"""Site payout orchestration — accrue sats, pay LN addresses via Blink."""

import logging
import secrets

from sqlalchemy import select

from x402_pow.config import get_settings
from x402_pow.ledger.db import Payout, Publisher, session
from x402_pow.payouts.blink import BlinkError, blink_status, send_to_lightning_address

log = logging.getLogger("x402_pow.payouts")


def unpaid_sats(pub: Publisher) -> int:
    accrued = int(pub.tbtc_sats or 0)
    paid = int(getattr(pub, "paid_sats", 0) or 0)
    return max(0, accrued - paid)


def try_payout_publisher(publisher_id: str, *, force: bool = False) -> dict:
    """
    If Site has lightning_address and unpaid >= min (or force), send via Blink.
    """
    settings = get_settings()
    with session() as db:
        pub = db.get(Publisher, publisher_id)
        if pub is None:
            return {"ok": False, "error": "unknown publisher"}
        ln = (pub.lightning_address or "").strip()
        if not ln:
            return {"ok": False, "error": "no lightning_address"}
        amount = unpaid_sats(pub)
        if amount < 1:
            return {"ok": False, "error": "nothing accrued"}
        if not force and amount < settings.payout_min_sats:
            return {
                "ok": False,
                "error": "below_minimum",
                "unpaidSats": amount,
                "minSats": settings.payout_min_sats,
            }
        if not settings.blink_api_key.strip():
            return {
                "ok": False,
                "error": "blink_not_configured",
                "unpaidSats": amount,
                "hint": "Set BLINK_API_KEY (+ BLINK_WALLET_ID) in .env",
            }

        payout_id = "pay_" + secrets.token_hex(12)
        row = Payout(
            id=payout_id,
            publisher_id=pub.id,
            lightning_address=ln,
            amount_sats=amount,
            status="pending",
        )
        db.add(row)
        db.commit()

    try:
        result = send_to_lightning_address(ln_address=ln, amount_sats=amount)
        status = (result.get("status") or "SUCCESS").upper()
        ok = status in ("SUCCESS", "ALREADY_PAID", "PENDING")
        with session() as db:
            row = db.get(Payout, payout_id)
            pub = db.get(Publisher, publisher_id)
            if row:
                row.blink_status = status
                row.status = "success" if ok else "failed"
                if not ok:
                    row.error = str(result)
            if ok and pub is not None:
                pub.paid_sats = int(pub.paid_sats or 0) + amount
            db.commit()
        return {
            "ok": ok,
            "payoutId": payout_id,
            "amountSats": amount,
            "lightningAddress": ln,
            "blinkStatus": status,
        }
    except BlinkError as e:
        with session() as db:
            row = db.get(Payout, payout_id)
            if row:
                row.status = "failed"
                row.error = str(e)
                db.commit()
        log.warning("payout failed publisher=%s: %s", publisher_id, e)
        return {"ok": False, "payoutId": payout_id, "error": str(e), "details": e.details}
    except Exception as e:
        with session() as db:
            row = db.get(Payout, payout_id)
            if row:
                row.status = "failed"
                row.error = str(e)
                db.commit()
        log.exception("payout exception")
        return {"ok": False, "payoutId": payout_id, "error": str(e)}


def list_payouts(publisher_id: str, *, limit: int = 20) -> list[dict]:
    with session() as db:
        rows = db.scalars(
            select(Payout)
            .where(Payout.publisher_id == publisher_id)
            .order_by(Payout.created_at.desc())
            .limit(limit)
        ).all()
        return [
            {
                "id": r.id,
                "amountSats": r.amount_sats,
                "lightningAddress": r.lightning_address,
                "status": r.status,
                "blinkStatus": r.blink_status,
                "error": r.error,
                "createdAt": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]


def payout_health() -> dict:
    return blink_status()
