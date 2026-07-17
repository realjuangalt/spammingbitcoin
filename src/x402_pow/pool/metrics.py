from __future__ import annotations

"""Pool metrics — share/hashrate stats. Never stores IP addresses.

Snapshot shapes borrow parasite.space conventions (hashrate windows,
historical buckets, highest/top diffs, worker leaderboard) adapted to
this Pool's Agent/Site model.
"""

import threading
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, Integer, String, select, text
from sqlalchemy.orm import Mapped, mapped_column

from x402_pow.ledger.db import Base, get_engine, session


# Bitcoin difficulty-1 ≈ 2^32 hashes
DIFF1_HASHES = float(1 << 32)


class ShareEvent(Base):
    __tablename__ = "share_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    network: Mapped[str] = mapped_column(String(16), default="testnet", index=True)  # testnet | mainnet
    source: Mapped[str] = mapped_column(String(16))  # stratum | http
    worker: Mapped[str] = mapped_column(String(200), default="")
    site_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    difficulty: Mapped[float] = mapped_column(Float, default=1.0)  # assigned work units
    hash_difficulty: Mapped[float | None] = mapped_column(Float, nullable=True)  # actual from digest
    accepted: Mapped[bool] = mapped_column(Boolean, default=True)
    forwarded: Mapped[bool] = mapped_column(Boolean, default=False)
    # NEVER store client IP / peer address here.


@dataclass
class RecentShare:
    ts: float
    source: str
    worker: str
    site_id: str | None
    difficulty: float
    accepted: bool
    forwarded: bool
    hash_difficulty: float | None = None
    network: str = "testnet"


class Metrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._recent: list[RecentShare] = []
        self._max_recent = 5000
        self._ready = False
        self._started_at = time.time()

    def ensure_schema(self) -> None:
        if self._ready:
            return
        eng = get_engine()
        # Re-run create_all so share_events exists even if DB was opened earlier
        Base.metadata.create_all(eng)
        self._migrate_columns(eng)
        self._hydrate_recent()
        self._ready = True

    def _migrate_columns(self, eng) -> None:
        """Add new columns to existing SQLite tables without wiping data."""
        try:
            with eng.connect() as conn:
                cols = {
                    row[1]
                    for row in conn.execute(text("PRAGMA table_info(share_events)")).fetchall()
                }
                if "hash_difficulty" not in cols:
                    conn.execute(text("ALTER TABLE share_events ADD COLUMN hash_difficulty FLOAT"))
                if "network" not in cols:
                    # Prior shares were Testnet4 — backfill so mainnet starts clean.
                    conn.execute(
                        text(
                            "ALTER TABLE share_events ADD COLUMN network VARCHAR(16) "
                            "DEFAULT 'testnet'"
                        )
                    )
                    conn.execute(
                        text(
                            "UPDATE share_events SET network = 'testnet' "
                            "WHERE network IS NULL OR network = ''"
                        )
                    )
                conn.commit()
        except Exception:
            pass

    def _hydrate_recent(self) -> None:
        """Load recent DB rows into memory so hashrate survives API restarts."""
        try:
            with session() as db:
                rows = db.scalars(
                    select(ShareEvent).order_by(ShareEvent.id.desc()).limit(self._max_recent)
                ).all()
            loaded = [
                RecentShare(
                    ts=r.created_at.timestamp() if r.created_at else time.time(),
                    source=r.source,
                    worker=r.worker or "",
                    site_id=r.site_id,
                    difficulty=float(r.difficulty or 1.0),
                    accepted=bool(r.accepted),
                    forwarded=bool(r.forwarded),
                    hash_difficulty=(
                        float(r.hash_difficulty) if r.hash_difficulty is not None else None
                    ),
                    network=(r.network or "testnet"),
                )
                for r in reversed(rows)
            ]
            with self._lock:
                if not self._recent:
                    self._recent = loaded
        except Exception:
            pass

    def record_share(
        self,
        *,
        source: str,
        worker: str = "",
        site_id: str | None = None,
        difficulty: float = 1.0,
        accepted: bool = True,
        forwarded: bool = False,
        hash_difficulty: float | None = None,
        network: str | None = None,
    ) -> None:
        """Record a share. Do not pass IPs or peer addresses."""
        self.ensure_schema()
        from x402_pow.config import get_settings, normalize_network

        net = normalize_network(network or get_settings().active_network)
        now = time.time()
        worker = (worker or "")[:200]
        with self._lock:
            self._recent.append(
                RecentShare(
                    ts=now,
                    source=source,
                    worker=worker,
                    site_id=site_id,
                    difficulty=float(difficulty),
                    accepted=accepted,
                    forwarded=forwarded,
                    hash_difficulty=hash_difficulty,
                    network=net,
                )
            )
            if len(self._recent) > self._max_recent:
                self._recent = self._recent[-self._max_recent :]

        try:
            with session() as db:
                db.add(
                    ShareEvent(
                        created_at=datetime.fromtimestamp(now, tz=timezone.utc),
                        network=net,
                        source=source,
                        worker=worker,
                        site_id=site_id,
                        difficulty=float(difficulty),
                        hash_difficulty=hash_difficulty,
                        accepted=accepted,
                        forwarded=forwarded,
                    )
                )
                db.commit()
        except Exception:
            # Metrics must never break mining path
            pass

    def _hashrate_from_shares(self, shares: list[RecentShare], window_sec: float) -> float:
        if window_sec <= 0:
            return 0.0
        total_hashes = sum(s.difficulty * DIFF1_HASHES for s in shares if s.accepted)
        return total_hashes / window_sec

    def _history(
        self,
        recent: list[RecentShare],
        *,
        now: float,
        period_sec: float = 3600,
        bucket_sec: float = 60,
    ) -> list[dict]:
        """Parasite-style historical buckets: timestamp + hashrate + agent/site counts."""
        if bucket_sec <= 0:
            return []
        start = now - period_sec
        n_buckets = max(1, int(period_sec // bucket_sec))
        # Align bucket ends to wall clock for stable chart refresh
        end_aligned = int(now // bucket_sec) * bucket_sec
        buckets: list[dict] = []
        for i in range(n_buckets):
            b_end = end_aligned - (n_buckets - 1 - i) * bucket_sec
            b_start = b_end - bucket_sec
            if b_end < start:
                continue
            in_b = [s for s in recent if b_start <= s.ts < b_end]
            accepted = [s for s in in_b if s.accepted]
            workers = {s.worker for s in accepted if s.worker}
            sites = {s.site_id for s in accepted if s.site_id}
            hashes = sum(s.difficulty * DIFF1_HASHES for s in accepted)
            best = max(
                (s.hash_difficulty or s.difficulty for s in accepted),
                default=0.0,
            )
            buckets.append(
                {
                    "timestamp": int(b_end),
                    "agents": len(workers),
                    "sites": len(sites),
                    "hashrate": hashes / bucket_sec if accepted else 0.0,
                    "accepted": len(accepted),
                    "rejected": sum(1 for s in in_b if not s.accepted),
                    "forwarded": sum(1 for s in accepted if s.forwarded),
                    "highestDiff": best,
                }
            )
        return buckets

    def snapshot(self, *, live_miners: list[dict] | None = None, network: str | None = None) -> dict:
        self.ensure_schema()
        from x402_pow.config import get_settings, normalize_network

        net = normalize_network(network or get_settings().active_network)
        now = time.time()
        with self._lock:
            recent = [s for s in self._recent if (s.network or "testnet") == net]

        def in_window(sec: float) -> list[RecentShare]:
            cut = now - sec
            return [s for s in recent if s.ts >= cut]

        w60 = in_window(60)
        w300 = in_window(300)
        w900 = in_window(900)
        w3600 = in_window(3600)
        # Longer windows if we have data (parasite shows 1d/6d/9d avgs)
        w6h = in_window(6 * 3600)
        w1d = in_window(24 * 3600)

        by_worker: dict[str, dict] = defaultdict(
            lambda: {
                "accepted": 0,
                "rejected": 0,
                "hashes": 0.0,
                "bestDiff": 0.0,
                "forwarded": 0,
            }
        )
        by_site: dict[str, dict] = defaultdict(lambda: {"accepted": 0, "hashes": 0.0})
        for s in w3600:
            key = s.worker or "(anonymous)"
            if s.accepted:
                by_worker[key]["accepted"] += 1
                by_worker[key]["hashes"] += s.difficulty * DIFF1_HASHES
                if s.forwarded:
                    by_worker[key]["forwarded"] += 1
                hd = s.hash_difficulty if s.hash_difficulty is not None else s.difficulty
                if hd > by_worker[key]["bestDiff"]:
                    by_worker[key]["bestDiff"] = hd
                if s.site_id:
                    by_site[s.site_id]["accepted"] += 1
                    by_site[s.site_id]["hashes"] += s.difficulty * DIFF1_HASHES
            else:
                by_worker[key]["rejected"] += 1

        workers_hour = [
            {
                "worker": w,
                "accepted": v["accepted"],
                "rejected": v["rejected"],
                "forwarded": v["forwarded"],
                "hashrate1h": v["hashes"] / 3600.0 if v["hashes"] else 0.0,
                "bestDiff": v["bestDiff"],
            }
            for w, v in sorted(by_worker.items(), key=lambda x: -x[1]["hashes"])
        ]
        sites_hour = [
            {
                "siteId": sid,
                "accepted": v["accepted"],
                "hashrate1h": v["hashes"] / 3600.0 if v["hashes"] else 0.0,
            }
            for sid, v in sorted(by_site.items(), key=lambda x: -x[1]["hashes"])
        ]

        # Leaderboard — parasite-style rank by contribution + best diff
        leaderboard = [
            {
                "rank": i + 1,
                "worker": w["worker"],
                "hashrate1h": w["hashrate1h"],
                "accepted": w["accepted"],
                "rejected": w["rejected"],
                "bestDiff": w["bestDiff"],
                "forwarded": w["forwarded"],
            }
            for i, w in enumerate(workers_hour[:20])
        ]

        # Top difficulties — best actual (or assigned) diffs recently
        top_diffs: list[dict] = []
        for s in sorted(
            (x for x in recent if x.accepted),
            key=lambda x: -(x.hash_difficulty if x.hash_difficulty is not None else x.difficulty),
        )[:15]:
            top_diffs.append(
                {
                    "ts": s.ts,
                    "ageSec": round(now - s.ts, 1),
                    "worker": s.worker or "—",
                    "siteId": s.site_id,
                    "difficulty": s.hash_difficulty
                    if s.hash_difficulty is not None
                    else s.difficulty,
                    "assignedDiff": s.difficulty,
                    "forwarded": s.forwarded,
                    "source": s.source,
                }
            )

        accepted_all = [s for s in recent if s.accepted]
        highest_diff = max(
            (
                s.hash_difficulty if s.hash_difficulty is not None else s.difficulty
                for s in accepted_all
            ),
            default=0.0,
        )

        # Site ledger balances
        sites_ledger = []
        try:
            from x402_pow.ledger.db import Publisher

            with session() as db:
                rows = db.scalars(select(Publisher).order_by(Publisher.share_units.desc())).all()
                sites_ledger = [
                    {
                        "siteId": p.id,
                        "name": p.display_name,
                        "shareUnits": p.share_units,
                        "tbtcSats": p.tbtc_sats,
                        "origin": p.origin,
                    }
                    for p in rows
                ]
        except Exception:
            pass

        log = [
            {
                "ts": s.ts,
                "ageSec": round(now - s.ts, 1),
                "network": s.network,
                "source": s.source,
                "worker": s.worker or "—",
                "siteId": s.site_id,
                "difficulty": s.difficulty,
                "hashDifficulty": s.hash_difficulty,
                "accepted": s.accepted,
                "forwarded": s.forwarded,
            }
            for s in reversed(recent[-50:])
        ]

        history = self._history(recent, now=now, period_sec=3600, bucket_sec=60)
        acc_1h = sum(1 for s in w3600 if s.accepted)
        rej_1h = sum(1 for s in w3600 if not s.accepted)
        accept_rate = (acc_1h / (acc_1h + rej_1h)) if (acc_1h + rej_1h) else 0.0

        # How many shares exist on the other network (for dashboard clarity)
        with self._lock:
            other_net = "mainnet" if net == "testnet" else "testnet"
            other_count = sum(1 for s in self._recent if (s.network or "testnet") == other_net)

        live = live_miners or []
        return {
            "generatedAt": now,
            "privacy": "no_ip_logging",
            "network": net,
            "bitcoinNetwork": "bitcoin:mainnet" if net == "mainnet" else "bitcoin:testnet4",
            "assetLabel": "BTC sats" if net == "mainnet" else "tBTC sats",
            "otherNetwork": {
                "network": other_net,
                "recentShareCount": other_count,
            },
            # parasite-style pool-stats block
            "pool": {
                "uptimeSec": round(now - self._started_at, 1),
                "highestDifficulty": highest_diff,
                "agents": len({m.get("worker") for m in live if m.get("worker")}),
                "sites": len(sites_ledger),
                "acceptRate1h": accept_rate,
            },
            "hashrate": {
                "1m": self._hashrate_from_shares(w60, 60),
                "5m": self._hashrate_from_shares(w300, 300),
                "15m": self._hashrate_from_shares(w900, 900),
                "1h": self._hashrate_from_shares(w3600, 3600),
                "6h": self._hashrate_from_shares(w6h, 6 * 3600),
                "1d": self._hashrate_from_shares(w1d, 24 * 3600),
            },
            "shares": {
                "accepted1m": sum(1 for s in w60 if s.accepted),
                "rejected1m": sum(1 for s in w60 if not s.accepted),
                "accepted5m": sum(1 for s in w300 if s.accepted),
                "rejected5m": sum(1 for s in w300 if not s.accepted),
                "accepted1h": acc_1h,
                "rejected1h": rej_1h,
                "forwarded1h": sum(1 for s in w3600 if s.forwarded),
            },
            "history": history,
            "leaderboard": leaderboard,
            "topDifficulties": top_diffs,
            "liveMiners": live,
            "workers1h": workers_hour,
            "sites1h": sites_hour,
            "sitesLedger": sites_ledger,
            "recent": log,
        }


metrics = Metrics()


def format_hashrate(hps: float) -> str:
    if hps >= 1e15:
        return f"{hps / 1e15:.2f} PH/s"
    if hps >= 1e12:
        return f"{hps / 1e12:.2f} TH/s"
    if hps >= 1e9:
        return f"{hps / 1e9:.2f} GH/s"
    if hps >= 1e6:
        return f"{hps / 1e6:.2f} MH/s"
    if hps >= 1e3:
        return f"{hps / 1e3:.2f} kH/s"
    return f"{hps:.0f} H/s"


def format_difficulty(diff: float) -> str:
    """Compact difficulty like parasite (15.1T / 1.02k)."""
    if diff <= 0:
        return "—"
    if diff >= 1e12:
        return f"{diff / 1e12:.2f}T"
    if diff >= 1e9:
        return f"{diff / 1e9:.2f}G"
    if diff >= 1e6:
        return f"{diff / 1e6:.2f}M"
    if diff >= 1e3:
        return f"{diff / 1e3:.2f}k"
    if diff >= 10:
        return f"{diff:.1f}"
    return f"{diff:.3g}"


def format_uptime(sec: float) -> str:
    sec = max(0, int(sec))
    d, rem = divmod(sec, 86400)
    h, rem = divmod(rem, 3600)
    m, _ = divmod(rem, 60)
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"
