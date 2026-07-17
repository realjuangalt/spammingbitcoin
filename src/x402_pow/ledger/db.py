from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, create_engine, func, text
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
