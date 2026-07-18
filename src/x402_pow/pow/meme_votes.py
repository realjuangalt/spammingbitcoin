from __future__ import annotations

"""Proof-of-work meritocracy: turn a voter's best-share work into a bounded vote weight.

Fixed-window model: a voter hashes for ~meme_window_seconds and submits their best
(lowest) share. The best of N tries statistically implies ~N hashes, so achieved
leading-zero bits ≈ log2(work). Weight is deliberately compressed so a phone still
counts while an ASIC farm gets a real-but-bounded edge:

    weight = clamp(1 + (achieved_bits - base_bits), 1, cap)

which is exactly clamp(1 + log2(work / 2**base_bits), 1, cap).
"""

from typing import Literal

from x402_pow.config import get_settings

Board = Literal["human", "agent"]


def normalize_board(value: str | None) -> Board:
    return "agent" if (value or "").strip().lower() == "agent" else "human"


def normalize_direction(value) -> int:
    """Map up/down/1/-1/'up'/'down' to +1 or -1."""
    if isinstance(value, (int, float)):
        return 1 if value >= 0 else -1
    v = (str(value or "")).strip().lower()
    return -1 if v in ("down", "-1", "dn", "downvote") else 1


def work_from_bits(achieved_bits: int) -> float:
    return float(2 ** max(0, int(achieved_bits)))


def vote_weight_from_bits(achieved_bits: int) -> float:
    settings = get_settings()
    base = int(settings.vote_weight_base_bits)
    cap = float(settings.vote_weight_cap)
    raw = 1.0 + (float(achieved_bits) - float(base))
    return max(1.0, min(cap, raw))


def meme_fund_allocation(total_sats: int, scores: list[dict]) -> list[dict]:
    """Stub: split the meme fund across memes by positive weighted score share.

    Real payouts are manual/future; this only computes the notional split so the
    leaderboard can show each meme's share of a hypothetical fund.
    """
    settings = get_settings()
    fund = int(round(total_sats * (settings.meme_fund_percent / 100.0)))
    positives = [(s["meme_id"], max(0.0, float(s.get("score") or 0.0))) for s in scores]
    denom = sum(w for _, w in positives)
    out: list[dict] = []
    for meme_id, w in positives:
        share = (w / denom) if denom > 0 else 0.0
        out.append({"meme_id": meme_id, "share": share, "sats": int(round(fund * share))})
    return out
