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

from sqlalchemy import (JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text,
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
# Customer-service cases use a simpler lifecycle.
SUPPORT_STATUSES = ("open", "resolved")

# What a case is about: a travel-rebate request, or a customer-service request.
CASE_KINDS = ("trip", "support")

# Kinds of identifiers we can use to recognize a returning client across channels.
IDENTITY_KINDS = ("messenger_psid", "email", "phone")

# Conversation lane chosen by the customer via the Messenger ice-breaker bubbles.
#   profiling -> the trip-rebate progressive-profiling bot (default)
#   concierge -> general-info AI assistant
#   human     -> no AI; route to a human agent and notify the backend
SUPPORT_MODES = ("profiling", "concierge", "human")


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
    # Messenger conversation lane (see SUPPORT_MODES); set by ice-breaker taps.
    support_mode: Mapped[str] = mapped_column(String(20), default="profiling")
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    tags: Mapped[list] = mapped_column(JSON, default=list)
    last_contact_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    # Single-use nonce for the current passwordless portal magic link (cleared
    # on first use). A fresh link overwrites it, so only the latest link works.
    portal_nonce: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # Client-completed identity/KYC details (legal names, DOB, address)
    # captured in the portal — needed to actually book a trip. No passport data.
    kyc: Mapped[dict] = mapped_column(JSON, default=dict)
    # Portal notification feed: list of {id, text, href, at, read}. Newest last.
    notifications: Mapped[list] = mapped_column(JSON, default=list)
    # Did the client allow us to use their reviews to improve the service?
    review_consent: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)

    identities = relationship(
        "ClientIdentity", back_populates="client",
        cascade="all, delete-orphan")
    requests = relationship(
        "Case", back_populates="client", order_by="Case.created_at.desc()")
    activities = relationship(
        "Interaction", back_populates="client",
        cascade="all, delete-orphan", order_by="Interaction.created_at.desc()")


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


# Kinds of timeline events we record per client.
ACTIVITY_KINDS = ("request_created", "message_in", "status_change", "reply_out",
                  "merge", "note", "follow_up", "claim", "assign")

# Back-office roles. 'admin' can do everything; 'agent' owns/follows up on cases.
STAFF_ROLES = ("admin", "agent")


class Interaction(Base):
    """One entry in a client's activity timeline (a message, a status change,
    an offer sent, a merge…). Append-only; drives the fiche-client timeline."""
    __tablename__ = "interactions"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, index=True)
    client_id: Mapped[int] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"), index=True)
    request_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("cases.id", ondelete="SET NULL"), nullable=True)
    kind: Mapped[str] = mapped_column(String(30))   # one of ACTIVITY_KINDS
    summary: Mapped[str] = mapped_column(Text)

    client = relationship("Client", back_populates="activities")


class Staff(Base):
    """A back-office team member who owns and follows up on cases.

    Phase 0: rows exist so a case can carry an owner even while the whole team
    shares one admin login (owner is then a convention picked from a menu).
    Phase 4: `password_hash` + individual sessions turn these into real per-user
    logins, and `owner_id` auto-fills from the session instead of a menu.
    Designed once here so no later phase needs a schema change."""
    __tablename__ = "staff"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    name: Mapped[str] = mapped_column(String(80))
    initials: Mapped[str] = mapped_column(String(6), default="")
    email: Mapped[Optional[str]] = mapped_column(String(200), nullable=True, index=True)
    role: Mapped[str] = mapped_column(String(20), default="admin")  # STAFF_ROLES
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    # Phase 4 (individual logins). Null = no direct login yet (shared admin).
    password_hash: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)

    owned_cases = relationship("Case", back_populates="owner")


class Case(Base):
    __tablename__ = "cases"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    # Owning client (nullable during/after migration; set on every new request).
    client_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("clients.id"), nullable=True, index=True)
    client = relationship("Client", back_populates="requests")

    channel: Mapped[str] = mapped_column(String(20))            # 'messenger' | 'form'
    # What this case is: a trip-rebate request or a customer-service request.
    kind: Mapped[str] = mapped_column(String(20), default="trip", index=True)
    status: Mapped[str] = mapped_column(String(20), default="new")
    # True while the ball is in OUR court: a client wrote and we haven't replied
    # or triaged yet. Drives the notification bell. Channel-agnostic.
    awaiting_reply: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
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
    # Conversation thread for service-client cases: list of {dir:'in'|'out', text, at}.
    messages: Mapped[list] = mapped_column(JSON, default=list)

    # Fulfillment fields, filled as the trip moves through the pipeline.
    quote_url: Mapped[Optional[str]] = mapped_column(String(600), nullable=True)   # quoted
    savings: Mapped[Optional[str]] = mapped_column(String(60), nullable=True)        # quoted
    flight_depart: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)  # booked
    flight_return: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)  # booked
    # Tripbook booking confirmation number — required to move a trip to "booked".
    booking_ref: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)    # booked
    # Client-submitted review on a finished trip: {rating:int, text:str, at:iso}
    review: Mapped[dict] = mapped_column(JSON, default=dict)

    # --- Ownership & follow-up (Phase 0/1) ---
    # Who's driving this dossier toward booked. Null = unclaimed (shared pool).
    owner_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("staff.id", ondelete="SET NULL"), nullable=True, index=True)
    owner = relationship("Staff", back_populates="owned_cases")
    # When the next proactive touch is due — drives the "À relancer" queue.
    next_follow_up_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True, index=True)
    # Last time anything happened on this dossier (message, status change, note,
    # follow-up). Backfilled from the timeline; drives staleness / "à risque".
    last_activity_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True, index=True)


def init_db() -> None:
    Base.metadata.create_all(engine)
    _ensure_columns()
    _backfill_clients()
    _seed_staff()
    _backfill_last_activity()
    _backfill_follow_ups()


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
            conn.execute(text(
                "ALTER TABLE cases ADD COLUMN IF NOT EXISTS messages JSONB DEFAULT '[]'::jsonb"
            ))
            conn.execute(text("ALTER TABLE cases ADD COLUMN IF NOT EXISTS quote_url VARCHAR(600)"))
            conn.execute(text("ALTER TABLE cases ADD COLUMN IF NOT EXISTS savings VARCHAR(60)"))
            conn.execute(text("ALTER TABLE cases ADD COLUMN IF NOT EXISTS flight_depart VARCHAR(40)"))
            conn.execute(text("ALTER TABLE cases ADD COLUMN IF NOT EXISTS flight_return VARCHAR(40)"))
            conn.execute(text("ALTER TABLE cases ADD COLUMN IF NOT EXISTS booking_ref VARCHAR(80)"))
            conn.execute(text("ALTER TABLE cases ADD COLUMN IF NOT EXISTS review JSONB DEFAULT '{}'::jsonb"))
            conn.execute(text("ALTER TABLE cases ADD COLUMN IF NOT EXISTS owner_id INTEGER"))
            conn.execute(text("ALTER TABLE cases ADD COLUMN IF NOT EXISTS next_follow_up_at TIMESTAMP"))
            conn.execute(text("ALTER TABLE cases ADD COLUMN IF NOT EXISTS last_activity_at TIMESTAMP"))
            # Support cases use open/resolved; remap any legacy trip-statuses.
            conn.execute(text(
                "UPDATE cases SET status='resolved' "
                "WHERE kind='support' AND status IN ('closed','resolved')"))
            conn.execute(text(
                "UPDATE cases SET status='open' "
                "WHERE kind='support' AND status NOT IN ('open','resolved')"))
            has = conn.execute(text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name='cases' AND column_name='awaiting_reply'"
            )).first()
            if not has:  # add + one-time backfill (open requests await a reply)
                conn.execute(text(
                    "ALTER TABLE cases ADD COLUMN awaiting_reply BOOLEAN DEFAULT TRUE"))
                conn.execute(text(
                    "UPDATE cases SET awaiting_reply = FALSE "
                    "WHERE status IN ('quoted','booked','closed')"))
            conn.execute(text(
                "ALTER TABLE clients ADD COLUMN IF NOT EXISTS "
                "support_mode VARCHAR(20) DEFAULT 'profiling'"))
            conn.execute(text(
                "ALTER TABLE clients ADD COLUMN IF NOT EXISTS portal_nonce VARCHAR(64)"))
            conn.execute(text(
                "ALTER TABLE clients ADD COLUMN IF NOT EXISTS kyc JSONB DEFAULT '{}'::jsonb"))
            conn.execute(text(
                "ALTER TABLE clients ADD COLUMN IF NOT EXISTS notifications JSONB DEFAULT '[]'::jsonb"))
            conn.execute(text(
                "ALTER TABLE clients ADD COLUMN IF NOT EXISTS review_consent BOOLEAN"))
            has_kind = conn.execute(text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name='cases' AND column_name='kind'")).first()
            if not has_kind:
                conn.execute(text("ALTER TABLE cases ADD COLUMN kind VARCHAR(20) DEFAULT 'trip'"))
                conn.execute(text(
                    "UPDATE cases SET kind='support' "
                    "WHERE needs_clarification::text ILIKE '%support humain%'"))
        elif engine.dialect.name == "sqlite":
            cols = [r[1] for r in conn.execute(text("PRAGMA table_info(cases)"))]
            if "screenshots" not in cols:
                conn.execute(text("ALTER TABLE cases ADD COLUMN screenshots JSON"))
            if "customer_phone" not in cols:
                conn.execute(text("ALTER TABLE cases ADD COLUMN customer_phone VARCHAR(40)"))
            if "client_id" not in cols:
                conn.execute(text("ALTER TABLE cases ADD COLUMN client_id INTEGER"))
            if "messages" not in cols:
                conn.execute(text("ALTER TABLE cases ADD COLUMN messages JSON"))
            if "quote_url" not in cols:
                conn.execute(text("ALTER TABLE cases ADD COLUMN quote_url VARCHAR(600)"))
            if "savings" not in cols:
                conn.execute(text("ALTER TABLE cases ADD COLUMN savings VARCHAR(60)"))
            if "flight_depart" not in cols:
                conn.execute(text("ALTER TABLE cases ADD COLUMN flight_depart VARCHAR(40)"))
            if "flight_return" not in cols:
                conn.execute(text("ALTER TABLE cases ADD COLUMN flight_return VARCHAR(40)"))
            if "booking_ref" not in cols:
                conn.execute(text("ALTER TABLE cases ADD COLUMN booking_ref VARCHAR(80)"))
            if "review" not in cols:
                conn.execute(text("ALTER TABLE cases ADD COLUMN review JSON"))
            if "owner_id" not in cols:
                conn.execute(text("ALTER TABLE cases ADD COLUMN owner_id INTEGER"))
            if "next_follow_up_at" not in cols:
                conn.execute(text("ALTER TABLE cases ADD COLUMN next_follow_up_at DATETIME"))
            if "last_activity_at" not in cols:
                conn.execute(text("ALTER TABLE cases ADD COLUMN last_activity_at DATETIME"))
            conn.execute(text(
                "UPDATE cases SET status='resolved' "
                "WHERE kind='support' AND status IN ('closed','resolved')"))
            conn.execute(text(
                "UPDATE cases SET status='open' "
                "WHERE kind='support' AND status NOT IN ('open','resolved')"))
            if "awaiting_reply" not in cols:  # add + one-time backfill
                conn.execute(text("ALTER TABLE cases ADD COLUMN awaiting_reply BOOLEAN DEFAULT 1"))
                conn.execute(text(
                    "UPDATE cases SET awaiting_reply = 0 "
                    "WHERE status IN ('quoted','booked','closed')"))
            ccols = [r[1] for r in conn.execute(text("PRAGMA table_info(clients)"))]
            if "support_mode" not in ccols:
                conn.execute(text(
                    "ALTER TABLE clients ADD COLUMN support_mode VARCHAR(20) DEFAULT 'profiling'"))
            if "portal_nonce" not in ccols:
                conn.execute(text("ALTER TABLE clients ADD COLUMN portal_nonce VARCHAR(64)"))
            if "kyc" not in ccols:
                conn.execute(text("ALTER TABLE clients ADD COLUMN kyc JSON"))
            if "notifications" not in ccols:
                conn.execute(text("ALTER TABLE clients ADD COLUMN notifications JSON"))
            if "review_consent" not in ccols:
                conn.execute(text("ALTER TABLE clients ADD COLUMN review_consent BOOLEAN"))
            if "kind" not in cols:
                conn.execute(text("ALTER TABLE cases ADD COLUMN kind VARCHAR(20) DEFAULT 'trip'"))
                conn.execute(text(
                    "UPDATE cases SET kind='support' "
                    "WHERE needs_clarification LIKE '%support humain%'"))


def _seed_staff() -> None:
    """Ensure at least one staff row exists so a case can have an owner from day
    one. Seeds from ADMIN_USER (the shared login) when the table is empty.
    Idempotent: does nothing once any staff row exists."""
    with SessionLocal() as s:
        if s.query(Staff).count() == 0:
            name = (settings.ADMIN_USER or "Admin").strip() or "Admin"
            parts = [p for p in name.replace(".", " ").split() if p]
            initials = ("".join(p[0] for p in parts[:2]) or name[:2]).upper()
            s.add(Staff(name=name, initials=initials, role="admin", active=True))
            s.commit()


def _backfill_last_activity() -> None:
    """One-time fill of last_activity_at for rows that don't have it yet: the most
    recent interaction on the case, falling back to the case's created_at.
    Idempotent: only touches rows where last_activity_at IS NULL."""
    with engine.begin() as conn:
        if engine.dialect.name == "postgresql":
            conn.execute(text(
                "UPDATE cases c SET last_activity_at = COALESCE("
                "(SELECT MAX(i.created_at) FROM interactions i WHERE i.request_id = c.id), "
                "c.created_at) WHERE c.last_activity_at IS NULL"))
        else:  # sqlite
            conn.execute(text(
                "UPDATE cases SET last_activity_at = COALESCE("
                "(SELECT MAX(created_at) FROM interactions WHERE request_id = cases.id), "
                "created_at) WHERE last_activity_at IS NULL"))


def _backfill_follow_ups() -> None:
    """One-time: give existing in-progress trip dossiers (needs_info/quoted) with
    no follow-up date one, derived from their last activity, so the 'À relancer'
    queue is populated from day one rather than only on future transitions.
    Idempotent: only fills rows where next_follow_up_at IS NULL."""
    with SessionLocal() as s:
        rows = (s.query(Case)
                .filter(Case.kind == "trip",
                        Case.status.in_(("needs_info", "quoted")),
                        Case.next_follow_up_at.is_(None))
                .all())
        for c in rows:
            base = c.last_activity_at or c.created_at or datetime.utcnow()
            schedule_follow_up(c, from_dt=base)
        if rows:
            s.commit()


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
    """Most recent still-open TRIP request for THIS client, any channel, within
    the merge window. New trip messages from a known client fold into it; once
    quoted/booked/closed, the next message starts a fresh one. Service-client
    (support) cases are excluded so trip profiling never merges into them."""
    if not client_id:
        return None
    cutoff = datetime.utcnow() - timedelta(days=within_days)
    return (
        db.query(Case)
        .filter(
            Case.client_id == client_id,
            Case.kind == "trip",
            Case.status.in_(OPEN_STATUSES),
            Case.created_at >= cutoff,
        )
        .order_by(Case.created_at.desc())
        .first()
    )


# --------------------------------------------------------------------------- #
# Follow-up & activity helpers (Phase 1)
# --------------------------------------------------------------------------- #

# Default delay before the next proactive touch when a case needs follow-up.
DEFAULT_FOLLOW_UP_DAYS = 2
# A quoted dossier with no activity for this many days is flagged "à risque".
STALE_QUOTED_DAYS = 3
# Statuses that still need active follow-up to move toward booked.
FOLLOW_UP_STATUSES = ("new", "needs_info", "quoted")


def add_business_days(start: datetime, days: int) -> datetime:
    """start + N business days (skips Sat/Sun); keeps the time-of-day."""
    d, added = start, 0
    while added < days:
        d = d + timedelta(days=1)
        if d.weekday() < 5:   # Mon..Fri
            added += 1
    return d


def schedule_follow_up(case: "Case", days: int = DEFAULT_FOLLOW_UP_DAYS,
                       from_dt: Optional[datetime] = None) -> None:
    """Set the next proactive-touch date on a case (business-day aware)."""
    case.next_follow_up_at = add_business_days(from_dt or datetime.utcnow(), days)


def touch_activity(case: "Case", when: Optional[datetime] = None) -> None:
    """Mark that something just happened on this dossier (drives staleness)."""
    case.last_activity_at = when or datetime.utcnow()


def is_stale_quoted(case: "Case", now: Optional[datetime] = None) -> bool:
    """A quoted dossier that hasn't moved in STALE_QUOTED_DAYS — a cooling lead."""
    if case.status != "quoted" or not case.last_activity_at:
        return False
    return (now or datetime.utcnow()) - case.last_activity_at >= timedelta(days=STALE_QUOTED_DAYS)


def due_follow_ups(db, now: Optional[datetime] = None, owner_id: Optional[int] = None):
    """In-progress trip dossiers whose follow-up is due (next_follow_up_at <= now),
    most overdue first. Optionally scoped to one owner. Excludes booked/closed
    and support cases."""
    now = now or datetime.utcnow()
    q = (db.query(Case)
         .filter(Case.kind == "trip",
                 Case.status.in_(FOLLOW_UP_STATUSES),
                 Case.next_follow_up_at.isnot(None),
                 Case.next_follow_up_at <= now))
    if owner_id is not None:
        q = q.filter(Case.owner_id == owner_id)
    return q.order_by(Case.next_follow_up_at.asc()).all()


def mark_follow_up_for_status(case: "Case", status: str,
                              now: Optional[datetime] = None) -> None:
    """Apply Phase-1 pipeline signals for the status a case just entered.
    Call right after setting the new status. Stamps activity, schedules the next
    proactive touch for statuses that need follow-up (needs_info/quoted), and
    clears it for terminal statuses (booked/closed). 'new' gets no follow-up
    date on purpose — fresh cases live in the 'À répondre' inbox, not the
    follow-up queue."""
    now = now or datetime.utcnow()
    touch_activity(case, now)
    if status in ("needs_info", "quoted"):
        schedule_follow_up(case, from_dt=now)
    elif status in ("booked", "closed"):
        case.next_follow_up_at = None


# --------------------------------------------------------------------------- #
# Ownership helpers (Phase 2) — who's on each dossier
# --------------------------------------------------------------------------- #

def active_staff(db):
    """Active back-office members, for owner pickers and 'acting as' menus."""
    return (db.query(Staff).filter(Staff.active.is_(True))
            .order_by(Staff.name.asc()).all())


def default_staff_id(db) -> Optional[int]:
    """Fallback 'current' staff while the team shares one login: the first active
    admin (Phase 4 replaces this with the authenticated session's staff)."""
    s = (db.query(Staff).filter(Staff.active.is_(True))
         .order_by(Staff.role.asc(), Staff.id.asc()).first())   # 'admin' < 'agent'
    return s.id if s else None


def unclaimed_cases(db):
    """In-progress trip dossiers with no owner — the shared 'À réclamer' pool,
    oldest first (the ones waiting longest to be picked up)."""
    return (db.query(Case)
            .filter(Case.kind == "trip",
                    Case.status.in_(FOLLOW_UP_STATUSES),
                    Case.owner_id.is_(None))
            .order_by(Case.created_at.asc()).all())


def cases_for_owner(db, owner_id: int):
    """In-progress trip dossiers owned by a staff member ('Mes dossiers'):
    follow-up due first (most overdue on top), then the rest by oldest."""
    return (db.query(Case)
            .filter(Case.kind == "trip",
                    Case.status.in_(FOLLOW_UP_STATUSES),
                    Case.owner_id == owner_id)
            .order_by(Case.next_follow_up_at.is_(None).asc(),
                      Case.next_follow_up_at.asc(),
                      Case.created_at.asc()).all())


def count_unclaimed(db) -> int:
    return (db.query(Case)
            .filter(Case.kind == "trip",
                    Case.status.in_(FOLLOW_UP_STATUSES),
                    Case.owner_id.is_(None)).count())


def unclaimed_support_cases(db):
    """In-progress support requests with no owner — the service pool to claim."""
    return (db.query(Case)
            .filter(Case.kind == "support",
                    Case.status.notin_(("closed", "resolved")),
                    Case.owner_id.is_(None))
            .order_by(Case.created_at.desc()).all())


def count_unclaimed_support(db) -> int:
    return (db.query(Case)
            .filter(Case.kind == "support",
                    Case.status.notin_(("closed", "resolved")),
                    Case.owner_id.is_(None)).count())


# --------------------------------------------------------------------------- #
# Pipeline metrics (Phase 3) — performance d'affaire
# --------------------------------------------------------------------------- #

def pipeline_stats(db, now: Optional[datetime] = None) -> dict:
    """Aggregate trip-pipeline numbers for the perf dashboard: current stage
    distribution, win rate (booked vs resolved), follow-up pressure (overdue +
    à risque), average age of the open pipeline, and a per-owner leaderboard."""
    from collections import Counter
    now = now or datetime.utcnow()
    rows = db.query(Case.status).filter(Case.kind == "trip").all()
    counts = Counter(s for (s,) in rows)
    by_status = {s: counts.get(s, 0) for s in STATUSES}
    booked = by_status.get("booked", 0)
    closed = by_status.get("closed", 0)
    resolved = booked + closed
    win_rate = round(booked / resolved * 100) if resolved else None

    overdue = len(due_follow_ups(db, now))
    quoted_cases = (db.query(Case)
                    .filter(Case.kind == "trip", Case.status == "quoted").all())
    at_risk = sum(1 for c in quoted_cases if is_stale_quoted(c, now))

    open_cases = (db.query(Case)
                  .filter(Case.kind == "trip",
                          Case.status.in_(FOLLOW_UP_STATUSES)).all())
    avg_age = (round(sum((now - c.created_at).days for c in open_cases) / len(open_cases), 1)
               if open_cases else 0)

    per_owner = []
    for s in active_staff(db):
        owned_open = (db.query(Case)
                      .filter(Case.kind == "trip", Case.owner_id == s.id,
                              Case.status.in_(FOLLOW_UP_STATUSES)).count())
        won = (db.query(Case)
               .filter(Case.kind == "trip", Case.owner_id == s.id,
                       Case.status == "booked").count())
        if owned_open or won:
            per_owner.append({"name": s.name, "open": owned_open, "won": won})
    per_owner.sort(key=lambda r: (-r["won"], -r["open"]))

    return {"by_status": by_status, "total": sum(by_status.values()),
            "booked": booked, "resolved": resolved, "win_rate": win_rate,
            "overdue": overdue, "at_risk": at_risk, "avg_age": avg_age,
            "unclaimed": count_unclaimed(db), "per_owner": per_owner}


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


def replace_primary_identity(db, client: "Client", kind: str, value: Optional[str]):
    """Make `value` this client's SOLE identity of `kind`, used when the client
    edits their canonical contact in the portal profile: drop this client's other
    identities of that kind (old values / typos that piled up across edits), then
    attach the new one. Only touches THIS client's rows — never another client's.
    No-op on an empty value (so we never wipe identities by accident)."""
    if not value:
        return
    for ident in db.query(ClientIdentity).filter_by(client_id=client.id, kind=kind).all():
        if ident.value != value:
            db.delete(ident)
    db.flush()
    add_identity(db, client, kind, value)


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


def log_activity(db, client_id: Optional[int], kind: str, summary: str,
                 request_id: Optional[int] = None) -> None:
    """Append a timeline entry for a client. No-op if there's no client."""
    if not client_id:
        return
    db.add(Interaction(client_id=client_id, kind=kind,
                       summary=(summary or "")[:500], request_id=request_id))


def push_notification(db, client_id: Optional[int], text_: str,
                      href: Optional[str] = None) -> None:
    """Append an unread notification to a client's portal feed (capped at 50).
    Used for trip status changes and replies to help requests."""
    if not client_id:
        return
    import secrets
    cl = db.get(Client, client_id)
    if not cl:
        return
    items = list(cl.notifications or [])
    items.append({
        "id": secrets.token_hex(6),
        "text": (text_ or "")[:300],
        "href": href,
        "at": datetime.utcnow().isoformat(timespec="seconds"),
        "read": False,
    })
    cl.notifications = items[-50:]


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


# --------------------------------------------------------------------------- #
# Duplicate detection + manual merge
# --------------------------------------------------------------------------- #
import unicodedata as _ud


def _norm_name(name: Optional[str]) -> Optional[str]:
    if not name:
        return None
    n = _ud.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    n = " ".join(n.split())
    return n or None


def find_duplicate_groups(db) -> list[list["Client"]]:
    """Clients that likely are the same person. Identities are globally unique,
    so true duplicates share NO identifier — the practical signal is a matching
    (accent/case-insensitive) name. Returns groups of 2+ clients."""
    groups: dict[str, list] = {}
    for cl in db.query(Client).all():
        key = _norm_name(cl.display_name)
        if key:
            groups.setdefault(key, []).append(cl)
    return [g for g in groups.values() if len(g) > 1]


def merge_clients(db, src_id: int, target_id: int) -> bool:
    """Merge client `src` INTO `target`: move all requests, identities and
    activity to target, fold in any fields target is missing, then delete src.
    Irreversible. Returns True on success."""
    if not src_id or not target_id or src_id == target_id:
        return False
    src = db.get(Client, src_id)
    target = db.get(Client, target_id)
    if not src or not target:
        return False

    label = src.display_name or src.primary_email or src.primary_phone or f"#{src_id}"

    # Reassign everything that points at src. (kind, value) is globally unique,
    # so identities can never collide between two real clients.
    db.query(Case).filter(Case.client_id == src_id).update(
        {"client_id": target_id}, synchronize_session=False)
    db.query(ClientIdentity).filter(ClientIdentity.client_id == src_id).update(
        {"client_id": target_id}, synchronize_session=False)
    db.query(Interaction).filter(Interaction.client_id == src_id).update(
        {"client_id": target_id}, synchronize_session=False)

    # Fold src's fields into any blanks on target.
    if not target.display_name and src.display_name:
        target.display_name = src.display_name
    if not target.primary_email and src.primary_email:
        target.primary_email = src.primary_email
    if not target.primary_phone and src.primary_phone:
        target.primary_phone = src.primary_phone
    if not target.preferred_channel and src.preferred_channel:
        target.preferred_channel = src.preferred_channel
    if src.notes:
        target.notes = (target.notes + "\n" + src.notes) if target.notes else src.notes
    if src.tags:
        target.tags = list(dict.fromkeys((target.tags or []) + list(src.tags)))
    if src.last_contact_at and (not target.last_contact_at
                                or src.last_contact_at > target.last_contact_at):
        target.last_contact_at = src.last_contact_at

    db.add(Interaction(client_id=target_id, kind="merge",
                       summary=f"Fiche fusionnée : {label} (#{src_id}) → ce client"))

    # Expire src so its (now-empty) relationships reload before the cascade delete,
    # otherwise delete-orphan could remove the rows we just repointed.
    db.flush()
    db.expire(src)
    db.delete(src)
    db.flush()
    return True
