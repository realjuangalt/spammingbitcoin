from __future__ import annotations

"""CPU (easy) vs GPU (hard) access difficulty negotiation."""

from typing import Literal

from x402_pow.config import get_settings

Capability = Literal["cpu", "gpu"]


def normalize_capability(value: str | None) -> Capability:
    v = (value or "cpu").strip().lower()
    return "gpu" if v == "gpu" else "cpu"


def resolve_access_zero_bits(cpu_bits: int, capability: str | None) -> int:
    """Map client capability to zero-bits. GPU is harder; pool trusts the proof, not the claim."""
    settings = get_settings()
    base = int(cpu_bits if cpu_bits is not None else settings.access_zero_bits)
    base = max(12, min(32, base))
    if normalize_capability(capability) == "gpu":
        return max(12, min(32, base + int(settings.access_gpu_bits_bonus)))
    return base
