from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field

from x402_pow.config import get_settings
from x402_pow.pow.hashing import target_from_zero_bits


@dataclass
class MiningJob:
    job_id: str
    prefix76_hex: str
    access_target: int
    zero_bits: int
    publisher_id: str
    resource: str
    created_at: float
    expires_at: float
    source: str = "local"  # local | reseller
    upstream_meta: dict = field(default_factory=dict)

    @property
    def prefix76(self) -> bytes:
        return bytes.fromhex(self.prefix76_hex)


class JobManager:
    """Jobs for access-gate PoW. Prefer Testnet4 upstream templates when reseller mode."""

    def __init__(self) -> None:
        self._jobs: dict[str, MiningJob] = {}

    def create_job(
        self,
        *,
        publisher_id: str,
        resource: str,
        zero_bits: int | None = None,
        ttl_seconds: int = 120,
    ) -> MiningJob:
        settings = get_settings()
        bits = zero_bits if zero_bits is not None else settings.access_zero_bits
        source = "local"
        upstream_meta: dict = {}
        prefix: bytes | None = None

        if settings.upstream_mode == "reseller":
            try:
                from x402_pow.stratum.upstream import upstream_client

                minted = upstream_client.mint_access_prefix()
                if minted is not None:
                    prefix, upstream_meta = minted
                    source = "reseller"
            except Exception:
                prefix = None

        if prefix is None:
            # Synthetic header when upstream is down / local mode
            version = (1).to_bytes(4, "little")
            prev = secrets.token_bytes(32)
            merkle = secrets.token_bytes(32)
            ntime = int(time.time()).to_bytes(4, "little")
            nbits = (0x1D00FFFF).to_bytes(4, "little")
            prefix = version + prev + merkle + ntime + nbits
            source = "local"
            upstream_meta = {}

        assert len(prefix) == 76
        now = time.time()
        job = MiningJob(
            job_id=secrets.token_hex(8),
            prefix76_hex=prefix.hex(),
            access_target=target_from_zero_bits(bits),
            zero_bits=bits,
            publisher_id=publisher_id,
            resource=resource,
            created_at=now,
            expires_at=now + ttl_seconds,
            source=source,
            upstream_meta=upstream_meta,
        )
        self._jobs[job.job_id] = job
        return job

    def get(self, job_id: str) -> MiningJob | None:
        job = self._jobs.get(job_id)
        if job is None:
            return None
        if time.time() > job.expires_at:
            self._jobs.pop(job_id, None)
            return None
        return job

    def purge_expired(self) -> None:
        now = time.time()
        dead = [k for k, j in self._jobs.items() if now > j.expires_at]
        for k in dead:
            self._jobs.pop(k, None)


job_manager = JobManager()
