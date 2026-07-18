from __future__ import annotations

"""CLI / fetch-and-run miner for x402 pool-share.

Tries GPU (hard job) first when --capability auto, then falls back to CPU (easy job).
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request


def sha256d(data: bytes) -> bytes:
    import hashlib

    return hashlib.sha256(hashlib.sha256(data).digest()).digest()


def mine_cpu(prefix76: bytes, target: int, max_seconds: float = 30.0) -> tuple[int, bytes, int]:
    import struct

    start = time.time()
    nonce = 0
    hashes = 0
    while time.time() - start < max_seconds:
        for _ in range(100_000):
            header = prefix76 + struct.pack("<I", nonce & 0xFFFFFFFF)
            digest = sha256d(header)
            hashes += 1
            if int.from_bytes(digest, "big") < target:
                return nonce, digest, hashes
            nonce = (nonce + 1) & 0xFFFFFFFF
            if nonce == 0:
                break
        else:
            continue
        break
    raise TimeoutError(f"no share found in {max_seconds}s (tried {hashes} hashes)")


def _worker_range(args: tuple) -> tuple[int | None, bytes | None, int]:
    prefix76, target, start_nonce, count = args
    import struct

    hashes = 0
    nonce = start_nonce
    for _ in range(count):
        header = prefix76 + struct.pack("<I", nonce & 0xFFFFFFFF)
        digest = sha256d(header)
        hashes += 1
        if int.from_bytes(digest, "big") < target:
            return nonce, digest, hashes
        nonce = (nonce + 1) & 0xFFFFFFFF
    return None, None, hashes


def mine_parallel(prefix76: bytes, target: int, max_seconds: float = 30.0) -> tuple[int, bytes, int]:
    """Multicore CPU grind for hard (GPU-tier) jobs when no OpenCL kernel is present."""
    import os
    from concurrent.futures import ProcessPoolExecutor, as_completed

    workers = max(1, min(8, os.cpu_count() or 2))
    chunk = 200_000
    start = time.time()
    nonce_base = 0
    total_hashes = 0
    with ProcessPoolExecutor(max_workers=workers) as pool:
        while time.time() - start < max_seconds:
            futs = []
            for i in range(workers):
                s = (nonce_base + i * chunk) & 0xFFFFFFFF
                futs.append(pool.submit(_worker_range, (prefix76, target, s, chunk)))
            nonce_base = (nonce_base + workers * chunk) & 0xFFFFFFFF
            for fut in as_completed(futs):
                nonce, digest, hashes = fut.result()
                total_hashes += hashes
                if nonce is not None and digest is not None:
                    return nonce, digest, total_hashes
    raise TimeoutError(f"no share found in {max_seconds}s (tried {total_hashes} hashes)")


def gpu_available() -> bool:
    """Local probe only — the website never sees this."""
    try:
        import pyopencl as cl  # type: ignore

        for platform in cl.get_platforms():
            if platform.get_devices(device_type=cl.device_type.GPU):
                return True
    except Exception:
        pass
    return False


def create_job(api: str, publisher: str, resource: str, capability: str) -> dict:
    payload = {
        "publisher_id": publisher,
        "resource": resource,
        "capability": capability,
    }
    req = urllib.request.Request(
        api.rstrip("/") + "/v1/jobs",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def submit_share(api: str, *, job_id: str, nonce: int, publisher: str, resource: str) -> dict:
    payload = {
        "job_id": job_id,
        "nonce": nonce,
        "publisher_id": publisher,
        "resource": resource,
    }
    req = urllib.request.Request(
        api.rstrip("/") + "/v1/shares/submit",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def resolve_capability(requested: str) -> str:
    req = (requested or "auto").strip().lower()
    if req == "cpu":
        return "cpu"
    # auto / gpu → hard tier first (multicore CPU today; OpenCL when present)
    return "gpu"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="x402-pow SHA-256d share miner (GPU-first, CPU fallback)")
    p.add_argument("--api", required=True, help="Pool API base URL")
    p.add_argument("--publisher", required=True)
    p.add_argument("--resource", required=True)
    p.add_argument(
        "--capability",
        default="auto",
        choices=("auto", "gpu", "cpu"),
        help="Hasher path. auto = probe GPU locally, else CPU. Default: auto",
    )
    p.add_argument("--job-id", default=None, help="Optional pre-baked job (skips create unless fallback)")
    p.add_argument("--prefix76", default=None)
    p.add_argument("--target", default=None, help="access target as hex int")
    p.add_argument("--receipt-out", default="-")
    p.add_argument("--max-seconds", type=float, default=30.0)
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
                    "mh_s": round(rate / 1e6, 3),
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
            " (hard) · OpenCL GPU detected"
            if capability == "gpu" and ocl
            else " (hard) · multicore CPU hasher"
            if capability == "gpu"
            else " (easy) · single-thread CPU"
        ),
        file=sys.stderr,
    )

    attempts: list[str] = [capability]
    if capability == "gpu":
        attempts.append("cpu")  # one automatic downgrade

    last_err: Exception | None = None
    for cap in attempts:
        try:
            if (
                args.job_id
                and args.prefix76
                and args.target
                and cap == capability
                and cap == "cpu"
                and args.capability == "cpu"
            ):
                # Legacy pre-baked easy job
                job = {
                    "jobId": args.job_id,
                    "prefix76": args.prefix76,
                    "accessTarget": args.target,
                    "zeroBits": None,
                    "capability": "cpu",
                }
            else:
                job = create_job(args.api, args.publisher, args.resource, cap)

            prefix = bytes.fromhex(job["prefix76"])
            if len(prefix) != 76:
                print("prefix76 must be 76 bytes", file=sys.stderr)
                return 2
            target = int(job["accessTarget"], 0)
            bits = job.get("zeroBits")
            print(
                f"mining job {job['jobId']} capability={job.get('capability', cap)} "
                f"zeroBits={bits} target={hex(target)} …",
                file=sys.stderr,
            )
            # GPU-tier: multicore CPU until a real OpenCL/CUDA kernel ships.
            miner = mine_parallel if cap == "gpu" else mine_cpu
            t0 = time.time()
            nonce, digest, hashes = miner(prefix, target, max_seconds=args.max_seconds)
            elapsed = time.time() - t0
            print(
                f"found nonce={nonce} in {elapsed:.2f}s hashes={hashes} "
                f"hash={digest[::-1].hex()} via={cap}",
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
            else:
                with open(args.receipt_out, "w", encoding="utf-8") as f:
                    f.write(out)
            return 0
        except TimeoutError as e:
            last_err = e
            print(f"{cap} attempt timed out: {e}", file=sys.stderr)
            continue
        except urllib.error.HTTPError as e:
            err = e.read().decode()
            print(err, file=sys.stderr)
            return 1
        except Exception as e:
            last_err = e
            print(f"{cap} attempt failed: {e}", file=sys.stderr)
            continue

    print(f"all attempts failed: {last_err}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
