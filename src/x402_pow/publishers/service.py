from __future__ import annotations

import secrets

from sqlalchemy import select

from x402_pow.config import get_settings
from x402_pow.ledger.db import Publisher, session


def enroll_publisher(
    *,
    origin: str,
    display_name: str,
    access_zero_bits: int | None = None,
    payout_address: str | None = None,
    lightning_address: str | None = None,
    contact: str | None = None,
) -> Publisher:
    settings = get_settings()
    bits = access_zero_bits if access_zero_bits is not None else settings.access_zero_bits
    pub_id = "pub_" + secrets.token_hex(8)
    api_key = "sk_" + secrets.token_hex(24)
    portal_token = secrets.token_urlsafe(32)
    stratum_user = f"{pub_id}.default"
    ln = (lightning_address or "").strip() or None
    pub = Publisher(
        id=pub_id,
        display_name=display_name,
        origin=origin.rstrip("/"),
        api_key=api_key,
        portal_token=portal_token,
        stratum_user=stratum_user,
        access_zero_bits=bits,
        payout_address=payout_address,
        lightning_address=ln,
        contact=contact,
        paid_sats=0,
    )
    with session() as db:
        db.add(pub)
        db.commit()
        db.refresh(pub)
        return pub


def get_by_api_key(api_key: str) -> Publisher | None:
    with session() as db:
        return db.scalar(select(Publisher).where(Publisher.api_key == api_key))


def get_by_portal_token(token: str) -> Publisher | None:
    tok = (token or "").strip()
    if not tok:
        return None
    with session() as db:
        return db.scalar(select(Publisher).where(Publisher.portal_token == tok))


def get_by_id(publisher_id: str) -> Publisher | None:
    with session() as db:
        return db.get(Publisher, publisher_id)


def ensure_portal_token(publisher_id: str) -> str | None:
    """Backfill portal_token for Sites enrolled before magic links."""
    with session() as db:
        pub = db.get(Publisher, publisher_id)
        if pub is None:
            return None
        if pub.portal_token:
            return pub.portal_token
        pub.portal_token = secrets.token_urlsafe(32)
        db.commit()
        return pub.portal_token


def update_lightning_address(publisher_id: str, lightning_address: str | None) -> Publisher | None:
    with session() as db:
        pub = db.get(Publisher, publisher_id)
        if pub is None:
            return None
        pub.lightning_address = (lightning_address or "").strip() or None
        db.commit()
        db.refresh(pub)
        return pub


def credit_share(publisher_id: str, units: int = 1) -> None:
    with session() as db:
        pub = db.get(Publisher, publisher_id)
        if pub is None:
            return
        pub.share_units += units
        db.commit()


def portal_url(pub: Publisher) -> str:
    settings = get_settings()
    token = pub.portal_token or ""
    return f"{settings.signup_base}/s/{token}"
