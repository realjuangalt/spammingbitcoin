from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, Integer, String, Text, case, create_engine, func, select, text
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from x402_pow.config import get_settings


class Base(DeclarativeBase):
    pass


class Publisher(Base):
    __tablename__ = "publishers"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(200))
    origin: Mapped[str] = mapped_column(String(500))
    api_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    # Unguessable portal secret — Site dashboard at /s/{portal_token}
    portal_token: Mapped[str | None] = mapped_column(String(64), unique=True, index=True, nullable=True)
    stratum_user: Mapped[str] = mapped_column(String(128))
    access_zero_bits: Mapped[int] = mapped_column(Integer, default=24)
    payout_address: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Lightning Address e.g. site@blink.sv — preferred payout rail
    lightning_address: Mapped[str | None] = mapped_column(String(200), nullable=True)
    contact: Mapped[str | None] = mapped_column(String(200), nullable=True)
    share_units: Mapped[int] = mapped_column(Integer, default=0)
    tbtc_sats: Mapped[int] = mapped_column(Integer, default=0)  # accrued (owed) sats
    paid_sats: Mapped[int] = mapped_column(Integer, default=0)  # successfully paid out
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SpentReceipt(Base):
    __tablename__ = "spent_receipts"

    receipt_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    publisher_id: Mapped[str] = mapped_column(String(64), index=True)
    resource: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Payout(Base):
    __tablename__ = "payouts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    publisher_id: Mapped[str] = mapped_column(String(64), index=True)
    lightning_address: Mapped[str] = mapped_column(String(200))
    amount_sats: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), default="pending")  # pending|success|failed
    blink_status: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class MemeVote(Base):
    """One PoW-weighted vote on a meme. One row per unlock receipt (single-use)."""

    __tablename__ = "meme_votes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    meme_id: Mapped[str] = mapped_column(String(200), index=True)
    board: Mapped[str] = mapped_column(String(16), index=True)  # human | agent
    direction: Mapped[int] = mapped_column(Integer)  # +1 upvote | -1 downvote
    achieved_bits: Mapped[int] = mapped_column(Integer, default=0)  # best-share leading zero bits
    work: Mapped[float] = mapped_column(Float, default=0.0)  # ~2^achieved_bits
    weight: Mapped[float] = mapped_column(Float, default=1.0)  # bounded vote weight
    receipt_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    publisher_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resource: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


_engine = None
_SessionLocal = None


def _migrate_sqlite(eng) -> None:
    """Add new columns to existing SQLite DBs (create_all won't alter)."""
    stmts = [
        "ALTER TABLE publishers ADD COLUMN portal_token VARCHAR(64)",
        "ALTER TABLE publishers ADD COLUMN lightning_address VARCHAR(200)",
        "ALTER TABLE publishers ADD COLUMN paid_sats INTEGER DEFAULT 0",
    ]
    with eng.begin() as conn:
        for sql in stmts:
            try:
                conn.execute(text(sql))
            except Exception:
                pass  # column already exists


def get_engine():
    global _engine, _SessionLocal
    if _engine is None:
        settings = get_settings()
        _engine = create_engine(settings.database_url, future=True)
        Base.metadata.create_all(_engine)
        if settings.database_url.startswith("sqlite"):
            _migrate_sqlite(_engine)
        _SessionLocal = sessionmaker(_engine, expire_on_commit=False, class_=Session)
    return _engine


def session() -> Session:
    get_engine()
    assert _SessionLocal is not None
    return _SessionLocal()


def record_vote(
    *,
    meme_id: str,
    board: str,
    direction: int,
    achieved_bits: int,
    work: float,
    weight: float,
    receipt_id: str,
    publisher_id: str | None = None,
    resource: str | None = None,
) -> bool:
    """Persist a weighted vote. Returns False if this receipt already voted."""
    with session() as db:
        existing = db.scalar(select(MemeVote).where(MemeVote.receipt_id == receipt_id))
        if existing is not None:
            return False
        db.add(
            MemeVote(
                meme_id=meme_id,
                board=board,
                direction=int(direction),
                achieved_bits=int(achieved_bits),
                work=float(work),
                weight=float(weight),
                receipt_id=receipt_id,
                publisher_id=publisher_id,
                resource=resource,
            )
        )
        db.commit()
    return True


def meme_scores(board: str | None = None, limit: int = 50) -> list[dict]:
    """Aggregated weighted leaderboard rows, highest net score first."""
    net = func.sum(MemeVote.weight * MemeVote.direction)
    up = func.sum(case((MemeVote.direction > 0, MemeVote.weight), else_=0.0))
    down = func.sum(case((MemeVote.direction < 0, MemeVote.weight), else_=0.0))
    votes = func.count()
    stmt = select(
        MemeVote.meme_id,
        net.label("score"),
        up.label("up_weight"),
        down.label("down_weight"),
        votes.label("votes"),
    ).group_by(MemeVote.meme_id)
    if board:
        stmt = stmt.where(MemeVote.board == board)
    stmt = stmt.order_by(net.desc()).limit(limit)
    with session() as db:
        rows = db.execute(stmt).all()
    return [
        {
            "meme_id": r.meme_id,
            "score": float(r.score or 0.0),
            "up_weight": float(r.up_weight or 0.0),
            "down_weight": float(r.down_weight or 0.0),
            "votes": int(r.votes or 0),
        }
        for r in rows
    ]


def meme_score_for(meme_id: str, board: str | None = None) -> dict:
    """Net weighted score + counts for a single meme."""
    net = func.sum(MemeVote.weight * MemeVote.direction)
    votes = func.count()
    stmt = select(net.label("score"), votes.label("votes")).where(MemeVote.meme_id == meme_id)
    if board:
        stmt = stmt.where(MemeVote.board == board)
    with session() as db:
        row = db.execute(stmt).one()
    return {"meme_id": meme_id, "score": float(row.score or 0.0), "votes": int(row.votes or 0)}
