from __future__ import annotations

import secrets
import time

from sqlalchemy import select

from x402_pow.ledger.db import SpentReceipt, session
from x402_pow.pow.hashing import digest_hex_le, hash_meets_target, pack_header, sha256d
from x402_pow.pow.jobs import job_manager
from x402_pow.publishers import service as publishers
from x402_pow.scheme.receipts import AccessReceipt, is_spent, mark_spent


class SettleError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def submit_share(
    *,
    job_id: str,
    nonce: int,
    publisher_id: str,
    resource: str,
) -> AccessReceipt:
    job = job_manager.get(job_id)
    if job is None:
        raise SettleError("unknown or expired job", 400)
    if job.publisher_id != publisher_id:
        raise SettleError("publisher mismatch", 400)
    if job.resource != resource:
        raise SettleError("resource mismatch", 400)

    header = pack_header(job.prefix76, nonce)
    digest = sha256d(header)
    if not hash_meets_target(digest, job.access_target):
        raise SettleError("share does not meet access difficulty", 402)

    forwarded = False
    # Opportunistically forward to Upstream pool when work is on a real job
    if job.source == "reseller" and job.upstream_meta:
        try:
            from x402_pow.stratum.upstream import share_meets_pool_difficulty, upstream_client

            meta = job.upstream_meta
            diff = float(meta.get("difficulty") or 1)
            if share_meets_pool_difficulty(digest, diff):
                upstream_client.submit_share(
                    upstream_job_id=str(meta["upstream_job_id"]),
                    extranonce2=str(meta["extranonce2"]),
                    ntime=str(meta["ntime"]),
                    nonce=nonce,
                )
                forwarded = True
        except Exception:
            pass

    receipt = AccessReceipt(
        receipt_id="rcpt_" + secrets.token_hex(12),
        publisher_id=publisher_id,
        resource=resource,
        job_id=job_id,
        nonce=nonce,
        digest_hex=digest_hex_le(digest),
        zero_bits=job.zero_bits,
        issued_at=time.time(),
    )
    publishers.credit_share(publisher_id, 1)

    try:
        from x402_pow.pool.metrics import metrics
        from x402_pow.stratum.upstream import digest_to_difficulty

        # access zero-bits → approx stratum difficulty: 2^bits hashes ≈ diff * 2^32
        approx_diff = (2 ** job.zero_bits) / (1 << 32)
        metrics.record_share(
            source="http",
            worker="access-agent",
            site_id=publisher_id,
            difficulty=approx_diff,
            accepted=True,
            forwarded=forwarded,
            hash_difficulty=digest_to_difficulty(digest),
        )
    except Exception:
        pass

    return receipt


def settle_receipt(token: str, *, expected_publisher: str | None = None, expected_resource: str | None = None) -> AccessReceipt:
    try:
        receipt = AccessReceipt.from_token(token)
    except ValueError as e:
        raise SettleError(str(e), 400) from e

    if expected_publisher and receipt.publisher_id != expected_publisher:
        raise SettleError("publisher mismatch", 400)
    if expected_resource and receipt.resource != expected_resource:
        raise SettleError("resource mismatch", 400)

    if is_spent(receipt.receipt_id):
        raise SettleError("receipt already used", 402)

    with session() as db:
        existing = db.get(SpentReceipt, receipt.receipt_id)
        if existing is not None:
            raise SettleError("receipt already used", 402)
        db.add(
            SpentReceipt(
                receipt_id=receipt.receipt_id,
                publisher_id=receipt.publisher_id,
                resource=receipt.resource,
            )
        )
        db.commit()

    mark_spent(receipt.receipt_id)
    return receipt
