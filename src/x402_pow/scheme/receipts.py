from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass

from itsdangerous import BadSignature, URLSafeTimedSerializer

from x402_pow.config import get_settings


@dataclass
class AccessReceipt:
    receipt_id: str
    publisher_id: str
    resource: str
    job_id: str
    nonce: int
    digest_hex: str
    zero_bits: int
    issued_at: float

    def to_token(self) -> str:
        s = URLSafeTimedSerializer(get_settings().receipt_secret, salt="x402-pow-receipt")
        return s.dumps(asdict(self))

    @classmethod
    def from_token(cls, token: str, max_age: int = 300) -> AccessReceipt:
        s = URLSafeTimedSerializer(get_settings().receipt_secret, salt="x402-pow-receipt")
        try:
            data = s.loads(token, max_age=max_age)
        except BadSignature as e:
            raise ValueError("invalid or expired receipt") from e
        return cls(**data)


# Spent receipt ids (process-local; also mirrored in SQLite)
_spent: set[str] = set()


def mark_spent(receipt_id: str) -> bool:
    """Return True if newly spent, False if already used."""
    if receipt_id in _spent:
        return False
    _spent.add(receipt_id)
    return True


def is_spent(receipt_id: str) -> bool:
    return receipt_id in _spent
