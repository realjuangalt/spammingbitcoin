from __future__ import annotations

"""Stratum V1 upstream client (Testnet4 reseller) + header builder."""

import hashlib
import json
import logging
import secrets
import socket
import struct
import threading
import time
from dataclasses import dataclass, field

from x402_pow.config import get_settings

log = logging.getLogger("x402_pow.stratum")


def sha256d(data: bytes) -> bytes:
    return hashlib.sha256(hashlib.sha256(data).digest()).digest()


def _hex_le32(hex_be: str) -> bytes:
    """4-byte big-endian hex → little-endian bytes for block header fields."""
    return bytes.fromhex(hex_be)[::-1]


def _swab32_words(data: bytes) -> bytes:
    """Byte-swap each uint32 word (ESP-Miner reverse_endianness_per_word)."""
    if len(data) != 32:
        raise ValueError("expected 32 bytes")
    out = bytearray(32)
    for i in range(0, 32, 4):
        out[i : i + 4] = data[i : i + 4][::-1]
    return bytes(out)


@dataclass
class UpstreamJob:
    job_id: str
    prevhash: str
    coinb1: str
    coinb2: str
    merkle_branches: list[str]
    version: str
    nbits: str
    ntime: str
    clean: bool
    extranonce1: str
    extranonce2_size: int
    difficulty: float
    received_at: float = field(default_factory=time.time)

    def build_prefix76(
        self,
        extranonce2_hex: str,
        *,
        extranonce1: str | None = None,
        ntime: str | None = None,
        version_bits: int = 0,
    ) -> bytes:
        en1 = self.extranonce1 if extranonce1 is None else extranonce1
        nt = self.ntime if ntime is None else ntime
        if len(extranonce2_hex) != self.extranonce2_size * 2:
            raise ValueError("extranonce2 length mismatch")
        coinbase = bytes.fromhex(self.coinb1 + en1 + extranonce2_hex + self.coinb2)
        merkle = sha256d(coinbase)
        for branch in self.merkle_branches:
            merkle = sha256d(merkle + bytes.fromhex(branch))
        # sha256d merkle root goes in the header as-is (Bitcoin internal byte order).
        # Stratum prevhash needs per-word bswap to match ASIC/ESP-Miner header layout.
        prev = _swab32_words(bytes.fromhex(self.prevhash))
        if len(prev) != 32 or len(merkle) != 32:
            raise ValueError("bad hash lengths")
        # Bitaxe / version-rolling: submit sends version_bits = rolled_version ^ job_version
        version = (int(self.version, 16) ^ int(version_bits)) & 0xFFFFFFFF
        prefix = (
            struct.pack("<I", version)
            + prev
            + merkle
            + _hex_le32(nt)
            + _hex_le32(self.nbits)
        )
        if len(prefix) != 76:
            raise ValueError(f"prefix length {len(prefix)}")
        return prefix


class UpstreamStratumClient:
    """Background Stratum V1 session to a public Testnet4 pool."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._job: UpstreamJob | None = None
        self._extranonce1 = ""
        self._extranonce2_size = 4
        self._difficulty = 1.0
        self._sock: socket.socket | None = None
        self._msg_id = 0
        self._authorized = False
        self._running = False
        self._thread: threading.Thread | None = None
        self._last_error: str | None = None
        self._connected_at: float | None = None
        self._job_listeners: list = []

    def on_job(self, callback) -> None:
        """Register callback(UpstreamJob) for each new upstream notify."""
        self._job_listeners.append(callback)

    @property
    def latest_job(self) -> UpstreamJob | None:
        with self._lock:
            return self._job

    @property
    def status(self) -> dict:
        settings = get_settings()
        up = settings.active_upstream()
        with self._lock:
            job = self._job
            return {
                "mode": "reseller",
                "network": up["network"],
                "connected": self._sock is not None and self._authorized,
                "authorized": self._authorized,
                "difficulty": self._difficulty,
                "jobId": job.job_id if job else None,
                "jobAgeSec": round(time.time() - job.received_at, 1) if job else None,
                "lastError": self._last_error,
                "connectedAt": self._connected_at,
                "upstream": f"{up['host']}:{up['port']}",
                "user": up["user"],
            }

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, name="stratum-upstream", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        self._close()

    def _next_id(self) -> int:
        self._msg_id += 1
        return self._msg_id

    def _close(self) -> None:
        with self._lock:
            if self._sock:
                try:
                    self._sock.close()
                except OSError:
                    pass
            self._sock = None
            self._authorized = False

    def _send(self, obj: dict) -> None:
        assert self._sock is not None
        self._sock.sendall((json.dumps(obj) + "\n").encode())

    def _run_loop(self) -> None:
        while self._running:
            try:
                self._session()
            except Exception as e:
                self._last_error = f"{type(e).__name__}: {e}"
                log.warning("upstream stratum session ended: %s", self._last_error)
            self._close()
            if self._running:
                time.sleep(3)

    def _session(self) -> None:
        settings = get_settings()
        up = settings.active_upstream()
        host = str(up["host"])
        port = int(up["port"])
        user = str(up["user"])
        password = str(up["password"])
        if not user:
            raise RuntimeError(
                "Upstream user not set — set UPSTREAM_STRATUM_USER or UPSTREAM_STRATUM_MAIN_USER"
            )

        log.info("connecting Upstream %s %s:%s as %s", up["network"], host, port, user)
        sock = socket.create_connection((host, port), timeout=20)
        sock.settimeout(60)
        with self._lock:
            self._sock = sock
            self._connected_at = time.time()
            self._last_error = None

        self._send(
            {
                "id": self._next_id(),
                "method": "mining.subscribe",
                "params": ["x402-pow/0.1"],
            }
        )
        self._send(
            {
                "id": self._next_id(),
                "method": "mining.authorize",
                "params": [user, password],
            }
        )

        buf = b""
        while self._running:
            chunk = sock.recv(65536)
            if not chunk:
                raise ConnectionError("upstream closed")
            buf += chunk
            while b"\n" in buf:
                line, _, buf = buf.partition(b"\n")
                if not line.strip():
                    continue
                msg = json.loads(line.decode())
                self._handle(msg)

    def _handle(self, msg: dict) -> None:
        method = msg.get("method")
        if method == "mining.notify":
            self._on_notify(msg["params"])
        elif method == "mining.set_difficulty":
            with self._lock:
                self._difficulty = float(msg["params"][0])
            log.info("upstream difficulty → %s", self._difficulty)
        elif method == "mining.set_extranonce":
            with self._lock:
                self._extranonce1 = (msg["params"][0] or "") if msg.get("params") else ""
                if len(msg["params"]) > 1:
                    self._extranonce2_size = int(msg["params"][1])
            log.info(
                "upstream set_extranonce en1=%r en2size=%s",
                self._extranonce1,
                self._extranonce2_size,
            )
        elif "result" in msg and msg.get("id") is not None:
            result = msg["result"]
            # subscribe result: [[...], extranonce1, extranonce2_size]
            # Braiins may return extranonce1 as "" (en2-only space).
            if (
                isinstance(result, list)
                and len(result) >= 3
                and isinstance(result[1], str)
                and isinstance(result[2], (int, float))
            ):
                with self._lock:
                    self._extranonce1 = result[1] or ""
                    self._extranonce2_size = int(result[2])
                log.info(
                    "subscribed enonce1=%r en2size=%s",
                    self._extranonce1,
                    self._extranonce2_size,
                )
            elif result is True:
                with self._lock:
                    self._authorized = True
                log.info("upstream authorized")
            elif result is False:
                self._last_error = "authorize rejected"
                log.error(
                    "upstream authorize rejected — check Upstream user/pass for %s",
                    get_settings().active_upstream()["network"],
                )

    def _on_notify(self, params: list) -> None:
        with self._lock:
            en1 = self._extranonce1
            en2sz = self._extranonce2_size
            diff = self._difficulty
        # Braiins uses empty extranonce1; only require a valid en2 size from subscribe.
        if en2sz <= 0:
            log.warning("upstream notify before subscribe completed — ignoring")
            return
        job = UpstreamJob(
            job_id=str(params[0]),
            prevhash=params[1],
            coinb1=params[2],
            coinb2=params[3],
            merkle_branches=list(params[4]),
            version=params[5],
            nbits=params[6],
            ntime=params[7],
            clean=bool(params[8]) if len(params) > 8 else True,
            extranonce1=en1 or "",
            extranonce2_size=en2sz,
            difficulty=diff,
        )
        with self._lock:
            self._job = job
        log.info("upstream job %s nbits=%s branches=%d", job.job_id, job.nbits, len(job.merkle_branches))
        for cb in list(self._job_listeners):
            try:
                cb(job)
            except Exception:
                log.exception("job listener failed")

    def mint_access_prefix(self) -> tuple[bytes, dict] | None:
        """Return (prefix76, upstream_meta) from the latest job, or None."""
        with self._lock:
            job = self._job
        if job is None:
            return None
        en2 = secrets.token_hex(job.extranonce2_size)
        prefix = job.build_prefix76(en2)
        meta = {
            "upstream_job_id": job.job_id,
            "extranonce2": en2,
            "ntime": job.ntime,
            "difficulty": job.difficulty,
            "nbits": job.nbits,
        }
        return prefix, meta

    def submit_share(
        self,
        *,
        upstream_job_id: str,
        extranonce2: str,
        ntime: str,
        nonce: int,
    ) -> bool:
        """Best-effort mining.submit to upstream. Returns True if accepted."""
        settings = get_settings()
        user = str(settings.active_upstream()["user"])
        nonce_hex = f"{nonce & 0xFFFFFFFF:08x}"
        # Stratum wants nonce as big-endian hex typically
        with self._lock:
            sock = self._sock
            if sock is None or not self._authorized:
                return False
            mid = self._next_id()
            try:
                self._send(
                    {
                        "id": mid,
                        "method": "mining.submit",
                        "params": [user, upstream_job_id, extranonce2, ntime, nonce_hex],
                    }
                )
            except OSError:
                return False
        # Non-blocking accept: we don't wait for response in hot path (logged in reader thread)
        return True


upstream_client = UpstreamStratumClient()


# Bitcoin difficulty-1 target (compact 0x1d00ffff), as uint256
DIFF1_TARGET = 0x00000000FFFF0000000000000000000000000000000000000000000000000000


def difficulty_to_target(diff: float) -> int:
    """Approximate share target from stratum difficulty (diff 1 ≈ Bitcoin diff-1 target)."""
    if diff <= 0:
        return DIFF1_TARGET
    return int(DIFF1_TARGET / diff)


def digest_to_difficulty(digest: bytes) -> float:
    """Actual share difficulty from sha256d digest (little-endian uint256)."""
    n = int.from_bytes(digest, "little")
    if n <= 0:
        return float("inf")
    return DIFF1_TARGET / n


def share_meets_pool_difficulty(digest: bytes, difficulty: float) -> bool:
    # sha256d digest is compared as a little-endian uint256 (Bitcoin consensus order).
    return int.from_bytes(digest, "little") < difficulty_to_target(difficulty)
