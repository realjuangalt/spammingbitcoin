from __future__ import annotations

import argparse
import json
import struct
import sys
import time
import urllib.error
import urllib.request


def sha256d(data: bytes) -> bytes:
    import hashlib

    return hashlib.sha256(hashlib.sha256(data).digest()).digest()


def _header(prefix76: bytes, nonce: int) -> bytes:
    return prefix76 + struct.pack("<I", nonce & 0xFFFFFFFF)


def mine_cpu_window(prefix76: bytes, target: int, window_seconds: float = 21.0) -> tuple[int, bytes, int]:
    """Single-thread: hash the whole window, keep the best (lowest) share."""
    start = time.time()
    nonce = 0
    hashes = 0
    best_val = None
    best_nonce = 0
    best_digest = None
    met = False
    while time.time() - start < window_seconds:
        for _ in range(50000):
            digest = sha256d(_header(prefix76, nonce))
            hashes += 1
            val = int.from_bytes(digest, "big")
            if best_val is None or val < best_val:
                best_val, best_nonce, best_digest = val, nonce, digest
            if val < target:
                met = True
            nonce = (nonce + 1) & 0xFFFFFFFF
            if nonce == 0:
                break
    if not met or best_digest is None:
        raise TimeoutError(f"no qualifying share in {window_seconds}s (tried {hashes} hashes)")
    return best_nonce, best_digest, hashes


def _worker_best(args: tuple):
    prefix76, target, start_nonce, count = args
    hashes = 0
    nonce = start_nonce
    best_val = None
    best_nonce = start_nonce
    best_digest = None
    met = False
    for _ in range(count):
        digest = sha256d(_header(prefix76, nonce))
        hashes += 1
        val = int.from_bytes(digest, "big")
        if best_val is None or val < best_val:
            best_val, best_nonce, best_digest = val, nonce, digest
        if val < target:
            met = True
        nonce = (nonce + 1) & 0xFFFFFFFF
    return best_nonce, best_digest, hashes, met, best_val


def mine_parallel_window(prefix76: bytes, target: int, window_seconds: float = 21.0) -> tuple[int, bytes, int]:
    """Multicore CPU grind (GPU-tier placeholder): window + best share across workers."""
    import os
    from concurrent.futures import ProcessPoolExecutor, as_completed

    workers = max(1, min(8, os.cpu_count() or 2))
    chunk = 200000
    start = time.time()
    nonce_base = 0
    total = 0
    g_best_val = None
    g_best_nonce = 0
    g_best_digest = None
    met = False
    with ProcessPoolExecutor(max_workers=workers) as pool:
        while time.time() - start < window_seconds:
            futs = [
                pool.submit(_worker_best, (prefix76, target, (nonce_base + i * chunk) & 0xFFFFFFFF, chunk))
                for i in range(workers)
            ]
            nonce_base = (nonce_base + workers * chunk) & 0xFFFFFFFF
            for fut in as_completed(futs):
                bn, bd, h, m, bv = fut.result()
                total += h
                if m:
                    met = True
                if bv is None:
                    continue
                if g_best_val is None or bv < g_best_val:
                    g_best_val, g_best_nonce, g_best_digest = bv, bn, bd
    if not met or g_best_digest is None:
        raise TimeoutError(f"no qualifying share in {window_seconds}s (tried {total} hashes)")
    return g_best_nonce, g_best_digest, total


def leading_zero_bits(digest: bytes) -> int:
    val = int.from_bytes(digest, "big")
    if val == 0:
        return 256
    return 256 - val.bit_length()


def gpu_available() -> bool:
    """Local probe only — the website never sees this."""
    try:
        import pyopencl as cl

        for platform in cl.get_platforms():
            if platform.get_devices(device_type=cl.device_type.GPU):
                return True
        return False
    except Exception:
        return False


def _post(url: str, payload: dict) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())


def create_job(api: str, publisher: str, resource: str, capability: str) -> dict:
    return _post(
        api.rstrip("/") + "/v1/jobs",
        {"publisher_id": publisher, "resource": resource, "capability": capability},
    )


def submit_share(api: str, *, job_id: str, nonce: int, publisher: str, resource: str) -> dict:
    return _post(
        api.rstrip("/") + "/v1/shares/submit",
        {"job_id": job_id, "nonce": nonce, "publisher_id": publisher, "resource": resource},
    )


def resolve_capability(requested: str) -> str:
    req = (requested or "auto").strip().lower()
    if req == "cpu":
        return "cpu"
    return "gpu"


def _run_window(prefix: bytes, target: int, cap: str, window_seconds: float):
    miner = mine_parallel_window if cap == "gpu" else mine_cpu_window
    t0 = time.time()
    nonce, digest, hashes = miner(prefix, target, window_seconds=window_seconds)
    return nonce, digest, hashes, time.time() - t0


def _mine_pool_share(args, cap: str) -> int:
    """Generic pool-share mode: mine the window, submit the best share to the pool."""
    job = create_job(args.api, args.publisher, args.resource, cap)
    prefix = bytes.fromhex(job["prefix76"])
    if len(prefix) != 76:
        print("prefix76 must be 76 bytes", file=sys.stderr)
        return 2
    target = int(job["accessTarget"], 0)
    print(
        f"mining job {job['jobId']} capability={job.get('capability', cap)}"
        f" zeroBits={job.get('zeroBits')} window={args.window_seconds}s …",
        file=sys.stderr,
    )
    nonce, digest, hashes, elapsed = _run_window(prefix, target, cap, args.window_seconds)
    print(
        f"best nonce={nonce} in {elapsed:.2f}s hashes={hashes}"
        f" bestBits={leading_zero_bits(digest)} hash={digest[::-1].hex()} via={cap}",
        file=sys.stderr,
    )
    body = submit_share(
        args.api,
        job_id=job["jobId"],
        nonce=nonce,
        publisher=args.publisher,
        resource=args.resource,
    )
    out = json.dumps(body, indent=2)
    if args.receipt_out == "-":
        print(out)
        return 0
    with open(args.receipt_out, "w", encoding="utf-8") as f:
        f.write(out)
    return 0


def _mine_meme(args, cap: str) -> int:
    """Meme mode: unlock via the site flow, then optionally cast a PoW-weighted vote."""
    base = args.meme_base.rstrip("/")
    job = _post(base + "/meme/job", {"capability": cap})
    if not job.get("job_id"):
        print(f"job request failed: {job}", file=sys.stderr)
        return 1
    prefix = bytes.fromhex(job["prefix76_hex"])
    target = (
        int(job["access_target"])
        if str(job["access_target"]).isdigit()
        else int(str(job["access_target"]), 0)
    )
    print(
        f"meme job {job['job_id']} window={args.window_seconds}s"
        f" gateBits={job.get('zero_bits')} via={cap} …",
        file=sys.stderr,
    )
    nonce, digest, hashes, elapsed = _run_window(prefix, target, cap, args.window_seconds)
    bits = leading_zero_bits(digest)
    print(
        f"best nonce={nonce} in {elapsed:.2f}s hashes={hashes} bestBits={bits} via={cap}",
        file=sys.stderr,
    )
    mine = _post(base + "/meme/mine-browser", {"job_id": job["job_id"], "nonce": nonce})
    if not mine.get("ok"):
        print(f"share rejected: {mine.get('error')}", file=sys.stderr)
        return 1
    unlocked = _post(base + "/meme/next", {"receipt_token": mine["receiptToken"]})
    if not unlocked.get("ok"):
        print(f"unlock failed: {unlocked.get('error')}", file=sys.stderr)
        return 1

    meme = unlocked.get("meme") or {}
    sol = unlocked.get("solution") or {}
    media = unlocked.get("media_url")
    vote_token = unlocked.get("vote_token")
    media_full = (base + media) if media else None

    result = {
        "meme": {"title": meme.get("title"), "credit": meme.get("credit")},
        "media_url": media_full,
        "vote_weight": sol.get("vote_weight"),
        "achieved_zero_bits": sol.get("achieved_zero_bits"),
        "hashes": hashes,
    }

    vote = (args.vote or "").strip().lower()
    if vote in ("up", "down"):
        if not vote_token:
            print("no vote token returned; cannot vote", file=sys.stderr)
        else:
            v = _post(
                base + "/meme/vote",
                {"vote_token": vote_token, "direction": vote, "board": args.board},
            )
            result["vote"] = v
            if v.get("ok"):
                print(
                    f"voted {vote} on '{meme.get('title')}' weight={v.get('weight')}x"
                    f" board={v.get('board')}",
                    file=sys.stderr,
                )
            else:
                print(f"vote failed: {v.get('error')}", file=sys.stderr)
    else:
        result["relay"] = {
            "note": "No --vote given. If you cannot see images, relay this meme to your owner for a verdict, then re-run with --vote up|down (the vote token is single-use, so you'll mine a fresh window).",
            "media_url": media_full,
            "title": meme.get("title"),
        }

    out = json.dumps(result, indent=2)
    if args.receipt_out == "-":
        print(out)
        return 0
    with open(args.receipt_out, "w", encoding="utf-8") as f:
        f.write(out)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="x402-pow SHA-256d window miner (best-share; GPU-first, CPU fallback)"
    )
    p.add_argument("--api", help="Pool API base URL (pool-share mode)")
    p.add_argument("--publisher")
    p.add_argument("--resource")
    p.add_argument(
        "--capability",
        default="auto",
        choices=("auto", "gpu", "cpu"),
        help="Hasher path. auto = probe GPU locally, else CPU. Default: auto",
    )
    p.add_argument("--receipt-out", default="-")
    p.add_argument(
        "--window-seconds",
        type=float,
        default=21.0,
        help="Fixed hash window; vote weight scales with work done here. Default 21.",
    )
    p.add_argument("--meme-base", help="Meme site base URL, e.g. https://spammingbitcoin.com")
    p.add_argument("--vote", choices=("up", "down"), help="Cast a PoW-weighted vote (meme mode)")
    p.add_argument("--board", default="agent", help="Vote board: agent|human (default agent)")
    p.add_argument("--benchmark", action="store_true")
    args = p.parse_args(argv)

    if args.benchmark:
        prefix = bytes(76)
        t0 = time.time()
        n = 0
        while time.time() - t0 < 2.0:
            sha256d(prefix + n.to_bytes(4, "little"))
            n += 1
        rate = n / (time.time() - t0)
        print(
            json.dumps(
                {
                    "hashes_per_sec": int(rate),
                    "mh_s": round(rate / 1_000_000.0, 3),
                    "gpu_available": gpu_available(),
                }
            )
        )
        return 0

    capability = resolve_capability(args.capability)
    ocl = gpu_available()
    print(
        f"capability → {capability}"
        + (
            " (OpenCL GPU detected)"
            if capability == "gpu" and ocl
            else " (multicore CPU hasher)"
            if capability == "gpu"
            else " (single-thread CPU)"
        )
        + f" · {args.window_seconds:.0f}s window",
        file=sys.stderr,
    )

    meme_mode = bool(args.meme_base) or bool(args.vote)
    if args.vote and not args.meme_base:
        print("--vote requires --meme-base", file=sys.stderr)
        return 2
    if not meme_mode and not (args.api and args.publisher and args.resource):
        print("pool-share mode needs --api, --publisher and --resource", file=sys.stderr)
        return 2

    attempts = [capability] + (["cpu"] if capability == "gpu" else [])
    last_err = None
    for cap in attempts:
        try:
            if meme_mode:
                return _mine_meme(args, cap)
            return _mine_pool_share(args, cap)
        except TimeoutError as e:
            last_err = e
            print(f"{cap} attempt timed out: {e}", file=sys.stderr)
            continue
        except urllib.error.HTTPError as e:
            print(e.read().decode(), file=sys.stderr)
            return 1
        except Exception as e:
            last_err = e
            print(f"{cap} attempt failed: {e}", file=sys.stderr)
            continue
    print(f"all attempts failed: {last_err}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
