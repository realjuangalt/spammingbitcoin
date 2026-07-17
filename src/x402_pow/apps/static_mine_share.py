from __future__ import annotations

"""CLI / fetch-and-run miner for x402 pool-share."""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request


def sha256d(data: bytes) -> bytes:
    import hashlib

    return hashlib.sha256(hashlib.sha256(data).digest()).digest()


def mine(prefix76: bytes, target: int, max_seconds: float = 30.0) -> tuple[int, bytes]:
    import struct

    start = time.time()
    nonce = 0
    while time.time() - start < max_seconds:
        for _ in range(100_000):
            header = prefix76 + struct.pack("<I", nonce & 0xFFFFFFFF)
            digest = sha256d(header)
            if int.from_bytes(digest, "big") < target:
                return nonce, digest
            nonce = (nonce + 1) & 0xFFFFFFFF
            if nonce == 0:
                break
        else:
            continue
        break
    raise TimeoutError(f"no share found in {max_seconds}s (tried up to nonce {nonce})")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="x402-pow SHA-256d share miner")
    p.add_argument("--api", required=True, help="Pool API base URL")
    p.add_argument("--job-id", required=True)
    p.add_argument("--prefix76", required=True, help="76-byte header prefix hex")
    p.add_argument("--target", required=True, help="access target as hex int")
    p.add_argument("--publisher", required=True)
    p.add_argument("--resource", required=True)
    p.add_argument("--receipt-out", default="-")
    p.add_argument("--max-seconds", type=float, default=30.0)
    p.add_argument("--benchmark", action="store_true")
    args = p.parse_args(argv)

    prefix = bytes.fromhex(args.prefix76)
    if len(prefix) != 76:
        print("prefix76 must be 76 bytes", file=sys.stderr)
        return 2
    target = int(args.target, 0)

    if args.benchmark:
        t0 = time.time()
        n = 0
        while time.time() - t0 < 2.0:
            sha256d(prefix + n.to_bytes(4, "little"))
            n += 1
        rate = n / (time.time() - t0)
        print(json.dumps({"hashes_per_sec": int(rate), "mh_s": round(rate / 1e6, 3)}))
        return 0

    print(f"mining job {args.job_id} target={hex(target)} …", file=sys.stderr)
    t0 = time.time()
    nonce, digest = mine(prefix, target, max_seconds=args.max_seconds)
    elapsed = time.time() - t0
    print(f"found nonce={nonce} in {elapsed:.2f}s hash={digest[::-1].hex()}", file=sys.stderr)

    payload = {
        "job_id": args.job_id,
        "nonce": nonce,
        "publisher_id": args.publisher,
        "resource": args.resource,
    }
    req = urllib.request.Request(
        args.api.rstrip("/") + "/v1/shares/submit",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        err = e.read().decode()
        print(err, file=sys.stderr)
        return 1

    out = json.dumps(body, indent=2)
    if args.receipt_out == "-":
        print(out)
    else:
        with open(args.receipt_out, "w", encoding="utf-8") as f:
            f.write(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
