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

from sqlalchemy import (JSON, DateTime, Float, ForeignKey, Integer, String, Text,
                        UniqueConstraint, create_engine, text)
from sqlalchemy.orm import (DeclarativeBase, Mapped, mapped_column, relationship,
                            sessionmaker)

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

# Kinds of identifiers we can use to recognize a returning client across channels.
IDENTITY_KINDS = ("messenger_psid", "email", "phone")


class Client(Base):
    """The durable record of a person. One client → many requests (demandes).

    A client is recognized across channels via rows in `client_identities`
    (a Messenger PSID, one or more emails, a phone). The primary_* columns are
    a denormalized convenience for display/sorting.
    """
    __tablename__ = "clients"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    display_name: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    primary_email: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    primary_phone: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    preferred_channel: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    tags: Mapped[list] = mapped_column(JSON, default=list)
    last_contact_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    identities = relationship(
        "ClientIdentity", back_populates="client",
        cascade="all, delete-orphan")
    requests = relationship(
        "Case", back_populates="client", order_by="Case.created_at.desc()")


class ClientIdentity(Base):
    """One way to recognize a client. (kind, value) is globally unique, so the
    same email/PSID can never point at two clients — the basis of dedup."""
    __tablename__ = "client_identities"
    __table_args__ = (UniqueConstraint("kind", "value", name="uq_identity_kind_value"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(20))   # one of IDENTITY_KINDS
    value: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    client = relationship("Client", back_populates="identities")


class Case(Base):
    __tablename__ = "cases"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    # Owning client (nullable during/after migration; set on every new request).
    client_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("clients.id"), nullable=True, index=True)
    client = relationship("Client", back_populates="requests")

    channel: Mapped[str] = mapped_column(String(20))            # 'messenger' | 'form'
    status: Mapped[str] = mapped_column(String(20), default="new")
    sender_ref: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    customer_email: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    customer_phone: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    parse_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    raw_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # The full TripRequest.model_dump() lives here.
    trip: Mapped[dict] = mapped_column(JSON, default=dict)
    # Promoted so the admin can see "what to ask the customer" at a glance.
    needs_clarification: Mapped[list] = mapped_column(JSON, default=list)
    # Screenshots the customer sent: list of {media_type, b64, received_at}.
    screenshots: Mapped[list] = mapped_column(JSON, default=list)


def init_db() -> None:
    Base.metadata.create_all(engine)
    _ensure_columns()
    _backfill_clients()


def _ensure_columns() -> None:
    """Add columns introduced after first deploy, without a manual migration."""
    with engine.begin() as conn:
        if engine.dialect.name == "postgresql":
            conn.execute(text(
                "ALTER TABLE cases ADD COLUMN IF NOT EXISTS screenshots JSONB DEFAULT '[]'::jsonb"
            ))
            conn.execute(text(
                "ALTER TABLE cases ADD COLUMN IF NOT EXISTS customer_phone VARCHAR(40)"
            ))
            conn.execute(text(
                "ALTER TABLE cases ADD COLUMN IF NOT EXISTS client_id INTEGER"
            ))
        elif engine.dialect.name == "sqlite":
            cols = [r[1] for r in conn.execute(text("PRAGMA table_info(cases)"))]
            if "screenshots" not in cols:
                conn.execute(text("ALTER TABLE cases ADD COLUMN screenshots JSON"))
            if "customer_phone" not in cols:
                conn.execute(text("ALTER TABLE cases ADD COLUMN customer_phone VARCHAR(40)"))
            if "client_id" not in cols:
                conn.execute(text("ALTER TABLE cases ADD COLUMN client_id INTEGER"))


# Statuses that mean "this request is still in progress" — new messages from the
# same sender merge into it. Once quoted/booked/closed, the next message is a
# fresh trip request.
OPEN_STATUSES = ("new", "needs_info")


def find_open_case_for_sender(db, sender_ref: Optional[str], within_days: int = 30):
    """Most recent open Messenger case for this sender, within the merge window.

    Kept for reference; the Messenger flow now resolves the client first and
    uses find_open_request_for_client (client-scoped, any channel)."""
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


def find_open_request_for_client(db, client_id: Optional[int], within_days: int = 30):
    """Most recent still-open request for THIS client, any channel, within the
    merge window. New messages from a known client fold into it; once the
    request is quoted/booked/closed, the next message starts a fresh one."""
    if not client_id:
        return None
    cutoff = datetime.utcnow() - timedelta(days=within_days)
    return (
        db.query(Case)
        .filter(
            Case.client_id == client_id,
            Case.status.in_(OPEN_STATUSES),
            Case.created_at >= cutoff,
        )
        .order_by(Case.created_at.desc())
        .first()
    )


# --------------------------------------------------------------------------- #
# Client identity resolution (recognize returning clients across channels)
# --------------------------------------------------------------------------- #
import re as _re


def normalize_email(email: Optional[str]) -> Optional[str]:
    if not email:
        return None
    e = email.strip().lower()
    return e or None


def normalize_phone(phone: Optional[str]) -> Optional[str]:
    """Loose E.164-ish normalization: keep digits, keep a leading '+'.
    Good enough to dedupe most North-American numbers entered by hand."""
    if not phone:
        return None
    p = phone.strip()
    plus = p.startswith("+")
    digits = _re.sub(r"\D", "", p)
    if not digits:
        return None
    # bare 10-digit NANP number -> assume +1
    if not plus and len(digits) == 10:
        digits = "1" + digits
    return "+" + digits


def find_client_by_identity(db, kind: str, value: Optional[str]) -> Optional["Client"]:
    if not value:
        return None
    row = db.query(ClientIdentity).filter_by(kind=kind, value=value).first()
    return row.client if row else None


def add_identity(db, client: "Client", kind: str, value: Optional[str]):
    """Attach an identity to a client, idempotently. If the (kind, value) is
    already taken (even by another client) we DO NOT steal it — that case is a
    potential cross-channel duplicate to be merged manually later."""
    if not value:
        return None
    existing = db.query(ClientIdentity).filter_by(kind=kind, value=value).first()
    if existing:
        return existing
    ident = ClientIdentity(client_id=client.id, kind=kind, value=value)
    db.add(ident)
    db.flush()
    return ident


def resolve_or_create_client(db, *, messenger_psid: Optional[str] = None,
                             email: Optional[str] = None, phone: Optional[str] = None,
                             name: Optional[str] = None,
                             channel: Optional[str] = None) -> "Client":
    """Find the client behind these identifiers, or create one. Matching order:
    Messenger PSID → email → phone. Never auto-merges two existing clients."""
    email = normalize_email(email)
    phone = normalize_phone(phone)

    client = (find_client_by_identity(db, "messenger_psid", messenger_psid)
              or find_client_by_identity(db, "email", email)
              or find_client_by_identity(db, "phone", phone))

    if client is None:
        client = Client(display_name=name, primary_email=email,
                        primary_phone=phone, preferred_channel=channel)
        db.add(client)
        db.flush()  # assign id before attaching identities
    else:
        # enrich the golden record without overwriting existing values
        if name and not client.display_name:
            client.display_name = name
        if email and not client.primary_email:
            client.primary_email = email
        if phone and not client.primary_phone:
            client.primary_phone = phone
        if channel and not client.preferred_channel:
            client.preferred_channel = channel

    add_identity(db, client, "messenger_psid", messenger_psid)
    add_identity(db, client, "email", email)
    add_identity(db, client, "phone", phone)

    client.last_contact_at = datetime.utcnow()
    db.flush()
    return client


def _backfill_clients() -> None:
    """Attach a Client to any legacy Case that has none. Idempotent: only
    touches rows where client_id IS NULL, so it's safe to run on every boot."""
    with SessionLocal() as db:
        orphans = (
            db.query(Case)
            .filter(Case.client_id.is_(None))
            .order_by(Case.created_at.asc())
            .all()
        )
        if not orphans:
            return
        for c in orphans:
            trip = c.trip or {}
            client = resolve_or_create_client(
                db,
                messenger_psid=c.sender_ref if c.channel == "messenger" else None,
                email=c.customer_email,
                phone=c.customer_phone,
                name=trip.get("customer_name"),
                channel=c.channel,
            )
            c.client_id = client.id
        db.commit()
