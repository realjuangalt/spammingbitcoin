#!/usr/bin/env python3
"""Generate (or print) the local Testnet4 P2WPKH pool payout wallet."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sys
from pathlib import Path

from bech32 import bech32_encode, convertbits
from coincurve import PrivateKey

ROOT = Path(__file__).resolve().parents[1]
WALLET = ROOT / "data" / "wallet"
ALPHABET = b"123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58encode(b: bytes) -> str:
    n = int.from_bytes(b, "big")
    res = bytearray()
    while n > 0:
        n, r = divmod(n, 58)
        res.append(ALPHABET[r])
    pad = sum(1 for byte in b if byte == 0)
    # only leading zero bytes
    pad = 0
    for byte in b:
        if byte == 0:
            pad += 1
        else:
            break
    return (ALPHABET[:1] * pad + res[::-1]).decode()


def main() -> int:
    WALLET.mkdir(parents=True, exist_ok=True)
    os.chmod(WALLET, 0o700)
    addr_path = WALLET / "address.txt"
    wif_path = WALLET / "private.wif"
    meta_path = WALLET / "wallet.json"

    if addr_path.exists() and wif_path.exists() and "--force" not in sys.argv:
        meta = json.loads(meta_path.read_text())
        print(json.dumps(meta, indent=2))
        print("\n(re-run with --force to rotate; update UPSTREAM_STRATUM_USER + restart API)")
        return 0

    sk = PrivateKey(secrets.token_bytes(32))
    pub = sk.public_key.format(compressed=True)
    h160 = hashlib.new("ripemd160", hashlib.sha256(pub).digest()).digest()
    data = convertbits(h160, 8, 5)
    assert data is not None
    addr = bech32_encode("tb", [0] + data)
    payload = b"\xef" + sk.secret + b"\x01"
    checksum = hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
    wif = b58encode(payload + checksum)

    wif_path.write_text(wif + "\n")
    os.chmod(wif_path, 0o600)
    addr_path.write_text(addr + "\n")
    meta = {
        "network": "bitcoin:testnet4",
        "type": "p2wpkh",
        "address": addr,
        "pubkey_compressed_hex": pub.hex(),
    }
    meta_path.write_text(json.dumps(meta, indent=2))
    os.chmod(meta_path, 0o600)
    print(json.dumps(meta, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
