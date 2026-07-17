from __future__ import annotations

import hashlib
import struct
from typing import Final

# Expected hashes ≈ 2^zero_bits
DEFAULT_ZERO_BITS: Final[int] = 24


def sha256d(data: bytes) -> bytes:
    return hashlib.sha256(hashlib.sha256(data).digest()).digest()


def target_from_zero_bits(zero_bits: int) -> int:
    """Hash interpreted as big-endian uint256 must be < target."""
    if zero_bits < 1 or zero_bits > 200:
        raise ValueError("zero_bits out of range")
    return 1 << (256 - zero_bits)


def hash_meets_target(digest: bytes, target: int) -> bool:
    return int.from_bytes(digest, "big") < target


def pack_header(prefix76: bytes, nonce: int) -> bytes:
    if len(prefix76) != 76:
        raise ValueError("prefix must be 76 bytes")
    return prefix76 + struct.pack("<I", nonce & 0xFFFFFFFF)


def mine_header(
    prefix76: bytes,
    target: int,
    *,
    start_nonce: int = 0,
    max_nonces: int | None = None,
) -> tuple[int, bytes] | None:
    """Return (nonce, digest) when found, else None."""
    limit = max_nonces if max_nonces is not None else (1 << 32) - start_nonce
    end = start_nonce + limit
    for nonce in range(start_nonce, min(end, 1 << 32)):
        digest = sha256d(pack_header(prefix76, nonce))
        if hash_meets_target(digest, target):
            return nonce, digest
    return None


def digest_hex_le(digest: bytes) -> str:
    """Bitcoin-style display: little-endian hex of the digest."""
    return digest[::-1].hex()
