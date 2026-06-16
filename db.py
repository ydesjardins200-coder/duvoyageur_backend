"""
db.py
=====
The cases database. One table: `cases`. Both intake paths (Messenger webhook and
the Netlify form) write rows here; the admin panel reads and updates them.

The full parsed TripRequest is stored as JSON in `trip`, with a few fields
promoted to real columns (status, channel, email, confidence) so the admin list
can sort/filter without unpacking JSON every time.

Works with Supabase Postgres, Railway Postgres, or local SQLite — controlled
entirely by DATABASE_URL.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import JSON, DateTime, Float, String, Text, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from config import settings

# Normalize the URL: some providers hand out "postgres://"; SQLAlchemy wants
# "postgresql://". This one-liner saves a classic deploy-day headache.
db_url = settings.DATABASE_URL
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)

connect_args = {"check_same_thread": False} if db_url.startswith("sqlite") else {}
engine = create_engine(db_url, connect_args=connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


# Status lifecycle for a case as it moves through the pipeline.
STATUSES = ("new", "needs_info", "quoted", "booked", "closed")


class Case(Base):
    __tablename__ = "cases"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    channel: Mapped[str] = mapped_column(String(20))            # 'messenger' | 'form'
    status: Mapped[str] = mapped_column(String(20), default="new")
    sender_ref: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    customer_email: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    parse_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    raw_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # The full TripRequest.model_dump() lives here.
    trip: Mapped[dict] = mapped_column(JSON, default=dict)
    # Promoted so the admin can see "what to ask the customer" at a glance.
    needs_clarification: Mapped[list] = mapped_column(JSON, default=list)


def init_db() -> None:
    Base.metadata.create_all(engine)


# Statuses that mean "this request is still in progress" — new messages from the
# same sender merge into it. Once quoted/booked/closed, the next message is a
# fresh trip request.
OPEN_STATUSES = ("new", "needs_info")


def find_open_case_for_sender(db, sender_ref: Optional[str], within_days: int = 30):
    """Most recent open Messenger case for this sender, within the merge window."""
    if not sender_ref:
        return None
    cutoff = datetime.utcnow() - timedelta(days=within_days)
    return (
        db.query(Case)
        .filter(
            Case.sender_ref == sender_ref,
            Case.channel == "messenger",
            Case.status.in_(OPEN_STATUSES),
            Case.created_at >= cutoff,
        )
        .order_by(Case.created_at.desc())
        .first()
    )
