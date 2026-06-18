"""
main.py
=======
The whole Phase 1 backend, deployable to Railway as one service.

Endpoints
---------
GET  /                       health check
GET  /webhook                Meta verification handshake
POST /webhook                inbound Messenger messages (acks fast, parses async)
POST /intake                 the Netlify form posts a TripRequest here
GET  /admin                  -> login form (or /admin/cases if logged in)
GET  /admin/cases            list of cases (session login)
GET  /admin/cases/{id}       one case (session login)
POST /admin/cases/{id}/status   update a case's status (session login)

Key design choices (carried from our strategy):
* The webhook ACKS 200 immediately and parses in a BackgroundTask, so Claude's
  call never blocks Meta's request (Meta retries slow webhooks -> duplicate
  cases). For real volume, swap BackgroundTasks for a proper queue (Redis/RQ).
* The Netlify form and the webhook both produce the SAME TripRequest -> one
  schema, one table, two front doors.
* If parsing fails (e.g. no API key), the message is NEVER lost: a fallback
  case is stored with the raw text so a human can pick it up.
"""
from __future__ import annotations

import base64
import logging
import os
import re
import time
import urllib.request
from contextlib import asynccontextmanager
from datetime import datetime
from html import escape

from fastapi import (BackgroundTasks, Depends, FastAPI, File, Form, HTTPException,
                     Request, Response, UploadFile)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, text
from starlette.middleware.sessions import SessionMiddleware

from auth import NotAuthenticated, check_credentials, require_admin
from concierge import concierge_reply
from config import settings
import storage
from db import (STATUSES, Case, Client, ClientIdentity, Interaction, SessionLocal, engine,
                add_identity, find_client_by_identity, find_duplicate_groups,
                find_open_case_for_sender, find_open_request_for_client, init_db, log_activity,
                merge_clients, normalize_email, normalize_phone, resolve_or_create_client,
                SUPPORT_STATUSES)
from facebook import (extract_messages, extract_postbacks, extract_quick_replies,
                      get_user_name, send_quick_replies, send_text, set_ice_breakers,
                      set_messenger_profile, set_persistent_menu, valid_signature,
                      verify_challenge)
from parser import parse_trip
from trip_schema import ContactChannel, TripRequest, merge_trip_requests

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("duvoyageur")

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    _backfill_trip_contact()
    _migrate_screenshots_to_r2()
    yield


app = FastAPI(title="Du Voyageur — Intake & Cases", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Signed session cookie that backs the admin login form.
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.SECRET_KEY,
    session_cookie="dv_admin",
    max_age=settings.SESSION_MAX_AGE,
    same_site="lax",
    https_only=settings.SECURE_COOKIES,
)


@app.exception_handler(NotAuthenticated)
async def _redirect_to_login(request: Request, exc: NotAuthenticated):
    """Protected pages bounce unauthenticated visitors to the login form."""
    return RedirectResponse("/admin/login", status_code=303)


# Public static assets (login background, logo). Absolute path so it resolves
# regardless of the working directory Railway starts the process from.
_STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")


# --------------------------------------------------------------------------- #
# Storage helper
# --------------------------------------------------------------------------- #
def store_case(channel: str, trip: TripRequest, sender_ref: str | None = None,
               shots: list | None = None) -> int:
    with SessionLocal() as db:
        rem = trip.remaining_fields()
        client = resolve_or_create_client(
            db,
            messenger_psid=sender_ref if channel == "messenger" else None,
            email=trip.customer_email,
            phone=trip.customer_phone,
            name=trip.customer_name,
            channel=channel,
        )
        case = Case(
            client_id=client.id,
            channel=channel,
            status="needs_info" if rem else "new",
            sender_ref=sender_ref,
            customer_email=trip.customer_email,
            customer_phone=trip.customer_phone,
            parse_confidence=trip.parse_confidence,
            raw_message=trip.raw_message,
            trip=trip.model_dump(),
            needs_clarification=rem,
            screenshots=shots or [],
        )
        db.add(case)
        db.flush()
        log_activity(db, client.id, "request_created",
                     f"Nouvelle demande via {channel}", case.id)
        db.commit()
        db.refresh(case)
        log.info("Stored case #%s via %s (conf=%.2f)", case.id, channel, trip.parse_confidence)
        return case.id


def _download_image(url: str) -> tuple[bytes, str] | tuple[None, None]:
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            data = r.read()
            media_type = r.headers.get("Content-Type", "image/png").split(";")[0]
            return data, media_type
    except Exception as e:  # noqa: BLE001
        log.warning("Could not download attachment: %s", e)
        return None, None


# --------------------------------------------------------------------------- #
# Async processing of one Messenger message
# --------------------------------------------------------------------------- #
# When this many fields are still unknown and no screenshot has arrived, asking
# for a screenshot is the highest-leverage move (it fills several fields at once).
SCREENSHOT_FIRST_THRESHOLD = 5

# Per-sender timestamp of the latest inbound message, used to debounce replies so
# a burst (text + screenshot as separate events) gets ONE reply on merged state.
_last_msg_at: dict[str, float] = {}
# Per-sender metadata about the latest message: did it add info, and its text.
_last_msg_meta: dict[str, dict] = {}

_TRIP_NOISE = {"raw_message", "needs_clarification", "parse_confidence", "agent_notes",
               "source", "customer_name"}


def _trip_changed(before: TripRequest, after: TripRequest) -> bool:
    """True if the message added or changed real trip data (not just metadata)."""
    return before.model_dump(exclude=_TRIP_NOISE) != after.model_dump(exclude=_TRIP_NOISE)


def _send_debounced_reply(sender: str, my_stamp: float) -> bool:
    """Reply once per burst: profiling question, or a concierge answer to a
    general question — never the canned line on repeat."""
    if _last_msg_at.get(sender) != my_stamp:
        return False  # a newer message superseded this one; it will reply
    meta = _last_msg_meta.get(sender, {})
    text = meta.get("text", "") or ""
    advanced = meta.get("advanced", True)
    with SessionLocal() as db:
        case = find_open_case_for_sender(db, sender)
        if not case:
            return False
        trip = TripRequest.model_validate(case.trip)
        has_shot = bool(case.screenshots)
        case_id = case.id

    # The message brought new info (or was a screenshot) -> keep profiling.
    rem = trip.remaining_fields()
    # Treat as a general question only if it looks like one, or there's nothing
    # left to profile (so a vague opener still gets the screenshot-first prompt).
    is_question = ("?" in text) or (not rem)
    if advanced or not text or not settings.CONCIERGE_ENABLED or not is_question:
        reply = _ack_message(trip, has_shot)
    else:
        # No new info + looks like a question -> answer it (hybrid), then, if we're
        # still profiling, re-ask the pending question.
        answer = concierge_reply(text, trip)
        if rem:
            q = trip.next_question()
            reply = (answer + " " + q) if answer else "Merci ! 🌴 " + q
        else:
            reply = answer or ("Bonne question ! 🌴 Un conseiller va te revenir bientôt "
                               "avec ton offre et pourra répondre à ça. 👍")
    sent = send_text(sender, reply, settings.FB_PAGE_TOKEN, settings.FB_GRAPH_VERSION)
    if sent:                                            # keep the full conversation
        _record_message(case_id, "out", reply)
    return bool(sent)


def _ack_message(trip: TripRequest, has_screenshot: bool = False) -> str:
    """Customer-facing acknowledgment: one relevant next step at a time."""
    rem = trip.remaining_fields()
    if not rem:
        return ("Merci ! 🌴 On a tout ce qu'il faut — on regarde ton forfait et on te "
                "revient bientôt par courriel avec ton rabais. 👍")
    # Screenshot-first: when lots is still missing and we have no image yet.
    if not has_screenshot and len(rem) >= SCREENSHOT_FIRST_THRESHOLD:
        return ("Merci ! 🌴 Le plus rapide : envoie-moi une capture d'écran du forfait "
                "que t'as trouvé 📸 — ça me donne presque tout d'un coup. Sinon, "
                "dis-moi juste la destination qui t'intéresse.")
    return "Merci ! 🌴 " + trip.next_question()


def process_messenger_message(sender: str | None, text: str, image_urls: list[str]) -> None:
    # Mark this as the latest message from this sender (for reply debouncing).
    my_stamp = time.monotonic()
    if sender:
        _last_msg_at[sender] = my_stamp

    # Download every screenshot attached to this message.
    downloaded: list[tuple[bytes, str]] = []
    for url in (image_urls or []):
        b, mt = _download_image(url)
        if b:
            downloaded.append((b, mt or "image/png"))

    # Persistable screenshot records (uploaded to R2 when configured, else b64).
    shots = [storage.make_screenshot(b, mt) for (b, mt) in downloaded]

    # Route by the conversation lane chosen via the ice-breaker bubbles. Human and
    # concierge lanes skip the trip-parsing AI entirely.
    mode = "profiling"
    is_cold = True
    if sender:
        with SessionLocal() as db:
            cl = find_client_by_identity(db, "messenger_psid", sender)
            if cl:
                mode = cl.support_mode or "profiling"
                is_cold = find_open_request_for_client(db, cl.id) is None
    if mode == "human":
        # Always record the message for the agent. If the customer signals rebate
        # intent, OFFER (once) to hand them to the profiling bot — their choice.
        _handle_human_message(sender, text, shots)
        if sender and _wants_rebate(text) and sender not in _rebate_offered:
            _rebate_offered.add(sender)
            send_quick_replies(sender, REBATE_OFFER_TEXT, REBATE_OFFER_QR,
                               settings.FB_PAGE_TOKEN, settings.FB_GRAPH_VERSION)
            log.info("Rebate-switch offer sent to %s", sender)
        return
    if mode == "concierge":
        _handle_concierge_message(sender, text)
        return

    # Profiling lane (default). Two shortcuts for people who type instead of
    # tapping a bubble:
    if sender and _wants_human(text):           # "je veux parler à un conseiller"
        _set_support_mode(sender, "human")
        _handle_human_message(sender, text, shots)
        send_text(sender,
                  "Parfait 🙏 Un conseiller va te répondre directement ici. On revient vite !",
                  settings.FB_PAGE_TOKEN, settings.FB_GRAPH_VERSION)
        return
    if (sender and not shots and is_cold and sender not in _triaged
            and _is_vague(text)):               # vague opener -> offer the 3 lanes once
        _triaged.add(sender)
        send_quick_replies(sender, TRIAGE_TEXT, TRIAGE_QR,
                           settings.FB_PAGE_TOKEN, settings.FB_GRAPH_VERSION)
        log.info("Triage quick replies sent to %s", sender)
        return

    try:
        new_trip = parse_trip(text, images=downloaded)
    except Exception as e:  # noqa: BLE001
        # Never drop a customer message — store a fallback the agent can rescue.
        log.exception("Parse failed; storing fallback case: %s", e)
        new_trip = TripRequest(raw_message=text or "(capture d'écran)", source="messenger",
                               agent_notes=f"Parsing automatique échoué: {e}",
                               needs_clarification=["à traiter manuellement"])

    # Find-or-merge: keep one evolving request per CLIENT (progressive profiling).
    with SessionLocal() as db:
        # Resolve the client behind this PSID up front (creates on first contact;
        # may also match a known client by an email/phone they typed in the chat).
        client = resolve_or_create_client(
            db, messenger_psid=sender,
            email=new_trip.customer_email, phone=new_trip.customer_phone,
            name=new_trip.customer_name, channel="messenger",
        ) if sender else None
        # A returning client already gave us their contact on a past request —
        # carry it onto this new one so the bot doesn't ask again (email OR phone
        # satisfies the requirement).
        if client and not new_trip.customer_email and client.primary_email:
            new_trip.customer_email = client.primary_email
        if client and not new_trip.customer_phone and client.primary_phone:
            new_trip.customer_phone = client.primary_phone
        existing = find_open_request_for_client(db, client.id if client else None)
        if existing:
            before_trip = TripRequest.model_validate(existing.trip)
            was_complete = not before_trip.remaining_fields()
            # Record the inbound message in the thread BEFORE raw_message changes.
            _inbound = text or ("(capture d'écran envoyée)" if shots else "")
            if _inbound:
                existing.messages = (_conversation(existing)
                                     + [{"dir": "in", "text": _inbound,
                                         "at": datetime.utcnow().isoformat(timespec="seconds")}])
            if was_complete and text and not downloaded:
                # Dossier already finalized + a text-only message => treat as a
                # question. Don't re-profile, but DO capture late contact details
                # (email / phone) so the case and client stay in sync.
                trip = before_trip.model_copy(deep=True)
                if new_trip.customer_email:
                    trip.customer_email = new_trip.customer_email
                if new_trip.customer_phone:
                    trip.customer_phone = new_trip.customer_phone
                trip.raw_message = ((existing.raw_message + "\n---\n" + text)
                                    if existing.raw_message else text)
                existing.raw_message = trip.raw_message
                existing.customer_email = trip.customer_email or existing.customer_email
                existing.customer_phone = trip.customer_phone or existing.customer_phone
                existing.trip = trip.model_dump()
                advanced = False
            else:
                trip = merge_trip_requests(before_trip, new_trip)
                advanced = (_trip_changed(before_trip, trip)
                            or len(trip.remaining_fields()) < len(before_trip.remaining_fields()))
                existing.trip = trip.model_dump()
                existing.needs_clarification = trip.needs_clarification
                existing.parse_confidence = trip.parse_confidence
                existing.raw_message = trip.raw_message
                existing.customer_email = trip.customer_email or existing.customer_email
                existing.customer_phone = trip.customer_phone or existing.customer_phone
                existing.status = "needs_info" if trip.needs_clarification else "new"
                if shots:                               # accumulate screenshots
                    existing.screenshots = (existing.screenshots or []) + shots
            existing.awaiting_reply = True              # client wrote -> our move
            log_activity(db, existing.client_id, "message_in",
                         "Message reçu sur Messenger", existing.id)
            db.commit()
            log.info("Merged into case #%s (sender %s, +%d shots, advanced=%s)",
                     existing.id, sender, len(shots), advanced)
        else:
            # Resolve the customer's Facebook name once, when the request is created.
            if sender and settings.FB_PAGE_TOKEN and not new_trip.customer_name:
                name = get_user_name(sender, settings.FB_PAGE_TOKEN, settings.FB_GRAPH_VERSION)
                if name:
                    new_trip.customer_name = name
                    if client and not client.display_name:
                        client.display_name = name
            trip = new_trip
            rem = new_trip.remaining_fields()
            _inbound = text or ("(capture d'écran envoyée)" if shots else "")
            case = Case(
                client_id=client.id if client else None,
                channel="messenger",
                status="needs_info" if rem else "new",
                sender_ref=sender,
                customer_email=new_trip.customer_email,
                customer_phone=new_trip.customer_phone,
                parse_confidence=new_trip.parse_confidence,
                raw_message=new_trip.raw_message,
                trip=new_trip.model_dump(),
                needs_clarification=rem,
                screenshots=shots,
                messages=([{"dir": "in", "text": _inbound,
                            "at": datetime.utcnow().isoformat(timespec="seconds")}]
                          if _inbound else []),
            )
            db.add(case)
            db.flush()
            log_activity(db, client.id if client else None, "request_created",
                         "Nouvelle demande via Messenger", case.id)
            db.commit()
            db.refresh(case)
            advanced = _trip_changed(TripRequest(source="messenger"), new_trip) or bool(new_trip.agent_notes)
            log.info("New case #%s (sender %s, %d shots, advanced=%s)",
                     case.id, sender, len(shots), advanced)

    # Record routing metadata for the debounced reply (OR 'advanced' across a
    # burst so a screenshot in the burst keeps us in profiling mode).
    if sender:
        prev = _last_msg_meta.get(sender)
        adv, txt = advanced, text
        if prev and (my_stamp - prev.get("at", 0)) < settings.ACK_DEBOUNCE_SECONDS + 2:
            adv = adv or prev.get("advanced", False)
            txt = text or prev.get("text", "")
        _last_msg_meta[sender] = {"text": txt, "advanced": adv, "at": my_stamp}

    # Debounced acknowledgment: wait briefly so a burst (text + screenshot as
    # separate events) produces ONE reply built on the final merged state. Only
    # the last message in the burst actually replies. Never breaks processing.
    if sender and settings.FB_PAGE_TOKEN:
        time.sleep(settings.ACK_DEBOUNCE_SECONDS)
        sent = _send_debounced_reply(sender, my_stamp)
        log.info("Reply to %s: %s", sender, "sent" if sent else "skipped (superseded)")


# --------------------------------------------------------------------------- #
# Public endpoints
# --------------------------------------------------------------------------- #
@app.get("/")
def health():
    return {"ok": True, "service": "duvoyageur-backend"}


# --------------------------------------------------------------------------- #
# Messenger welcome screen
# --------------------------------------------------------------------------- #
GET_STARTED_PAYLOAD = "GET_STARTED"

# Static text shown on the welcome screen BEFORE the user types. Messenger
# auto-fills {{user_first_name}}.
GREETING_TEXT = (
    "Salut {{user_first_name}} ! 🌴 Trouve ton forfait tout inclus, envoie-nous "
    "une capture d'écran, et on te trouve le même voyage avec un rabais. 💸"
)

# Ice-breaker bubbles shown when a user first opens the conversation. Each tap
# fires its payload as a postback (handled below), which sets the conversation
# lane on the client so subsequent messages route to the right subsystem.
IB_PROFILING = "IB_PROFILING"   # trip-rebate progressive profiling (default)
IB_CONCIERGE = "IB_CONCIERGE"   # general-info AI concierge
IB_HUMAN = "IB_HUMAN"           # human support, no AI, notify the backend

ICE_BREAKERS = [
    {"question": "✈️ Trouver un rabais sur mon voyage", "payload": IB_PROFILING},
    {"question": "❓ Question générale", "payload": IB_CONCIERGE},
    {"question": "🧑‍💼 Parler à un conseiller", "payload": IB_HUMAN},
]

# Quick-reply chips shown on a vague cold open (user typed instead of tapping a
# bubble). A tap sends the payload back, routed exactly like an ice-breaker.
TRIAGE_TEXT = ("Salut ! 🌴 Bienvenue chez Du Voyageur. Comment puis-je t'aider ?")
TRIAGE_QR = [
    {"content_type": "text", "title": "✈️ Rabais voyage", "payload": IB_PROFILING},
    {"content_type": "text", "title": "❓ Infos générales", "payload": IB_CONCIERGE},
    {"content_type": "text", "title": "👤 Un conseiller", "payload": IB_HUMAN},
]

# Always-available hamburger (☰) menu — same three lanes, any time in the chat.
PERSISTENT_MENU = [
    {"type": "postback", "title": "✈️ Trouver un rabais", "payload": IB_PROFILING},
    {"type": "postback", "title": "❓ Question générale", "payload": IB_CONCIERGE},
    {"type": "postback", "title": "👤 Parler à un conseiller", "payload": IB_HUMAN},
]

# Senders already shown the triage chips (once per process lifetime, avoids spam).
_triaged: set[str] = set()

# When a human-lane customer signals rebate intent, we OFFER to switch to the
# profiling bot (rather than hijacking the conversation). Shown once per sender.
REBATE_OFFER_TEXT = ("On dirait que tu cherches un rabais sur un voyage 🌴 "
                     "Je peux m'en occuper tout de suite — on y va ?")
REBATE_OFFER_QR = [
    {"content_type": "text", "title": "✈️ Oui, mon rabais", "payload": IB_PROFILING},
    {"content_type": "text", "title": "👤 Un conseiller", "payload": IB_HUMAN},
]
_rebate_offered: set[str] = set()

_HUMAN_RE = re.compile(
    r"(agent|conseill\w+|repr[ée]sentant|humain|une personne|vraie personne|"
    r"quelqu'?un|parler [àa]|service (?:[àa] la )?client\w*|\bsupport\b)", re.I)
_GREETING_RE = re.compile(
    r"^\s*(allo|all[ôo]|bonjour|bonsoir|salut|coucou|hello|hi|hey|yo|"
    r"info\w*|question|aide|help|renseign\w*|besoin)", re.I)
_TRIP_HINT_RE = re.compile(
    r"(forfait|voyage|tout[- ]inclus|h[ôo]tel|s[ée]jour|rabais|prix|destination|"
    r"\bvol\b|partir|semaine|nuits?|cuba|mexi\w+|punta|cana|varadero|canc[uú]n|"
    r"r[ée]publique|dominicaine|jama[ïi]que|riviera|maya|floride|plage|resort)", re.I)

# Strong "I want a deal / to book a trip" intent — used to pull a customer out of
# the human-support lane back into trip profiling when they pivot to the funnel.
_REBATE_RE = re.compile(
    r"(rabais|moins cher|meilleur prix|bon prix|[ée]conomis\w+|\bdeal\b|aubaine|"
    r"(?:trouver?|obtenir|avoir|veux|cherche\w*|r[ée]server)\b[^.?!]*"
    r"\b(?:voyage|forfait|rabais|tout[- ]inclus|s[ée]jour)\b|"
    r"soumission|cotation|coter)", re.I)


def _wants_rebate(text: str) -> bool:
    return bool(text and _REBATE_RE.search(text))


def _wants_human(text: str) -> bool:
    return bool(text and _HUMAN_RE.search(text))


def _is_vague(text: str) -> bool:
    """A cold opener with no clear travel intent — worth offering the 3 lanes."""
    t = (text or "").strip()
    if not t or _TRIP_HINT_RE.search(t):
        return False  # empty (screenshot-only) or a clear trip request -> profiling
    if _GREETING_RE.search(t):
        return True
    return len(t.split()) <= 4 and not any(ch.isdigit() for ch in t)


def _set_support_mode(sender: str, mode: str) -> None:
    """Remember which lane this Messenger user chose."""
    with SessionLocal() as db:
        client = resolve_or_create_client(db, messenger_psid=sender, channel="messenger")
        client.support_mode = mode
        db.commit()


def _profiling_intro(sender: str) -> str:
    first = ""
    name = get_user_name(sender, settings.FB_PAGE_TOKEN, settings.FB_GRAPH_VERSION)
    if name:
        first = " " + name.split()[0]
    return (
        f"Salut{first} ! 🌴 Parfait, on s'occupe de ça. Envoie-moi une capture "
        "d'écran 📸 du forfait que tu as trouvé (ou décris-le) — je te trouve le "
        "même voyage avec un rabais."
    )


def _handle_concierge_message(sender: str, text: str) -> None:
    """General-info lane: answer with the concierge AI, no trip profiling."""
    if not sender:
        return
    if not text:
        send_text(sender, "Avec plaisir ! 🌴 Pose-moi ta question.",
                  settings.FB_PAGE_TOKEN, settings.FB_GRAPH_VERSION)
        return
    answer = concierge_reply(text)
    reply = answer or ("Bonne question ! 🌴 Un conseiller pourra te détailler ça. "
                       "Veux-tu qu'on regarde un forfait pour toi ?")
    send_text(sender, reply, settings.FB_PAGE_TOKEN, settings.FB_GRAPH_VERSION)
    with SessionLocal() as db:
        cl = find_client_by_identity(db, "messenger_psid", sender)
        if cl:
            log_activity(db, cl.id, "message_in", f"Question générale : {text[:120]}")
            db.commit()
    log.info("Concierge reply to %s: %s", sender, "sent" if reply else "skipped")


def _conversation(case) -> list:
    """The message thread (in/out). Seeds inbound history from raw_message for
    cases created before structured threads existed."""
    if case.messages:
        return list(case.messages)
    parts = [p.strip() for p in (case.raw_message or "").split("\n---\n")
             if p.strip() and p.strip() != "(demande de support)"]
    at = case.created_at.isoformat(timespec="seconds") if case.created_at else ""
    return [{"dir": "in", "text": p, "at": at} for p in parts]


def _record_message(case_id: int, direction: str, text: str) -> None:
    """Append an in/out message to a case's conversation thread."""
    if not (case_id and text):
        return
    now = datetime.utcnow().isoformat(timespec="seconds")
    with SessionLocal() as db:
        c = db.get(Case, case_id)
        if c is not None:
            c.messages = _conversation(c) + [{"dir": direction, "text": text, "at": now}]
            db.commit()


def _render_thread(case, name: str = "Client") -> str:
    """Render the full in/out conversation as chat bubbles."""
    convo = _conversation(case)
    if not convo:
        return "<p class='muted'>Aucun message.</p>"
    bubbles = []
    for m in convo:
        out = m.get("dir") == "out"
        at = (m.get("at") or "").replace("T", " ")
        who = "Toi" if out else name
        bubbles.append(
            f"<div class='{'msg-out' if out else 'msg-in'}'>"
            f"{escape(m.get('text') or '')}"
            f"<div class='msg-at'>{escape(who)} · {escape(at)}</div></div>"
        )
    return "".join(bubbles)


_BOARD_FR = {"all_inclusive": "Tout inclus", "breakfast": "Petit-déjeuner",
             "half_board": "Demi-pension", "full_board": "Pension complète",
             "room_only": "Sans repas", "unknown": None}
_BASIS_FR = {"per_person": "par personne", "total": "pour le groupe", "unknown": None}


def _trip_info_cards(c, editable: bool = False) -> str:
    """Voyage / Voyageurs / Prix trouvé / Captures cards for a trip case. When
    editable, each card carries an inline edit form (toggled per voyagebox)."""
    t = c.trip or {}

    def val(x):
        if x in (None, "", [], "unknown"):
            return "<span class='muted'>\u2014</span>"
        return escape(str(x))

    def kv(label, value, sub=None):
        sub_html = f"<div class='sub'>{escape(sub)}</div>" if sub else ""
        return (f"<div class='kv'><span class='k'>{label}</span>"
                f"<span class='v'>{value}{sub_html}</span></div>")

    def inp(label, name, value, typ="text"):
        v = "" if value in (None, "") else escape(str(value))
        return (f"<label class='flbl'>{label}</label>"
                f"<input name='{name}' type='{typ}'" + (" step='any'" if typ == "number" else "")
                + f" value=\"{v}\">")

    def sel(label, name, value, options):
        o = "".join(f"<option value='{ov}'{' selected' if str(value or '') == str(ov) else ''}>{ol}</option>"
                    for ov, ol in options)
        return f"<label class='flbl'>{label}</label><select name='{name}'>{o}</select>"

    def card(title, view_rows, edit_rows=""):
        hdr = f"<h3 style='margin:0'>{title}</h3>"
        if editable and edit_rows:
            hdr = ("<div class='cardhdr2'>" + hdr
                   + f"<button type='button' class='editbtn' onclick='tripEdit({c.id},true)'>\u270f\ufe0f \u00c9diter</button></div>")
            return (f"<div class='card'>{hdr}<div class='cardv'>{view_rows}</div>"
                    f"<div class='carde'>{edit_rows}</div></div>")
        return f"<div class='card'>{hdr}{view_rows}</div>"

    city, iata = t.get("origin_city"), t.get("origin_airport_iata")
    origin = f"{city} ({iata})" if city and iata else (city or iata)
    dep, ret, nights = t.get("departure_date"), t.get("return_date"), t.get("nights")
    if dep or ret:
        dates = f"{dep or '?'} \u2192 {ret or '?'}" + (f" \u00b7 {nights} nuits" if nights else "")
    else:
        dates = t.get("dates_raw")
    hotel = t.get("hotel_name_raw")
    norm = t.get("hotel_name_normalized")
    hotel_sub = norm if norm and norm != hotel else None
    ages = [str(p.get("age")) for p in (t.get("passengers") or []) if p.get("age") is not None]
    ages_str = ", ".join(ages) if ages else None
    ps = t.get("price_seen") or {}
    price_amt = f"{ps.get('amount')} {ps.get('currency', 'CAD')}" if ps.get("amount") is not None else None
    taxes = ps.get("taxes_included")
    taxes_str = "Oui" if taxes is True else ("Non" if taxes is False else None)

    board_opts = [("", "\u2014")] + [(k, v) for k, v in _BOARD_FR.items() if k != "unknown"]
    basis_opts = [("", "\u2014"), ("per_person", "par personne"), ("total", "pour le groupe")]
    taxes_opts = [("", "\u2014"), ("true", "Oui"), ("false", "Non")]
    taxes_val = "true" if taxes is True else ("false" if taxes is False else "")

    voyage_view = (
        kv("Destination", val(t.get("destination")))
        + kv("H\u00f4tel", val(hotel), sub=hotel_sub)
        + kv("D\u00e9part", val(origin))
        + kv("Dates", val(dates))
        + kv("Forfait", val(_BOARD_FR.get(t.get("board"))))
        + kv("Transporteur", val(t.get("operator"))))
    voyage_edit = (
        inp("Destination", "destination", t.get("destination"))
        + inp("H\u00f4tel", "hotel_name_raw", hotel)
        + inp("Ville de d\u00e9part", "origin_city", city)
        + inp("A\u00e9roport (IATA)", "origin_airport_iata", iata)
        + inp("Date d\u00e9part", "departure_date", dep, "date")
        + inp("Date retour", "return_date", ret, "date")
        + inp("Nuits", "nights", nights, "number")
        + sel("Forfait", "board", t.get("board"), board_opts)
        + inp("Transporteur", "operator", t.get("operator")))

    voyageurs_view = (
        kv("Adultes", val(t.get("num_adults")))
        + kv("Enfants", val(t.get("num_children")))
        + kv("\u00c2ges", val(ages_str))
        + kv("Chambres", val(t.get("num_rooms")))
        + kv("Type de chambre", val(t.get("room_type"))))
    voyageurs_edit = (
        inp("Adultes", "num_adults", t.get("num_adults"), "number")
        + inp("Enfants", "num_children", t.get("num_children"), "number")
        + inp("Chambres", "num_rooms", t.get("num_rooms"), "number")
        + inp("Type de chambre", "room_type", t.get("room_type")))

    prix_view = (
        kv("Montant", val(price_amt))
        + kv("Base", val(_BASIS_FR.get(ps.get("basis"))))
        + kv("Taxes incluses", val(taxes_str))
        + kv("Source", val(t.get("source")))
        + kv("Texte original", val(ps.get("raw"))))
    prix_edit = (
        inp("Montant", "price_amount", ps.get("amount"), "number")
        + inp("Devise", "price_currency", ps.get("currency") or "CAD")
        + sel("Base", "price_basis", ps.get("basis"), basis_opts)
        + sel("Taxes incluses", "price_taxes", taxes_val, taxes_opts)
        + inp("Source", "source", t.get("source"))
        + inp("Texte original", "price_raw", ps.get("raw")))

    cards = (
        card("Voyage", voyage_view, voyage_edit)
        + card("Voyageurs", voyageurs_view, voyageurs_edit)
        + card("Prix trouv\u00e9", prix_view, prix_edit))

    shots = c.screenshots or []
    if shots:
        imgs = "".join(
                        f"<img src='/admin/cases/{c.id}/screenshot/{i}' alt='capture {i+1}' "
            f"onclick=\"lightbox('/admin/cases/{c.id}/screenshot/{i}')\" "
            "style='max-width:100%;max-height:460px;display:block;margin:0 auto 10px;cursor:zoom-in;"
            "border-radius:10px;border:1px solid var(--line);background:rgba(3,18,27,.4)'>"
            for i in range(len(shots)))
        cards += (f"<div class='card'><h3>Capture(s) d'\u00e9cran \u00b7 {len(shots)}</h3>{imgs}"
                  "<p class='sub'>Clique pour agrandir.</p></div>")
    return cards


def _trip_fulfillment_section(c, redirect: str) -> str:
    """Status-driven action area shown at the bottom of a trip box:
    needs_info -> missing fields; quoted -> quote URL + savings; booked/closed ->
    read-only summary (quote, savings, flights auto-captured from the trip)."""
    st = c.status
    t = c.trip or {}

    def link_row(label, url):
        return (f"<div class='kv'><span class='k'>{label}</span><span class='v'>"
                f"<a href=\"{escape(url)}\" target='_blank'>Ouvrir &rarr;</a></span></div>")

    def info_row(label, value):
        return (f"<div class='kv'><span class='k'>{label}</span>"
                f"<span class='v'>{escape(str(value))}</span></div>")

    # Flight dates are the trip's own dates (captured from the Voyage card).
    dep, ret, draw = t.get("departure_date"), t.get("return_date"), t.get("dates_raw")
    vols = (f"{dep or '?'} \u2192 {ret or '?'}") if (dep or ret) else (draw or None)

    if st == "needs_info":
        missing = c.needs_clarification or []
        if not missing:
            return ""
        chips = "".join(f"<span class='chip'>{escape(str(m))}</span>" for m in missing)
        return ("<div class='fbox'><div class='fhdr'>Infos manquantes pour coter</div>"
                f"<div class='chips'>{chips}</div></div>")

    if st == "quoted":
        cur, sav = c.quote_url or "", c.savings or ""
        shown = ""
        if cur:
            shown += link_row("Quote d\u00e9pos\u00e9e", cur) + info_row("Lien", cur)
        if sav:
            shown += info_row("\u00c9conomie", sav)
        return ("<div class='fbox'><div class='fhdr'>Quote &amp; \u00e9conomie</div>"
                f"{shown}"
                f"<form method='post' action='/admin/cases/{c.id}/quote'>"
                f"<input type='hidden' name='next' value=\"{escape(redirect)}\">"
                "<label class='flbl'>Lien de la quote</label>"
                f"<input name='quote_url' type='url' placeholder='https://\u2026' "
                f"value=\"{escape(cur)}\" style='width:100%'>"
                "<label class='flbl' style='margin-top:10px'>\u00c9conomie (rabais donn\u00e9 au client)</label>"
                f"<input name='savings' placeholder='ex. 195 $' value=\"{escape(sav)}\" style='width:100%'>"
                "<button style='margin-top:12px'>Enregistrer</button></form></div>")

    if st == "booked":
        items = []
        if c.quote_url:
            items.append(f"<span class='k'>Quote</span> <a href=\"{escape(c.quote_url)}\" "
                         "target='_blank'>Ouvrir &rarr;</a>")
        if c.savings:
            items.append(f"<span class='k'>Économie</span> {escape(c.savings)}")
        items.append(f"<span class='k'>Vols</span> {escape(vols or '—')}")
        line = "".join(f"<span class='fitem'>{it}</span>" for it in items)
        return (f"<div class='fline'>{line}<span class='fitem fnote' "
                "title='Dates de vol issues de la carte Voyage · passe à closed après le retour'>ⓘ</span></div>")

    if st == "closed":
        items = []
        if c.quote_url:
            items.append(f"<span class='k'>Quote</span> <a href=\"{escape(c.quote_url)}\" "
                         "target='_blank'>Ouvrir &rarr;</a>")
        if c.savings:
            items.append(f"<span class='k'>Économie</span> {escape(c.savings)}")
        items.append(f"<span class='k'>Vols</span> {escape(vols or '—')}")
        items.append("<span class='muted'>Voyage terminé · feedback à venir</span>")
        line = "".join(f"<span class='fitem'>{it}</span>" for it in items)
        return f"<div class='fline'>{line}</div>"

    return ""  # new


def _close_returned_trips(db) -> None:
    """Auto-close booked trips whose return flight date has passed (ISO strings
    sort lexicographically, so a plain string compare is correct here)."""
    today = datetime.utcnow().date().isoformat()
    rows = (db.query(Case)
            .filter(Case.kind == "trip", Case.status == "booked",
                    Case.flight_return.isnot(None), Case.flight_return != "",
                    Case.flight_return < today)
            .all())
    for r in rows:
        log_activity(db, r.client_id, "status_change",
                     f"Statut : booked → closed (retour de voyage)", r.id)
        r.status = "closed"
        r.awaiting_reply = False
    if rows:
        db.commit()


def _backfill_trip_contact() -> None:
    """Rescue trips stuck at needs_info only because their contact was missing,
    when the client already has an email/phone on file. Recomputes status so
    completed dossiers re-enter the 'new' priority queue."""
    with SessionLocal() as db:
        cases = db.query(Case).filter(Case.kind == "trip", Case.status == "needs_info").all()
        changed = 0
        for c in cases:
            cl = db.get(Client, c.client_id) if c.client_id else None
            if not cl:
                continue
            try:
                trip = TripRequest.model_validate(c.trip)
            except Exception:  # noqa: BLE001
                continue
            seeded = False
            if not trip.customer_email and cl.primary_email:
                trip.customer_email = cl.primary_email
                seeded = True
            if not trip.customer_phone and cl.primary_phone:
                trip.customer_phone = cl.primary_phone
                seeded = True
            if not seeded:
                continue
            rem = trip.remaining_fields()
            c.trip = trip.model_dump()
            c.needs_clarification = rem
            c.customer_email = trip.customer_email or c.customer_email
            c.customer_phone = trip.customer_phone or c.customer_phone
            if not rem:
                c.status = "new"
                log_activity(db, c.client_id, "status_change",
                             "Statut : needs_info → new (contact récupéré de la fiche client)", c.id)
            changed += 1
        if changed:
            db.commit()
            log.info("Backfilled contact on %d trips stuck at needs_info", changed)


def _migrate_screenshots_to_r2() -> None:
    """One-time (idempotent): move screenshots still stored as inline base64 into
    R2, keeping only the object key in the DB. No-op when R2 isn't configured."""
    if not storage.r2_enabled():
        return
    with SessionLocal() as db:
        cases = db.query(Case).all()
        moved = 0
        for c in cases:
            shots = c.screenshots or []
            if not any(s.get("b64") and not s.get("key") for s in shots):
                continue
            new_shots = []
            for s in shots:
                if s.get("b64") and not s.get("key"):
                    try:
                        data = base64.b64decode(s["b64"])
                        rec = storage.make_screenshot(data, s.get("media_type", "image/png"))
                        if rec.get("key"):                  # uploaded successfully
                            rec["received_at"] = s.get("received_at", rec["received_at"])
                            new_shots.append(rec)
                            moved += 1
                            continue
                    except Exception:  # noqa: BLE001 — keep the original on any error
                        pass
                new_shots.append(s)
            c.screenshots = new_shots
        if moved:
            db.commit()
            log.info("Migrated %d screenshots from base64 to R2", moved)


def _handle_human_message(sender: str, text: str, shots: list | None) -> None:
    """Human-support lane: NO AI. Store the message on a support case, record it
    in the conversation thread, flag it for the agent (bell), and stay silent."""
    if not sender:
        return
    text = (text or "").strip()
    now = datetime.utcnow().isoformat(timespec="seconds")
    with SessionLocal() as db:
        client = resolve_or_create_client(db, messenger_psid=sender, channel="messenger")
        if not client.display_name and settings.FB_PAGE_TOKEN:
            nm = get_user_name(sender, settings.FB_PAGE_TOKEN, settings.FB_GRAPH_VERSION)
            if nm:
                client.display_name = nm
        case = (db.query(Case)
                .filter(Case.client_id == client.id, Case.kind == "support",
                        Case.status != "resolved")
                .order_by(Case.created_at.desc()).first())
        if case is None:
            case = Case(
                client_id=client.id, channel="messenger", status="open",
                kind="support", sender_ref=sender,
                raw_message=text or "(demande de support)",
                trip={"customer_name": client.display_name} if client.display_name else {},
                needs_clarification=[], screenshots=shots or [],
                messages=([{"dir": "in", "text": text, "at": now}] if text else []),
            )
            db.add(case)
            db.flush()
            log_activity(db, client.id, "request_created",
                         "Demande de support (conseiller humain)", case.id)
        else:
            if text:
                case.messages = _conversation(case) + [{"dir": "in", "text": text, "at": now}]
                if case.raw_message in (None, "", "(demande de support)"):
                    case.raw_message = text
                else:
                    case.raw_message = case.raw_message + "\n---\n" + text
            if shots:
                case.screenshots = (case.screenshots or []) + shots
        case.awaiting_reply = True
        if text:
            log_activity(db, client.id, "message_in", "Message reçu (support humain)", case.id)
        db.commit()
    log.info("Human-support message stored for %s (no AI reply)", sender)


def process_postback(sender: str | None, payload: str) -> None:
    """Handle button / ice-breaker taps: set the conversation lane and reply."""
    if not (sender and settings.FB_PAGE_TOKEN):
        return
    if payload in (GET_STARTED_PAYLOAD, IB_PROFILING):
        _set_support_mode(sender, "profiling")
        sent = send_text(sender, _profiling_intro(sender),
                         settings.FB_PAGE_TOKEN, settings.FB_GRAPH_VERSION)
        log.info("Profiling intro to %s: %s", sender, "sent" if sent else "failed")
    elif payload == IB_CONCIERGE:
        _set_support_mode(sender, "concierge")
        send_text(sender,
                  "Avec plaisir ! 🌴 Pose-moi ta question (météo, destinations, "
                  "« tout inclus »…) et j'y réponds.",
                  settings.FB_PAGE_TOKEN, settings.FB_GRAPH_VERSION)
        log.info("Concierge lane set for %s", sender)
    elif payload == IB_HUMAN:
        _set_support_mode(sender, "human")
        _handle_human_message(sender, "", None)
        send_text(sender,
                  "Parfait 🙏 Un conseiller va te répondre directement ici. "
                  "Écris-nous ta question, on revient vite !",
                  settings.FB_PAGE_TOKEN, settings.FB_GRAPH_VERSION)
        log.info("Human-support lane set for %s", sender)


@app.get("/webhook")
def webhook_verify(request: Request):
    p = request.query_params
    challenge = verify_challenge(
        p.get("hub.mode"), p.get("hub.verify_token"), p.get("hub.challenge"),
        expected_token=settings.FB_VERIFY_TOKEN,
    )
    if challenge is not None:
        return PlainTextResponse(challenge)
    return PlainTextResponse("Verification failed", status_code=403)


@app.post("/webhook")
async def webhook_receive(request: Request, background: BackgroundTasks):
    raw = await request.body()
    if not valid_signature(raw, request.headers.get("X-Hub-Signature-256"),
                           settings.FB_APP_SECRET):
        return PlainTextResponse("Bad signature", status_code=403)

    payload = await request.json()
    # Messenger events have object == "page"; ignore anything else but still 200.
    if payload.get("object") != "page":
        return PlainTextResponse("EVENT_RECEIVED")
    for sender, text, image_urls in extract_messages(payload):
        background.add_task(process_messenger_message, sender, text, image_urls)
    for sender, pb_payload in extract_postbacks(payload):
        background.add_task(process_postback, sender, pb_payload)
    for sender, qr_payload in extract_quick_replies(payload):
        background.add_task(process_postback, sender, qr_payload)

    # ACK immediately; parsing happens after the response is sent.
    return PlainTextResponse("EVENT_RECEIVED")


@app.post("/intake")
def intake_form(trip: TripRequest):
    """The Netlify form posts a TripRequest JSON body here."""
    trip.source = trip.source or "form"
    if trip.parse_confidence == 0.0:
        trip.parse_confidence = 1.0  # human-entered data is trusted
    # Web-form customers gave their email and aren't on Messenger -> default to email.
    if trip.preferred_channel == ContactChannel.unknown and trip.customer_email:
        trip.preferred_channel = ContactChannel.email
    case_id = store_case("form", trip)
    return {"ok": True, "case_id": case_id,
            "searchable": trip.is_searchable(),
            "needs": trip.remaining_fields()}


def _trip_from_form(email, name, origin_city, origin_airport_iata, where,
                    dep, ret, adults, children, operator, notes, price, basis) -> TripRequest:
    from trip_schema import PriceBasis, PriceSeen
    price_seen = None
    if price is not None:
        try:
            price_seen = PriceSeen(amount=float(price),
                                   basis=PriceBasis(basis) if basis else PriceBasis.unknown)
        except Exception:  # noqa: BLE001
            price_seen = None
    return TripRequest(
        customer_email=email or None,
        customer_name=name or None,
        origin_city=origin_city or None,
        origin_airport_iata=origin_airport_iata or None,
        hotel_name_raw=where or None,
        departure_date=dep or None,
        return_date=ret or None,
        num_adults=adults,
        num_children=children,
        operator=operator or None,
        agent_notes=notes or None,
        price_seen=price_seen,
    )


@app.post("/parse/screenshot")
async def parse_screenshot(file: UploadFile = File(...)):
    """Read a deal image and return the extracted fields WITHOUT saving anything.
    The web form calls this on upload so the customer can review/edit before sending."""
    img_bytes = await file.read()
    media = file.content_type or "image/png"
    try:
        trip = parse_trip("(capture web)", images=[(img_bytes, media)])
    except Exception as e:  # noqa: BLE001
        log.exception("Screenshot parse failed: %s", e)
        return {"ok": False, "error": "parse_failed", "trip": {}}
    return {"ok": True, "trip": trip.model_dump()}


@app.post("/intake/screenshot")
async def intake_screenshot(
    file: UploadFile = File(...),
    parse: bool = Form(True),
    email: str | None = Form(None),
    name: str | None = Form(None),
    origin_city: str | None = Form(None),
    origin_airport_iata: str | None = Form(None),
    where: str | None = Form(None),
    dep: str | None = Form(None),
    ret: str | None = Form(None),
    adults: int | None = Form(None),
    children: int | None = Form(None),
    operator: str | None = Form(None),
    notes: str | None = Form(None),
    price: float | None = Form(None),
    basis: str | None = Form(None),
):
    """Stores a web submission that came with a deal screenshot. The image is
    saved on the case (like Messenger). If parse=False the form fields were
    already reviewed by the customer, so we trust them and skip the AI call."""
    img_bytes = await file.read()
    media = file.content_type or "image/png"

    if parse:
        try:
            trip = parse_trip("(capture web)", images=[(img_bytes, media)])
        except Exception as e:  # noqa: BLE001 — never lose a submission
            log.exception("Screenshot parse failed: %s", e)
            trip = TripRequest(raw_message="(capture web)",
                               agent_notes=f"Analyse automatique échouée: {e}")
    else:
        trip = TripRequest(raw_message="(capture web — révisé par le client)")

    manual = _trip_from_form(email, name, origin_city, origin_airport_iata, where,
                             dep, ret, adults, children, operator, notes, price, basis)
    trip = merge_trip_requests(trip, manual)
    trip.source = "capture web"
    if trip.preferred_channel == ContactChannel.unknown and trip.customer_email:
        trip.preferred_channel = ContactChannel.email

    shots = [storage.make_screenshot(img_bytes, media)]
    case_id = store_case("form", trip, shots=shots)
    return {"ok": True, "case_id": case_id,
            "trip": trip.model_dump(), "needs": trip.remaining_fields()}


# --------------------------------------------------------------------------- #
# Admin panel (Basic auth)
# --------------------------------------------------------------------------- #
_PAGE = """<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Du Voyageur — Dossiers</title>
<link rel="icon" type="image/png" href="/static/logo.png">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,700;12..96,800&family=Inter:wght@400;500;600&family=Space+Grotesk:wght@500;700&display=swap" rel="stylesheet">
<style>
 :root{{
   --abyss:#03121b;--abyss-2:#06212f;--deep:#0a3346;--pacific:#19d3e6;--lagoon:#3df0c5;
   --surf:#9bf6ec;--gold:#ffd23f;--foam:#eafcff;--mist:#94b8c6;
   --line:rgba(155,246,236,.16);--glass:linear-gradient(180deg,rgba(20,62,82,.5),rgba(8,33,47,.62));
   --glow:rgba(25,211,230,.5);
 }}
 *{{box-sizing:border-box}}
 body{{margin:0;font-family:"Inter",system-ui,sans-serif;font-size:15px;line-height:1.6;color:var(--foam);
   background:
     radial-gradient(80% 50% at 85% -8%, rgba(25,211,230,.10), transparent 60%),
     radial-gradient(70% 50% at 0% 108%, rgba(61,240,197,.08), transparent 60%),
     var(--abyss);
   background-attachment:fixed;-webkit-font-smoothing:antialiased}}
 a{{color:var(--surf);text-decoration:none}} a:hover{{text-decoration:underline}}
 header{{position:sticky;top:0;z-index:10;background:rgba(6,33,47,.82);backdrop-filter:blur(12px);
   -webkit-backdrop-filter:blur(12px);border-bottom:1px solid var(--line)}}
 .topbar{{display:flex;justify-content:space-between;align-items:center;gap:14px;flex-wrap:wrap;padding:11px 20px}}
 .actions{{display:flex;align-items:center;gap:9px;flex-wrap:wrap}}
 .iform{{margin:0;display:inline}}
 .act{{display:inline-flex;align-items:center;gap:6px;font-family:"Space Grotesk",monospace;font-size:12.5px;
   font-weight:600;padding:7px 12px;border-radius:10px;border:1px solid var(--line);background:rgba(3,18,27,.4);
   color:var(--foam);cursor:pointer;box-shadow:none;transition:border-color .15s,background .15s}}
 .act:hover{{text-decoration:none;background:rgba(25,211,230,.09);transform:none;box-shadow:none}}
 .act-frontend{{border-color:rgba(61,240,197,.45);color:#a9f7e2}}
 .act-client{{border-color:rgba(167,139,250,.45);color:#d6c9ff}}
 .act-danger{{border-color:rgba(224,103,91,.5);color:#ffb3aa}} .act-danger:hover{{background:rgba(224,103,91,.13)}}
 .searchbox{{margin:0}}
 .searchbox input{{width:230px;max-width:42vw;padding:7px 12px;font-size:13px;border-radius:10px}}
 .searchbox input::placeholder{{color:var(--mist)}}
 .navrow{{display:flex;gap:2px;flex-wrap:wrap;padding:0 14px;border-top:1px solid var(--line)}}
 .navtab{{display:inline-flex;align-items:center;gap:7px;padding:12px 14px;color:var(--mist);font-size:13.5px;
   font-weight:600;border-bottom:2px solid transparent}}
 .navtab:hover{{color:var(--foam);text-decoration:none}}
 .navtab.active{{color:var(--foam);border-bottom-color:var(--pacific)}}
 .navtab .n{{font-family:"Space Grotesk",monospace;font-size:11px;padding:1px 7px;border-radius:999px;
   background:rgba(25,211,230,.18);color:var(--surf)}}
 .btn-ghost{{background:rgba(3,18,27,.4);border:1px solid var(--line);color:var(--foam);box-shadow:none;
   font-family:"Space Grotesk",monospace;font-size:13px;font-weight:600;padding:8px 14px}}
 .btn-ghost:hover{{background:rgba(25,211,230,.1);transform:none;box-shadow:none}}
 .pagehdr{{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:8px;flex-wrap:wrap}}
 .brand{{display:flex;align-items:center;gap:11px}}
 .brand img{{width:34px;height:34px;border-radius:50%;box-shadow:0 0 0 1px var(--line)}}
 h1{{font-family:"Bricolage Grotesque",sans-serif;font-weight:800;font-size:18px;letter-spacing:-.02em;margin:0}}
 h2{{font-family:"Bricolage Grotesque",sans-serif;font-weight:700;font-size:22px;letter-spacing:-.02em;margin:6px 0 4px}}
 .logout{{color:var(--mist);font-size:13px;font-family:"Space Grotesk",monospace}} .logout:hover{{color:var(--foam);text-decoration:none}}
 .topnav{{display:flex;gap:6px}}
 .topnav a{{color:var(--mist);font-size:13.5px;font-weight:600;padding:6px 13px;border-radius:999px}}
 .topnav a:hover{{color:var(--foam);text-decoration:none;background:rgba(25,211,230,.1)}}
 .hdr-right{{display:flex;align-items:center;gap:14px}}
 .bell{{position:relative}}
 .bell-btn{{background:transparent;border:0;cursor:pointer;font-size:20px;line-height:1;padding:5px;
   color:var(--foam);box-shadow:none;border-radius:9px}}
 .bell-btn:hover{{transform:none;box-shadow:none;background:rgba(25,211,230,.12)}}
 .bell-badge{{position:absolute;top:-1px;right:-3px;min-width:17px;height:17px;padding:0 4px;
   border-radius:999px;background:#e0675b;color:#fff;font:700 11px/17px "Space Grotesk",monospace;text-align:center}}
 .bell-panel{{position:absolute;right:0;top:44px;width:320px;max-width:82vw;z-index:50;display:none;
   background:rgba(6,33,47,.98);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);
   border:1px solid var(--line);border-radius:14px;box-shadow:0 22px 54px -16px rgba(0,0,0,.85);overflow:hidden}}
 .bell-panel.open{{display:block}}
 .bell-head{{padding:11px 14px;font-family:"Space Grotesk",monospace;font-size:12px;letter-spacing:.1em;
   text-transform:uppercase;color:var(--pacific);border-bottom:1px solid var(--line)}}
 .bell-item{{display:block;padding:11px 14px;border-bottom:1px solid var(--line);color:var(--foam)}}
 .bell-item:last-child{{border-bottom:0}}
 .bell-item:hover{{background:rgba(25,211,230,.08);text-decoration:none}}
 .bell-empty{{padding:18px 14px;color:var(--mist);font-size:13px}}
 .bell-name{{font-weight:600}}
 .bell-meta{{display:flex;gap:8px;align-items:center;margin-top:4px;font-size:12px;color:var(--mist)}}
 main{{padding:22px 26px;max-width:none}}
 table{{width:100%;border-collapse:collapse;font-size:14px;background:rgba(6,33,47,.4);
   border:1px solid var(--line);border-radius:14px;overflow:hidden}}
 th,td{{text-align:left;padding:11px 14px;border-bottom:1px solid var(--line);vertical-align:top}}
 tr:last-child td{{border-bottom:0}} tbody tr:hover td,table tr:hover td{{background:rgba(25,211,230,.05)}}
 th{{font-family:"Space Grotesk",monospace;font-size:12px;letter-spacing:.06em;text-transform:uppercase;
   color:var(--mist);font-weight:500}}
 .tag{{display:inline-block;padding:3px 10px;border-radius:999px;font-size:12px;font-weight:600;
   border:1px solid var(--line);background:rgba(148,184,198,.12);color:var(--foam)}}
 .new{{background:rgba(25,211,230,.16);border-color:rgba(25,211,230,.4);color:#bff3fb}}
 .needs_info{{background:rgba(255,210,63,.15);border-color:rgba(255,210,63,.4);color:#ffe08a}}
 .booked{{background:rgba(61,240,197,.15);border-color:rgba(61,240,197,.4);color:#a9f7e2}}
 .quoted{{background:rgba(167,139,250,.18);border-color:rgba(167,139,250,.4);color:#d6c9ff}}
 .closed{{background:rgba(148,184,198,.12);border-color:var(--line);color:var(--mist)}}
 .tag.open{{background:rgba(25,211,230,.16);border-color:rgba(25,211,230,.4);color:#bff3fb}}
 .tag.resolved{{background:rgba(61,240,197,.15);border-color:rgba(61,240,197,.4);color:#a9f7e2}}
 .svc tr:hover td{{background:rgba(25,211,230,.07)}}
 .modal-ov{{position:fixed;inset:0;background:rgba(2,11,16,.74);display:none;z-index:80;
   align-items:center;justify-content:center;padding:24px}}
 .modal-ov.open{{display:flex}}
 .modal{{background:var(--abyss-2);border:1px solid var(--line);border-radius:16px;max-width:700px;
   width:100%;max-height:86vh;display:flex;flex-direction:column;box-shadow:0 24px 64px rgba(0,0,0,.6)}}
 .modal-hd{{display:flex;align-items:center;justify-content:space-between;gap:12px;
   padding:14px 18px;border-bottom:1px solid var(--line)}}
 .modal-bd{{padding:16px 18px;overflow-y:auto}}
 .modal-ft{{padding:14px 18px;border-top:1px solid var(--line)}}
 .modal-x{{background:rgba(3,18,27,.4);border:1px solid var(--line);color:var(--foam);
   box-shadow:none;border-radius:9px;padding:6px 11px;cursor:pointer;font-size:14px}}
 .lightbox{{position:fixed;inset:0;background:rgba(2,11,16,.85);display:none;z-index:120;
   align-items:center;justify-content:center;padding:30px}}
 .lightbox.open{{display:flex}}
 .lightbox img{{max-width:95vw;max-height:90vh;border-radius:12px;border:1px solid var(--line);
   box-shadow:0 24px 64px rgba(0,0,0,.6)}}
 .lightbox .lb-x{{position:absolute;top:18px;right:22px;background:rgba(3,18,27,.6);
   border:1px solid var(--line);color:var(--foam);border-radius:9px;padding:8px 13px;cursor:pointer;font-size:16px}}
 .muted{{color:var(--mist)}} code{{background:rgba(3,18,27,.6);padding:1px 6px;border-radius:5px}}
 .grid2{{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:16px;margin:18px 0}}
 .idtab{{display:grid;grid-template-columns:1fr 1fr;gap:16px;align-items:stretch;margin:18px 0}}
 @media(max-width:900px){{.idtab{{grid-template-columns:1fr}}}}
 .idtab>.col{{display:flex;flex-direction:column;gap:16px}}
 .voyagebox{{border:1px solid var(--line);border-radius:16px;padding:18px 20px;margin:0 0 22px;background:rgba(255,255,255,.012)}}
 .voyagebox .grid2{{margin:14px 0}}
 .cardhdr2{{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:8px}}
 .cardhdr2 h3{{margin:0}}
 .voyagebox .carde{{display:none}}
 .voyagebox.editing .cardv{{display:none}}
 .voyagebox.editing .carde{{display:block}}
 .carde .flbl{{display:block;margin-top:8px}}
 .carde input,.carde select{{width:100%}}
 .savebar{{display:none;margin-top:14px;gap:10px}}
 .voyagebox.editing .savebar{{display:flex}}
 .voyagebox.editing .editbtn{{display:none}}
 .fbox{{margin-top:8px;padding:16px 18px;border:1px solid var(--line);border-radius:12px;background:rgba(6,28,40,.45)}}
 .fhdr{{font-family:"Space Grotesk",monospace;font-size:12px;letter-spacing:.1em;text-transform:uppercase;color:var(--pacific);margin-bottom:10px}}
 .fline{{display:flex;flex-wrap:wrap;align-items:baseline;gap:6px 22px;padding:11px 16px;margin-top:8px;
   border:1px solid var(--line);border-radius:12px;background:rgba(6,28,40,.45)}}
 .fline .k{{color:var(--mist);font-size:13px;margin-right:6px}}
 .fnote{{color:var(--mist);cursor:help;margin-left:auto}}
 .act-card{{position:relative;padding:0;min-height:320px}}
 .act-inner{{position:absolute;inset:0;display:flex;flex-direction:column;padding:18px 20px}}
 .act-inner>h3{{margin:0 0 12px}}
 .act-scroll{{flex:1;min-height:0;overflow-y:auto;padding-right:6px}}
 .cardhdr{{display:flex;align-items:center;gap:12px;margin-bottom:14px}}
 .eyebrow{{font-family:"Space Grotesk",monospace;font-size:12px;letter-spacing:.12em;text-transform:uppercase;color:var(--pacific)}}
 .editbtn{{padding:6px 12px;font-size:12px;border-radius:9px}}
 .btn-ghost{{background:transparent;border:1px solid var(--line);color:var(--mist);box-shadow:none}}
 .card{{background:var(--glass);border:1px solid var(--line);border-radius:16px;padding:16px 18px}}
 .card h3{{margin:0 0 12px;font-family:"Space Grotesk",monospace;font-size:12px;font-weight:700;
   letter-spacing:.14em;text-transform:uppercase;color:var(--pacific)}}
 .kv{{display:flex;justify-content:space-between;gap:14px;padding:7px 0;border-bottom:1px solid var(--line)}}
 .kv:last-child{{border-bottom:0}} .kv .k{{color:var(--mist)}} .kv .v{{text-align:right;font-weight:500;color:var(--foam)}}
 .sub{{font-size:12px;color:var(--mist);font-weight:400}}
 .chips{{display:flex;flex-wrap:wrap;gap:8px;margin-top:6px}}
 .chip{{background:rgba(255,210,63,.14);border:1px solid rgba(255,210,63,.32);color:#ffe0a8;
   padding:3px 11px;border-radius:999px;font-size:13px}}
 .ok{{color:var(--lagoon);font-weight:600}}
 .meter{{height:10px;background:rgba(3,18,27,.6);border:1px solid var(--line);border-radius:999px;overflow:hidden;margin:8px 0}}
 .meter > i{{display:block;height:100%;border-radius:999px}}
 .full{{grid-column:1 / -1}}
 select,input,textarea{{font-size:14px;font-family:inherit;padding:9px 11px;border-radius:10px;
   border:1px solid var(--line);background:rgba(3,18,27,.55);color:var(--foam)}}
 select:focus,input:focus,textarea:focus{{outline:none;border-color:var(--pacific);box-shadow:0 0 0 3px rgba(25,211,230,.16)}}
 button,.btn{{font-family:"Bricolage Grotesque",sans-serif;font-size:14px;font-weight:700;padding:9px 16px;
   border-radius:999px;border:0;cursor:pointer;color:#02161c;
   background:linear-gradient(120deg,var(--pacific),var(--lagoon));
   box-shadow:0 10px 24px -12px var(--glow);transition:transform .15s,box-shadow .15s}}
 button:hover,.btn:hover{{transform:translateY(-2px);box-shadow:0 16px 30px -12px var(--glow);text-decoration:none}}
 .btn-danger{{background:linear-gradient(120deg,#e0675b,#b23a30);color:#fff}}
 details summary{{cursor:pointer;color:var(--mist);margin-top:18px}}
 pre{{background:rgba(3,18,27,.6);border:1px solid var(--line);border-radius:12px;padding:12px;overflow:auto;color:var(--surf)}}
 tr[data-href]{{cursor:pointer}}
 .tabs{{display:flex;flex-wrap:wrap;gap:8px;margin:2px 0 18px}}
 .tab{{display:inline-flex;align-items:center;gap:7px;padding:7px 14px;border-radius:999px;
   border:1px solid var(--line);background:rgba(6,33,47,.5);color:var(--mist);font-size:13.5px;
   font-weight:600;text-decoration:none}}
 .tab:hover{{color:var(--foam);text-decoration:none;border-color:rgba(25,211,230,.35)}}
 .tab.active{{background:linear-gradient(120deg,rgba(25,211,230,.22),rgba(61,240,197,.18));
   border-color:rgba(25,211,230,.5);color:var(--foam)}}
 .tab-n{{font-family:"Space Grotesk",monospace;font-size:11px;padding:1px 7px;border-radius:999px;
   background:rgba(3,18,27,.5);color:var(--mist)}}
 .tab.active .tab-n{{background:rgba(3,18,27,.45);color:var(--surf)}}
 .tl{{display:flex;gap:12px;padding:11px 0;border-bottom:1px solid var(--line)}}
 .tl:last-child{{border-bottom:0}}
 .tl-dot{{flex:none;width:30px;height:30px;border-radius:50%;display:grid;place-items:center;
   background:rgba(3,18,27,.6);border:1px solid var(--line);font-size:14px}}
 .tl-body{{flex:1;min-width:0}}
 .tl-top{{display:flex;align-items:baseline;gap:8px;flex-wrap:wrap}}
 .tl-at{{margin-left:auto;font-family:"Space Grotesk",monospace;font-size:12px;color:var(--mist)}}
 .flbl{{display:block;font-size:12px;color:var(--mist);margin:10px 0 5px}}
 .card form input,.card form textarea{{width:100%}}
 .stats{{display:flex;flex-wrap:wrap;gap:12px;margin:6px 0 0}}
 .stat{{background:var(--glass);border:1px solid var(--line);border-radius:14px;padding:12px 18px;min-width:130px}}
 a.stat{{text-decoration:none;color:inherit;display:block;transition:border-color .15s,transform .15s}}
 a.stat:hover{{border-color:var(--pacific);transform:translateY(-1px)}}
 .stat-n{{font-family:"Bricolage Grotesque",sans-serif;font-weight:800;font-size:1.6rem;color:var(--foam);line-height:1}}
 .stat-l{{font-family:"Space Grotesk",monospace;font-size:11px;letter-spacing:.1em;text-transform:uppercase;color:var(--mist);margin-top:5px}}
 .msg-in{{background:rgba(3,18,27,.5);border:1px solid var(--line);border-radius:14px 14px 14px 4px;
   padding:11px 14px;margin:0 auto 10px 0;white-space:pre-wrap;max-width:85%;line-height:1.5}}
 .msg-out{{background:linear-gradient(120deg,rgba(25,211,230,.16),rgba(61,240,197,.13));
   border:1px solid rgba(25,211,230,.32);border-radius:14px 14px 4px 14px;padding:11px 14px;
   margin:0 0 10px auto;white-space:pre-wrap;max-width:85%;line-height:1.5}}
 .msg-at{{font-family:"Space Grotesk",monospace;font-size:10.5px;color:var(--mist);margin-top:6px;opacity:.85}}
</style></head><body>
<header>
 <div class="topbar">
  <span class="brand"><img src="/static/logo.png" alt=""><h1>Du Voyageur</h1></span>
  <div class="actions">
   <a class="act act-frontend" href="https://duvoyageur.netlify.app" target="_blank" rel="noopener">🌐 Frontend</a>
   <a class="act act-client" href="/admin/espace-client">👤 Espace client</a>
   <form class="searchbox" method="get" action="/admin/search" role="search">
    <input name="q" placeholder="Rechercher client, réf, courriel…" autocomplete="off">
   </form>
   <form method="post" action="/admin/reset" class="iform"
         onsubmit="return confirm('Vider TOUS les dossiers (clients, demandes, historique) ? Action irréversible.')">
    <button class="act act-danger" type="submit">🗑 Vider les dossiers</button>
   </form>
   {bell}
   <a class="logout" href="/admin/logout">Déconnexion</a>
  </div>
 </div>
 <nav class="navrow">{nav}</nav>
</header>
<main>{body}</main>
<script>
document.addEventListener('click',function(e){{
 var tr=e.target.closest('tr[data-href]');
 if(!tr||e.target.closest('a'))return;
 window.location=tr.getAttribute('data-href');
}});
(function(){{
 var b=document.getElementById('bellBtn');
 if(!b)return;
 b.addEventListener('click',function(e){{
  e.stopPropagation();
  document.getElementById('bellPanel').classList.toggle('open');
 }});
 document.addEventListener('click',function(e){{
  var p=document.getElementById('bellPanel');
  if(p&&!e.target.closest('.bell'))p.classList.remove('open');
 }});
}})();
function lightbox(src){{
 var o=document.getElementById('lightbox');
 if(!o)return;
 o.querySelector('img').src=src;
 o.classList.add('open');
}}
document.addEventListener('click',function(e){{
 var o=document.getElementById('lightbox');
 if(o&&(e.target.id==='lightbox'||e.target.classList.contains('lb-x')))o.classList.remove('open');
}});
document.addEventListener('keydown',function(e){{
 if(e.key==='Escape'){{var o=document.getElementById('lightbox');if(o)o.classList.remove('open');}}
}});
</script>
<div id="lightbox" class="lightbox"><button type="button" class="lb-x">\u2715</button><img src="" alt="capture"></div>
</body></html>"""


def _bell_html() -> str:
    """Notification bell: requests still awaiting our reply, any channel."""
    with SessionLocal() as db:
        q = db.query(Case).filter(Case.awaiting_reply.is_(True), Case.status.notin_(("closed", "resolved")))
        total = q.count()
        pend = q.order_by(Case.created_at.desc()).limit(15).all()
        rows = []
        for c in pend:
            t = c.trip or {}
            nm = escape(str(t.get("customer_name") or "Client inconnu"))
            where = escape(str(t.get("hotel_name_raw") or t.get("destination") or "—"))
            rows.append(
                f"<a class='bell-item' href='/admin/cases/{c.id}'>"
                f"<div class='bell-name'>{nm}</div>"
                f"<div class='bell-meta'><span class='tag {c.status}'>{c.status}</span>"
                f"<span>{where}</span></div></a>"
            )
    badge = (f"<span class='bell-badge'>{total if total < 100 else '99+'}</span>"
             if total else "")
    head = f"<div class='bell-head'>À répondre · {total}</div>"
    items = "".join(rows) or "<div class='bell-empty'>Rien à traiter pour l'instant 🎉</div>"
    return (
        "<div class='bell'>"
        f"<button class='bell-btn' id='bellBtn' aria-label='Notifications'>🔔{badge}</button>"
        f"<div class='bell-panel' id='bellPanel'>{head}{items}</div></div>"
    )


def _nav_html(active: str = "") -> str:
    """Top navigation row, travel-domain sections, with a live count on the
    new-requests queue."""
    with SessionLocal() as db:
        n_new = db.query(Case).filter(Case.kind == "trip", Case.status == "new").count()
        n_svc = db.query(Case).filter(Case.kind == "support", Case.awaiting_reply.is_(True),
                                      Case.status.notin_(("closed", "resolved"))).count()
    items = [
        ("queue", "Nouvelle demande de voyage", "/admin/cases?status=new", n_new),
        ("queue_service", "Nouvelle demande de service client",
         "/admin/cases?status=service", n_svc),
        ("cases", "Demandes", "/admin/cases", None),
        ("clients", "Clients", "/admin/clients", None),
        ("traveling", "Clients en voyage", "/admin/cases?status=booked", None),
        ("completed", "Voyages complétés", "/admin/cases?status=closed", None),
        ("health", "System Health", "/admin/system", None),
        ("config", "System Config", "/admin/config", None),
        ("reports", "Reports", "/admin/reports", None),
    ]
    out = []
    for key, label, href, badge in items:
        cls = "navtab active" if key == active else "navtab"
        b = f"<span class='n'>{badge}</span>" if badge is not None else ""
        out.append(f"<a class='{cls}' href='{href}'>{label}{b}</a>")
    return "".join(out)


def page_header(title: str, refresh_url: str | None = None) -> str:
    """A page title row, optionally with a Refresh button (à la System Health)."""
    btn = (f"<a class='btn-ghost' href='{refresh_url}'>&#8635; Rafraîchir</a>"
           if refresh_url else "")
    return f"<div class='pagehdr'><h2>{escape(title)}</h2>{btn}</div>"


def render_page(body: str, active: str = "") -> str:
    """Render an admin page in the shell (top nav + notification bell)."""
    return _PAGE.format(body=body, bell=_bell_html(), nav=_nav_html(active))


_LOGIN_PAGE = """<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Du Voyageur — Connexion</title>
<link rel="icon" type="image/png" href="/static/logo.png">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,700;12..96,800&family=Inter:wght@400;500;600&family=Space+Grotesk:wght@500&display=swap" rel="stylesheet">
<style>
 :root{{
   --abyss:#03121b;--deep:#0a3346;--pacific:#19d3e6;--lagoon:#3df0c5;
   --surf:#9bf6ec;--gold:#ffd23f;--foam:#eafcff;--mist:#94b8c6;
   --line:rgba(155,246,236,.16);--glow:rgba(25,211,230,.55);
 }}
 *{{box-sizing:border-box}}
 body{{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
   padding:24px;font-family:"Inter",system-ui,sans-serif;color:var(--foam);
   background:
     radial-gradient(80% 55% at 80% -5%, rgba(25,211,230,.18), transparent 60%),
     linear-gradient(180deg, rgba(3,18,27,.78), rgba(3,18,27,.88)),
     url("/static/login-bg.webp") center/cover fixed no-repeat;}}
 .card{{width:100%;max-width:380px;text-align:center;
   background:linear-gradient(180deg, rgba(20,62,82,.55), rgba(8,33,47,.66));
   border:1px solid var(--line);border-radius:22px;padding:34px 30px 30px;
   box-shadow:0 30px 70px -24px rgba(0,0,0,.8),0 0 40px -12px var(--glow);
   backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px)}}
 .logo{{width:96px;height:96px;border-radius:50%;display:block;margin:0 auto 14px;
   box-shadow:0 0 0 1px var(--line),0 10px 30px -8px rgba(0,0,0,.7)}}
 h1{{font-family:"Bricolage Grotesque",sans-serif;font-weight:800;font-size:24px;
   letter-spacing:-.02em;margin:0}}
 .sub{{font-family:"Space Grotesk",monospace;text-transform:uppercase;letter-spacing:.22em;
   font-size:11px;color:var(--pacific);margin:6px 0 22px}}
 label{{display:block;text-align:left;font-size:12px;color:var(--mist);margin:14px 0 6px}}
 input{{width:100%;font-size:15px;padding:12px 13px;border-radius:12px;color:var(--foam);
   border:1px solid var(--line);background:rgba(3,18,27,.55)}}
 input::placeholder{{color:#6f93a3}}
 input:focus{{outline:none;border-color:var(--pacific);box-shadow:0 0 0 3px rgba(25,211,230,.18)}}
 button{{width:100%;margin-top:24px;font-family:"Bricolage Grotesque",sans-serif;font-weight:700;
   font-size:15px;padding:13px;border-radius:999px;border:0;cursor:pointer;color:#02161c;
   background:linear-gradient(120deg,var(--pacific),var(--lagoon));
   box-shadow:0 12px 30px -10px var(--glow);transition:transform .15s,box-shadow .15s}}
 button:hover{{transform:translateY(-2px);box-shadow:0 18px 40px -10px var(--glow)}}
 .err{{background:rgba(95,29,29,.5);border:1px solid rgba(255,120,120,.4);color:#ffc1c1;
   font-size:13px;padding:10px 12px;border-radius:12px;margin-bottom:16px}}
 .foot{{margin-top:20px;font-size:11px;color:var(--mist);opacity:.8}}
</style></head><body>
 <form class="card" method="post" action="/admin/login">
   <img class="logo" src="/static/logo.png" alt="Du Voyageur">
   <h1>Du Voyageur</h1>
   <p class="sub">Espace administrateur</p>
   {error}
   <label for="u">Identifiant</label>
   <input id="u" name="username" autocomplete="username" autofocus required>
   <label for="p">Mot de passe</label>
   <input id="p" name="password" type="password" autocomplete="current-password" required>
   <button type="submit">Se connecter</button>
   <p class="foot">Permis d'agence 700495</p>
 </form>
</body></html>"""

_LOGIN_ERR = "<div class='err'>Identifiant ou mot de passe invalide.</div>"


@app.get("/admin")
def admin_root(request: Request):
    if request.session.get("admin"):
        return RedirectResponse("/admin/cases", status_code=303)
    return RedirectResponse("/admin/login", status_code=303)


@app.get("/admin/login", response_class=HTMLResponse)
def admin_login_form(request: Request, error: int = 0):
    if request.session.get("admin"):
        return RedirectResponse("/admin/cases", status_code=303)
    return _LOGIN_PAGE.format(error=_LOGIN_ERR if error else "")


@app.post("/admin/login")
def admin_login(request: Request, username: str = Form(""), password: str = Form("")):
    if check_credentials(username, password):
        request.session["admin"] = True
        request.session["user"] = username
        return RedirectResponse("/admin/cases", status_code=303)
    return RedirectResponse("/admin/login?error=1", status_code=303)


@app.get("/admin/logout")
def admin_logout(request: Request):
    request.session.clear()
    return RedirectResponse("/admin/login", status_code=303)


@app.get("/admin/cases", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
def admin_cases(status: str = "all", view: str = "voyage"):
    # Top-nav travel sections (status filters, trip only).
    SECTION = {"new": "queue", "service": "queue_service",
               "booked": "traveling", "closed": "completed"}
    SEC_TITLE = {"queue": "Nouvelle demande de voyage",
                 "queue_service": "Nouvelle demande de service client",
                 "traveling": "Clients en voyage", "completed": "Voyages complétés"}
    nav_active = SECTION.get(status, "cases")

    def next_step(c) -> str:
        """A short human next action, derived from kind + status + missing info."""
        if (c.kind or "trip") == "support":
            return "Répondre au client" if c.awaiting_reply else "—"
        miss = c.needs_clarification or []
        if c.status == "needs_info" or (c.status == "new" and miss):
            return "Demander : " + ", ".join(miss) if miss else "Relancer le client"
        return {"new": "Coter le forfait", "quoted": "Faire un suivi",
                "booked": "Préparer le départ", "closed": "—"}.get(c.status, "—")

    def table(cases, support=False) -> str:
        rows = []
        for c in cases:
            t = c.trip or {}
            name = escape(str(t.get("customer_name") or "Client inconnu"))
            base = (
                f"<tr data-href='/admin/cases/{c.id}'>"
                f"<td><a href='/admin/cases/{c.id}'>#{c.id}</a></td>"
                f"<td><b>{name}</b></td>"
                f"<td><span class='tag {c.status}'>{c.status}</span></td>"
                f"<td>{escape(c.channel)}</td>"
            )
            if support:
                preview = (c.raw_message or "").replace("\n", " ").strip()
                preview = (preview[:60] + "…") if len(preview) > 60 else (preview or "—")
                rows.append(
                    base
                    + f"<td>{escape(preview)}</td>"
                    + f"<td class='muted'>{escape(next_step(c))}</td>"
                    + f"<td class='muted'>{c.created_at:%Y-%m-%d %H:%M}</td></tr>"
                )
            else:
                where = escape(str(t.get("hotel_name_raw") or t.get("destination") or "—"))
                rows.append(
                    base
                    + f"<td>{where}</td>"
                    + f"<td>{c.parse_confidence:.2f}</td>"
                    + f"<td class='muted'>{escape(next_step(c))}</td>"
                    + f"<td class='muted'>{c.created_at:%Y-%m-%d %H:%M}</td></tr>"
                )
        if support:
            head = ("<th>#</th><th>Client</th><th>Statut</th><th>Canal</th>"
                    "<th>Message</th><th>Prochaine étape</th><th>Reçu</th>")
            ncol = 7
        else:
            head = ("<th>#</th><th>Client</th><th>Statut</th><th>Canal</th>"
                    "<th>Hôtel / Dest.</th><th>Conf.</th><th>Prochaine étape</th><th>Reçu</th>")
            ncol = 8
        empty = f"<tr><td colspan='{ncol}' class='muted'>Aucun dossier ici.</td></tr>"
        return f"<table><tr>{head}</tr>" + ("".join(rows) or empty) + "</table>"

    # Focused service-client queue: support cases awaiting our reply.
    if nav_active == "queue_service":
        with SessionLocal() as db:
            cases = (db.query(Case)
                     .filter(Case.kind == "support", Case.awaiting_reply.is_(True),
                             Case.status.notin_(("closed", "resolved")))
                     .order_by(Case.created_at.desc()).limit(200).all())
        body = page_header(SEC_TITLE[nav_active], "/admin/cases?status=service") + table(cases, support=True)
        return render_page(body, nav_active)

    # Focused travel sections (trip only).
    if nav_active in ("queue", "traveling", "completed"):
        with SessionLocal() as db:
            cases = (db.query(Case)
                     .filter(Case.kind == "trip", Case.status == status)
                     .order_by(Case.created_at.desc()).limit(200).all())
        body = page_header(SEC_TITLE[nav_active], f"/admin/cases?status={status}") + table(cases)
        return render_page(body, nav_active)

    # "Demandes": split between Voyage and Service client.
    view = "service" if view == "service" else "voyage"
    kind = "support" if view == "service" else "trip"
    with SessionLocal() as db:
        cases = (db.query(Case).filter(Case.kind == kind)
                 .order_by(Case.created_at.desc()).limit(300).all())
        n_voyage = db.query(Case).filter(Case.kind == "trip").count()
        n_service = db.query(Case).filter(Case.kind == "support").count()
    subtabs = (
        f"<a class='tab{' active' if view == 'voyage' else ''}' href='/admin/cases?view=voyage'>"
        f"✈️ Voyage<span class='tab-n'>{n_voyage}</span></a>"
        f"<a class='tab{' active' if view == 'service' else ''}' href='/admin/cases?view=service'>"
        f"💬 Service client<span class='tab-n'>{n_service}</span></a>"
    )
    body = f"<h2>Demandes</h2><div class='tabs'>{subtabs}</div>" + table(cases, support=(view == "service"))
    return render_page(body, "cases")


@app.get("/admin/clients", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
def admin_clients():
    with SessionLocal() as db:
        clients = (
            db.query(Client)
            .order_by(func.coalesce(Client.last_contact_at, Client.created_at).desc())
            .limit(300).all()
        )
        rows = []
        for cl in clients:
            reqs = cl.requests  # ordered most-recent-first by the relationship
            last = reqs[0] if reqs else None
            n_booked = sum(1 for r in reqs if r.status == "booked")
            name = escape(cl.display_name or "Client sans nom")
            contact = escape(cl.primary_email or cl.primary_phone or "—")
            last_status = (f"<span class='tag {last.status}'>{last.status}</span>"
                           if last else "<span class='muted'>—</span>")
            lastc = cl.last_contact_at.strftime("%Y-%m-%d %H:%M") if cl.last_contact_at else "—"
            rows.append(
                f"<tr data-href='/admin/clients/{cl.id}'>"
                f"<td><a href='/admin/clients/{cl.id}'><b>{name}</b></a></td>"
                f"<td>{contact}</td>"
                f"<td>{len(reqs)}</td>"
                f"<td>{n_booked}</td>"
                f"<td>{last_status}</td>"
                f"<td class='muted'>{lastc}</td></tr>"
            )

        # Suggested duplicates (clients sharing a name): offer a quick merge.
        dup_html = ""
        groups = find_duplicate_groups(db)
        if groups:
            cards = []
            for g in groups:
                ids = ",".join(str(c.id) for c in g)
                lines = "".join(
                    f"<div class='kv'><span class='k'>"
                    f"<a href='/admin/clients/{c.id}'>{escape(c.display_name or 'Client')}</a></span>"
                    f"<span class='v muted'>{escape(c.primary_email or c.primary_phone or '—')} · "
                    f"{len(c.requests)} demande(s)</span></div>"
                    for c in g
                )
                opts = "".join(
                    f"<option value='{c.id}'>{escape(c.display_name or 'Client')} "
                    f"(#{c.id}, {len(c.requests)} dem.)</option>" for c in g
                )
                cards.append(
                    f"<div class='dupgrp'>{lines}"
                    f"<form method='post' action='/admin/clients/merge' "
                    "onsubmit=\"return confirm('Fusionner ces fiches ? Action irréversible.')\" "
                    "style='margin-top:8px;display:flex;gap:8px;align-items:center;flex-wrap:wrap'>"
                    f"<input type='hidden' name='drop' value='{ids}'>"
                    f"<label class='sub'>Conserver :</label><select name='keep'>{opts}</select>"
                    "<button>Fusionner</button></form></div>"
                )
            dup_html = (
                "<div class='card' style='margin-bottom:18px'>"
                "<h3>Doublons possibles</h3>"
                "<p class='sub'>Mêmes noms détectés. Vérifie puis fusionne si c'est la même personne.</p>"
                + "".join(cards) + "</div>"
            )
    empty = "<tr><td colspan='6' class='muted'>Aucun client pour l'instant.</td></tr>"
    body = (
        "<h2>Clients</h2>"
        + dup_html
        + "<table><tr><th>Client</th><th>Contact</th><th>Demandes</th><th>Réservées</th>"
        "<th>Dernier statut</th><th>Dernier contact</th></tr>"
        + ("".join(rows) or empty)
        + "</table>"
    )
    return render_page(body, "clients")


@app.get("/admin/clients/{client_id}", response_class=HTMLResponse,
         dependencies=[Depends(require_admin)])
def admin_client_detail(client_id: int, tab: str = "identite"):
    KIND_FR = {"messenger_psid": "Messenger (PSID)", "email": "Courriel", "phone": "Téléphone"}
    tab = tab if tab in ("identite", "voyage", "service") else "identite"

    def val(x):
        if x in (None, "", "—"):
            return "<span class='muted'>—</span>"
        return escape(str(x))

    def kv(label, value):
        return f"<div class='kv'><span class='k'>{label}</span><span class='v'>{value}</span></div>"

    def status_form(cid, status, nxt, kind="trip"):
        choices = SUPPORT_STATUSES if kind == "support" else STATUSES
        opts = "".join(f"<option value='{s}'{' selected' if s == status else ''}>{s}</option>"
                       for s in choices)
        return (f"<form method='post' action='/admin/cases/{cid}/status' "
                "style='display:flex;gap:8px;align-items:center;margin:0'>"
                f"<input type='hidden' name='next' value=\"{escape(nxt)}\">"
                f"<select name='status'>{opts}</select>"
                "<button>Mettre à jour</button></form>")

    with SessionLocal() as db:
        _close_returned_trips(db)
        cl = db.get(Client, client_id)
        if not cl:
            return HTMLResponse(render_page("<p>Client introuvable.</p>", "clients"), status_code=404)
        reqs = cl.requests
        idents = cl.identities
        acts = cl.activities
        name = escape(cl.display_name or "Client sans nom")

        trips = [r for r in reqs if (r.kind or "trip") == "trip"]
        supports = [r for r in reqs if (r.kind or "trip") == "support"]
        booked = [r for r in trips if r.status == "booked"]
        est_total = 0.0
        for r in booked:
            amt = ((r.trip or {}).get("price_seen") or {}).get("amount")
            if isinstance(amt, (int, float)):
                est_total += amt

        base = f"/admin/clients/{cl.id}"
        # Top tiles — the two demande tiles are clickable to their tabs.
        stats = (
            "<div class='stats'>"
            f"<a class='stat' href='{base}?tab=voyage'><div class='stat-n'>{len(trips)}</div>"
            "<div class='stat-l'>Demandes de voyage</div></a>"
            f"<a class='stat' href='{base}?tab=service'><div class='stat-n'>{len(supports)}</div>"
            "<div class='stat-l'>Demandes de service</div></a>"
            f"<div class='stat'><div class='stat-n'>{len(booked)}</div><div class='stat-l'>Réservées</div></div>"
            f"<div class='stat'><div class='stat-n'>{est_total:,.0f} $</div><div class='stat-l'>Valeur estimée</div></div>"
            "</div>"
            "<p class='sub' style='margin:4px 0 14px'>Valeur estimée d'après les prix vus sur les "
            "demandes de voyage réservées.</p>"
        )

        def tnav(key, label, n):
            return (f"<a class='tab{' active' if tab == key else ''}' href='{base}?tab={key}'>"
                    f"{label}<span class='tab-n'>{n}</span></a>")
        tabs = ("<div class='tabs'>"
                f"<a class='tab{' active' if tab == 'identite' else ''}' href='{base}?tab=identite'>Identité</a>"
                + tnav("voyage", "✈️ Demandes de voyage", len(trips))
                + tnav("service", "💬 Demandes de service", len(supports))
                + "</div>")

        if tab == "identite":
            id_rows = "".join(kv(KIND_FR.get(i.kind, i.kind), escape(i.value)) for i in idents)
            chans = (("", "\u2014"), ("messenger", "messenger"), ("email", "courriel"), ("sms", "sms"))
            cur_ch = cl.preferred_channel or ""
            opts_ch = "".join(
                f"<option value='{c}'{' selected' if cur_ch == c else ''}>{lbl}</option>"
                for c, lbl in chans)

            view_block = (
                "<div id='idview'>"
                + kv("Nom", val(cl.display_name))
                + kv("Courriel", val(cl.primary_email))
                + kv("T\u00e9l\u00e9phone", val(cl.primary_phone))
                + kv("Canal pr\u00e9f\u00e9r\u00e9", val(cl.preferred_channel))
                + kv("Cr\u00e9\u00e9", cl.created_at.strftime("%Y-%m-%d %H:%M"))
                + kv("Dernier contact",
                     cl.last_contact_at.strftime("%Y-%m-%d %H:%M") if cl.last_contact_at else "\u2014")
                + "<h3 style='margin-top:18px'>Identifiants</h3>"
                + (id_rows or "<div class='muted'>Aucun.</div>")
                + "</div>"
            )
            edit_block = (
                f"<form id='idedit' method='post' action='/admin/clients/{cl.id}/update' style='display:none'>"
                "<label class='flbl'>Nom</label>"
                f"<input name='display_name' value=\"{escape(cl.display_name or '')}\">"
                "<label class='flbl' style='margin-top:10px'>Courriel</label>"
                f"<input name='primary_email' type='email' value=\"{escape(cl.primary_email or '')}\">"
                "<label class='flbl' style='margin-top:10px'>T\u00e9l\u00e9phone</label>"
                f"<input name='primary_phone' value=\"{escape(cl.primary_phone or '')}\">"
                "<label class='flbl' style='margin-top:10px'>Canal pr\u00e9f\u00e9r\u00e9</label>"
                f"<select name='preferred_channel'>{opts_ch}</select>"
                "<div style='margin-top:14px;display:flex;gap:8px'>"
                "<button>Enregistrer</button>"
                "<button type='button' class='btn-ghost' onclick='idEdit(false)'>Annuler</button>"
                "</div></form>"
            )
            identity_card = (
                "<div class='card'>"
                "<div class='cardhdr'>"
                "<button type='button' class='editbtn' onclick='idEdit(true)'>\u270f\ufe0f \u00c9diter</button>"
                "<span class='eyebrow'>Identit\u00e9</span></div>"
                + view_block + edit_block
                + "<script>function idEdit(o){var v=document.getElementById('idview'),"
                  "e=document.getElementById('idedit'),b=document.querySelector('.editbtn');"
                  "v.style.display=o?'none':'';e.style.display=o?'':'none';b.style.display=o?'none':'';}</script>"
                "</div>"
            )

            others = (db.query(Client).filter(Client.id != cl.id)
                      .order_by(func.coalesce(Client.display_name, "")).limit(500).all())
            merge_card = ""
            if others:
                mopts = "".join(
                    f"<option value='{o.id}'>{escape(o.display_name or 'Client')} (#{o.id})</option>"
                    for o in others)
                merge_card = (
                    "<div class='card'><h3>Fusion</h3>"
                    "<form method='post' action='/admin/clients/merge' "
                    "onsubmit=\"return confirm('Fusionner cette fiche dans la fiche choisie ? Action irr\u00e9versible.')\">"
                    f"<input type='hidden' name='drop' value='{cl.id}'>"
                    "<label class='flbl'>Fusionner cette fiche dans :</label>"
                    f"<select name='keep'>{mopts}</select>"
                    "<button class='btn-danger' style='margin-top:12px'>Fusionner</button></form>"
                    "<p class='sub'>D\u00e9place les demandes, identit\u00e9s et l'activit\u00e9 vers la fiche "
                    "choisie, puis supprime celle-ci.</p></div>"
                )

            KIND_LABEL = {
                "request_created": ("Nouvelle demande", "\U0001f195"),
                "message_in": ("Message re\u00e7u", "\U0001f4ac"),
                "status_change": ("Changement de statut", "\U0001f504"),
                "reply_out": ("R\u00e9ponse envoy\u00e9e", "\U0001f4e4"),
                "merge": ("Fusion de fiches", "\U0001f517"),
                "note": ("Note", "\U0001f4dd"),
            }
            if acts:
                items = []
                for a in acts:
                    lbl, icon = KIND_LABEL.get(a.kind, (a.kind, "\u2022"))
                    link = (f" <a href='/admin/cases/{a.request_id}'>#{a.request_id}</a>"
                            if a.request_id else "")
                    items.append(
                        f"<div class='tl'><div class='tl-dot'>{icon}</div>"
                        f"<div class='tl-body'><div class='tl-top'><b>{escape(lbl)}</b>{link}"
                        f"<span class='tl-at'>{a.created_at:%Y-%m-%d %H:%M}</span></div>"
                        f"<div class='muted'>{escape(a.summary)}</div></div></div>"
                    )
                act_items = "".join(items)
            else:
                act_items = "<div class='muted'>Aucune activit\u00e9 enregistr\u00e9e.</div>"
            activity_card = (
                "<div class='card act-card'><div class='act-inner'>"
                f"<h3>Activit\u00e9 \u00b7 {len(acts)}</h3>"
                f"<div class='act-scroll'>{act_items}</div>"
                "</div></div>"
            )

            content = (
                "<div class='idtab'>"
                f"<div class='col'>{identity_card}{merge_card}</div>"
                f"{activity_card}"
                "</div>"
            )
        elif tab == "voyage":
            if trips:
                blocks = []
                for r in trips:
                    nxt = f"{base}?tab=voyage"
                    blocks.append(
                        f"<div class='voyagebox' id='vb-{r.id}'>"
                        "<div class='pagehdr'>"
                        f"<h3 style='margin:0'>#{r.id} <span class='tag {r.status}'>{r.status}</span></h3>"
                        + status_form(r.id, r.status, nxt)
                        + "</div>"
                        f"<form method='post' action='/admin/cases/{r.id}/trip'>"
                        f"<input type='hidden' name='next' value=\"{nxt}\">"
                        f"<div class='grid2'>{_trip_info_cards(r, editable=True)}</div>"
                        "<div class='savebar'><button>Enregistrer les modifications</button> "
                        f"<button type='button' class='btn-ghost' onclick='tripEdit({r.id},false)'>Annuler</button></div>"
                        "</form>"
                        + _trip_fulfillment_section(r, nxt)
                        + f"<div style='margin-top:14px'><a href='/admin/cases/{r.id}'>Ouvrir le dossier &rarr;</a></div>"
                        "</div>"
                    )
                content = "".join(blocks) + (
                    "<script>function tripEdit(i,on){var b=document.getElementById('vb-'+i);"
                    "if(b)b.classList[on?'add':'remove']('editing');}</script>"
                )
            else:
                content = "<div class='card full'><div class='muted'>Aucune demande de voyage.</div></div>"

        else:
            if supports:
                rows, modals = [], []
                for r in supports:
                    msgs = _conversation(r)
                    last_in = next((m for m in reversed(msgs) if m.get("dir") == "in"), None)
                    preview = (last_in.get("text") if last_in else (r.raw_message or "")) or "—"
                    if len(preview) > 90:
                        preview = preview[:90] + "…"
                    rows.append(
                        f"<tr onclick=\"openSvc({r.id})\" style='cursor:pointer'>"
                        f"<td>#{r.id}</td>"
                        f"<td><span class='tag {r.status}'>{r.status}</span></td>"
                        f"<td>{escape(r.channel)}</td>"
                        f"<td>{escape(preview)}</td>"
                        f"<td>{r.created_at:%Y-%m-%d %H:%M}</td></tr>"
                    )
                    modals.append(
                        f"<div class='modal-ov' id='svc-{r.id}'>"
                        "<div class='modal'>"
                        "<div class='modal-hd'>"
                        f"<h3 style='margin:0'>#{r.id} <span class='tag {r.status}'>{r.status}</span></h3>"
                        + status_form(r.id, r.status, f"{base}?tab=service", kind="support")
                        + "<button type='button' class='modal-x' onclick='closeSvc()'>✕</button>"
                        "</div>"
                        f"<div class='modal-bd'>{_render_thread(r, name)}</div>"
                        "<div class='modal-ft'>"
                        f"<form method='post' action='/admin/cases/{r.id}/send'>"
                        f"<input type='hidden' name='next' value=\"{base}?tab=service\">"
                        "<textarea name='message' rows='3' placeholder='Répondre au client…' "
                        "style='width:100%'></textarea>"
                        "<button style='margin-top:10px'>Répondre au client</button></form>"
                        "</div></div></div>"
                    )
                content = (
                    "<table class='svc'><tr><th>#</th><th>Statut</th><th>Canal</th>"
                    "<th>Message</th><th>Reçu</th></tr>" + "".join(rows) + "</table>"
                    + "".join(modals)
                    + "<script>function openSvc(i){document.getElementById('svc-'+i).classList.add('open');}"
                    "function closeSvc(){document.querySelectorAll('.modal-ov.open').forEach("
                    "function(m){m.classList.remove('open');});}"
                    "document.addEventListener('click',function(e){if(e.target.classList&&"
                    "e.target.classList.contains('modal-ov'))closeSvc();});</script>"
                )
            else:
                content = "<div class='card full'><div class='muted'>Aucune demande de service.</div></div>"

        body = (
            "<p><a href='/admin/clients'>&larr; Tous les clients</a></p>"
            f"<h2>{name}</h2>"
            f"{stats}{tabs}{content}"
        )
    return render_page(body, "clients")


@app.post("/admin/clients/{client_id}/update", dependencies=[Depends(require_admin)])
async def admin_client_update(client_id: int, request: Request):
    form = await request.form()
    with SessionLocal() as db:
        cl = db.get(Client, client_id)
        if cl:
            if "display_name" in form:
                cl.display_name = (form.get("display_name") or "").strip() or None
            if "primary_email" in form:
                em = normalize_email(form.get("primary_email"))
                cl.primary_email = em
                if em:
                    add_identity(db, cl, "email", em)
            if "primary_phone" in form:
                ph = normalize_phone(form.get("primary_phone"))
                cl.primary_phone = ph
                if ph:
                    add_identity(db, cl, "phone", ph)
            if "preferred_channel" in form:
                cl.preferred_channel = (form.get("preferred_channel") or "").strip() or None
            if "notes" in form:
                cl.notes = (form.get("notes") or "").strip() or None
            if "tags" in form:
                cl.tags = [t.strip() for t in (form.get("tags") or "").split(",") if t.strip()]
            log_activity(db, cl.id, "note", "Fiche client modifiée")
            db.commit()
    return RedirectResponse(f"/admin/clients/{client_id}", status_code=303)


@app.post("/admin/clients/merge", dependencies=[Depends(require_admin)])
async def admin_clients_merge(request: Request):
    form = await request.form()
    try:
        keep = int(form.get("keep") or 0)
    except ValueError:
        keep = 0
    drops = [int(x) for x in (form.get("drop") or "").split(",") if x.strip().isdigit()]
    if keep:
        with SessionLocal() as db:
            for src in drops:
                if src and src != keep:
                    merge_clients(db, src, keep)
            db.commit()
        return RedirectResponse(f"/admin/clients/{keep}", status_code=303)
    return RedirectResponse("/admin/clients", status_code=303)


@app.get("/admin/search", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
def admin_search(q: str = ""):
    q = (q or "").strip()
    if not q:
        body = ("<h2>Recherche</h2><div class='card'><p class='muted'>"
                "Saisis un nom, un courriel, un téléphone ou un numéro de demande (ex. 42).</p></div>")
        return render_page(body, "")
    like = f"%{q}%"
    with SessionLocal() as db:
        found = {}
        for c in (db.query(Client).filter(
                (Client.display_name.ilike(like)) | (Client.primary_email.ilike(like))
                | (Client.primary_phone.ilike(like))).limit(50).all()):
            found[c.id] = c
        for ident in db.query(ClientIdentity).filter(
                ClientIdentity.value.ilike(like)).limit(50).all():
            if ident.client_id not in found and ident.client:
                found[ident.client_id] = ident.client
        crows = []
        for c in found.values():
            crows.append(
                f"<tr data-href='/admin/clients/{c.id}'>"
                f"<td><a href='/admin/clients/{c.id}'><b>{escape(c.display_name or 'Client')}</b></a></td>"
                f"<td>{escape(c.primary_email or c.primary_phone or '—')}</td>"
                f"<td>{len(c.requests)}</td></tr>"
            )
        case_block = ""
        digits = q.lstrip("#").strip()
        if digits.isdigit():
            cs = db.get(Case, int(digits))
            if cs:
                t = cs.trip or {}
                case_block = (
                    "<h3 class='sub' style='margin:20px 0 6px'>Demande #"
                    f"{cs.id}</h3><table><tr><th>#</th><th>Client</th><th>Statut</th>"
                    "<th>Hôtel / Dest.</th></tr>"
                    f"<tr data-href='/admin/cases/{cs.id}'>"
                    f"<td><a href='/admin/cases/{cs.id}'>#{cs.id}</a></td>"
                    f"<td>{escape(str(t.get('customer_name') or '—'))}</td>"
                    f"<td><span class='tag {cs.status}'>{cs.status}</span></td>"
                    f"<td>{escape(str(t.get('hotel_name_raw') or t.get('destination') or '—'))}</td>"
                    "</tr></table>"
                )
    ctable = ("<table><tr><th>Client</th><th>Contact</th><th>Demandes</th></tr>"
              + ("".join(crows) or "<tr><td colspan='3' class='muted'>Aucun client trouvé.</td></tr>")
              + "</table>")
    body = f"<h2>Recherche : « {escape(q)} »</h2>" + ctable + case_block
    return render_page(body, "")


@app.get("/admin/system", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
def admin_system():
    with SessionLocal() as db:
        try:
            db.execute(text("SELECT 1"))
            db_ok = True
        except Exception:  # noqa: BLE001
            db_ok = False
        n_clients = db.query(Client).count()
        n_cases = db.query(Case).count()
        n_ident = db.query(ClientIdentity).count()
        n_acts = db.query(Interaction).count()
        n_await = db.query(Case).filter(
            Case.awaiting_reply.is_(True), Case.status.notin_(("closed", "resolved"))).count()
    dialect = engine.dialect.name
    secret_ok = bool(settings.SECRET_KEY) and settings.SECRET_KEY != "dev-only-insecure-change-me"

    def chk(label, ok, detail=""):
        dot = "🟢" if ok else "🔴"
        return (f"<div class='kv'><span class='k'>{dot} {label}</span>"
                f"<span class='v'>{escape(detail)}</span></div>")

    services = (
        "<div class='card'><h3>Services</h3>"
        + chk("Base de données", db_ok, dialect)
        + chk("IA — clé Anthropic", bool(settings.ANTHROPIC_API_KEY),
              "configurée" if settings.ANTHROPIC_API_KEY else "absente")
        + chk("Messenger — token Facebook", bool(settings.FB_PAGE_TOKEN),
              "configuré" if settings.FB_PAGE_TOKEN else "absent")
        + chk("Cookie de session — SECRET_KEY", secret_ok,
              "OK" if secret_ok else "à définir")
        + "</div>"
    )
    stats = (
        "<div class='stats'>"
        f"<div class='stat'><div class='stat-n'>{n_clients}</div><div class='stat-l'>Clients</div></div>"
        f"<div class='stat'><div class='stat-n'>{n_cases}</div><div class='stat-l'>Demandes</div></div>"
        f"<div class='stat'><div class='stat-n'>{n_await}</div><div class='stat-l'>À répondre</div></div>"
        f"<div class='stat'><div class='stat-n'>{n_ident}</div><div class='stat-l'>Identités</div></div>"
        f"<div class='stat'><div class='stat-n'>{n_acts}</div><div class='stat-l'>Activités</div></div>"
        "</div>"
    )
    body = (page_header("System Health", "/admin/system") + stats
            + f"<div class='grid2'>{services}</div>")
    return render_page(body, "health")


@app.get("/admin/config", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
def admin_config():
    body = (
        page_header("System Config")
        + "<div class='card'><h3>Configurer l'accueil Messenger</h3>"
        "<p class='sub'>Définit la salutation, le bouton « Démarrer », les 3 bulles "
        "d'accueil et le menu ☰ de ta page.</p>"
        "<form method='post' action='/admin/setup-greeting' style='margin-top:14px'>"
        "<button>Configurer l'accueil Messenger</button></form></div>"
    )
    return render_page(body, "config")


@app.get("/admin/reports", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
def admin_reports():
    body = page_header("Reports") + "<div class='card'><p class='muted'>Rapports à venir.</p></div>"
    return render_page(body, "reports")


@app.get("/admin/espace-client", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
def admin_espace_client():
    body = (page_header("Espace client")
            + "<div class='card'><p class='muted'>Le portail self-service pour les clients "
              "sera bâti plus tard.</p></div>")
    return render_page(body, "")


@app.get("/admin/cases/{case_id}", response_class=HTMLResponse,
         dependencies=[Depends(require_admin)])
def admin_case_detail(case_id: int):
    import json as _json

    BOARD_FR = {"all_inclusive": "Tout inclus", "breakfast": "Petit-déjeuner",
                "half_board": "Demi-pension", "full_board": "Pension complète",
                "room_only": "Chambre seulement", "other": "Autre", "unknown": None}
    BASIS_FR = {"per_person": "par personne", "total": "pour le groupe", "unknown": None}
    CHANNEL_FR = {"messenger": "Messenger", "email": "Courriel", "sms": "SMS", "unknown": None}

    def val(x):
        """Render a value, or a muted dash when empty."""
        if x in (None, "", [], "unknown"):
            return "<span class='muted'>—</span>"
        return escape(str(x))

    def kv(label, value, sub=None):
        sub_html = f"<div class='sub'>{escape(sub)}</div>" if sub else ""
        return f"<div class='kv'><span class='k'>{label}</span><span class='v'>{value}{sub_html}</span></div>"

    def card(title, *rows):
        return f"<div class='card'><h3>{title}</h3>{''.join(rows)}</div>"

    with SessionLocal() as db:
        c = db.get(Case, case_id)
        if not c:
            return HTMLResponse(render_page("<p>Introuvable.</p>", "cases"), status_code=404)
        t = c.trip or {}

        # Customer-service case: show the message(s) and a reply box, nothing more.
        if (c.kind or "trip") == "support":
            name = escape(str(t.get("customer_name") or "Client inconnu"))
            convo = _conversation(c)
            if convo:
                bubbles = []
                for m in convo:
                    out = m.get("dir") == "out"
                    at = (m.get("at") or "").replace("T", " ")
                    who = "Toi" if out else name
                    bubbles.append(
                        f"<div class='{'msg-out' if out else 'msg-in'}'>"
                        f"{escape(m.get('text') or '')}"
                        f"<div class='msg-at'>{who} · {escape(at)}</div></div>")
                msg_html = "".join(bubbles)
            else:
                msg_html = "<p class='muted'>En attente du premier message du client…</p>"
            opts = "".join(
                f"<option value='{s}'{' selected' if s == c.status else ''}>{s}</option>"
                for s in SUPPORT_STATUSES)
            client_link = (f"<a href='/admin/clients/{c.client_id}'>voir la fiche &rarr;</a>"
                           if c.client_id else "<span class='muted'>—</span>")

            shots = c.screenshots or []
            shot_html = ""
            if shots:
                imgs = "".join(
                                        f"<img src='/admin/cases/{c.id}/screenshot/{i}' alt='pièce {i+1}' "
                    f"onclick=\"lightbox('/admin/cases/{c.id}/screenshot/{i}')\" "
                    f"style='max-width:100%;cursor:zoom-in;border-radius:10px;border:1px solid var(--line);margin-bottom:10px'>"
                    for i in range(len(shots)))
                shot_html = f"<div class='card full'><h3>Pièce(s) jointe(s) · {len(shots)}</h3>{imgs}</div>"

            reply = ""
            if c.channel == "messenger" and c.sender_ref:
                reply = (
                    "<div class='card full'><h3>Répondre au client</h3>"
                    f"<form method='post' action='/admin/cases/{c.id}/send'>"
                    "<textarea name='message' rows='4' placeholder='Écris ta réponse au client…' "
                    "style='width:100%'></textarea>"
                    "<button style='margin-top:10px'>Envoyer sur Messenger</button></form>"
                    "<p class='sub'>Envoi direct via Messenger (fenêtre de 24 h après le dernier "
                    "message du client). Une fois envoyé, le dossier sort de la cloche.</p></div>"
                )
            else:
                reply = ("<div class='card full'><h3>Répondre au client</h3>"
                         "<p class='muted'>Réponse directe indisponible pour ce canal.</p></div>")

            info = (
                "<div class='card'><h3>Client</h3>"
                f"<div class='kv'><span class='k'>Nom</span><span class='v'>{name}</span></div>"
                f"<div class='kv'><span class='k'>Canal</span><span class='v'>{escape(c.channel)}</span></div>"
                f"<div class='kv'><span class='k'>Reçu</span><span class='v'>{c.created_at:%Y-%m-%d %H:%M}</span></div>"
                f"<div class='kv'><span class='k'>Fiche client</span><span class='v'>{client_link}</span></div>"
                "</div>"
            )
            msg_card = f"<div class='card full'><h3>Conversation</h3>{msg_html}</div>"
            status_form = (
                f"<form method='post' action='/admin/cases/{c.id}/status' "
                "style='display:flex;gap:8px;align-items:center;margin:0'>"
                f"<select name='status'>{opts}</select>"
                "<button>Mettre à jour</button></form>"
            )
            body = (
                "<p><a href='/admin/cases?view=service'>&larr; Service client</a></p>"
                "<div class='pagehdr'>"
                f"<h2 style='margin:0'>#{c.id} · {name} <span class='tag {c.status}'>{c.status}</span> "
                "<span class='tag'>service client</span></h2>"
                f"{status_form}</div>"
                f"<div class='grid2'>{info}{msg_card}{reply}{shot_html}</div>"
            )
            return render_page(body, "cases")

        # --- derived display values ---
        name = escape(str(t.get("customer_name") or "Client inconnu"))
        # origin
        city, iata = t.get("origin_city"), t.get("origin_airport_iata")
        origin = f"{city} ({iata})" if city and iata else (city or iata)
        # dates
        dep, ret, nights = t.get("departure_date"), t.get("return_date"), t.get("nights")
        if dep or ret:
            dates = f"{dep or '?'} → {ret or '?'}" + (f" · {nights} nuits" if nights else "")
        else:
            dates = t.get("dates_raw")
        # hotel + normalized subtext
        hotel = t.get("hotel_name_raw")
        norm = t.get("hotel_name_normalized")
        hotel_sub = norm if norm and norm != hotel else None
        # passengers ages
        ages = [str(p.get("age")) for p in (t.get("passengers") or []) if p.get("age") is not None]
        ages_str = ", ".join(ages) if ages else None
        # price
        ps = t.get("price_seen") or {}
        price_amt = f"{ps.get('amount')} {ps.get('currency', 'CAD')}" if ps.get("amount") is not None else None
        taxes = ps.get("taxes_included")
        taxes_str = "Oui" if taxes is True else ("Non" if taxes is False else None)

        # confidence meter
        conf = float(c.parse_confidence or 0)
        conf_color = "#e0675b" if conf < 0.4 else ("#e0b35b" if conf < 0.7 else "#7be0a0")

        # missing info
        missing = c.needs_clarification or []
        if missing:
            missing_html = "<div class='chips'>" + "".join(
                f"<span class='chip'>{escape(m)}</span>" for m in missing) + "</div>"
        else:
            missing_html = "<div class='ok'>✓ Profil complet — prêt à coter</div>"

        opts = "".join(
            f"<option value='{s}'{' selected' if s == c.status else ''}>{s}</option>"
            for s in STATUSES)

        cl = db.get(Client, c.client_id) if c.client_id else None
        disp_email = t.get("customer_email") or (cl.primary_email if cl else None)
        disp_phone = t.get("customer_phone") or (cl.primary_phone if cl else None)
        cards = (
            card("Client",
                 kv("Nom", val(t.get("customer_name") or (cl.display_name if cl else None))),
                 kv("Courriel", val(disp_email)),
                 kv("Téléphone", val(disp_phone)),
                 kv("Reçoit l'offre par", val(CHANNEL_FR.get(t.get("preferred_channel")))),
                 kv("Canal", val(c.channel)),
                 kv("Reçu", c.created_at.strftime("%Y-%m-%d %H:%M")),
                 (kv("Fiche client", f"<a href='/admin/clients/{c.client_id}'>voir l'historique &rarr;</a>")
                  if c.client_id else "")) +
            card("Voyage",
                 kv("Destination", val(t.get("destination"))),
                 kv("Hôtel", val(hotel), sub=hotel_sub),
                 kv("Départ", val(origin)),
                 kv("Dates", val(dates)),
                 kv("Forfait", val(BOARD_FR.get(t.get("board")))),
                 kv("Transporteur", val(t.get("operator")))) +
            card("Voyageurs",
                 kv("Adultes", val(t.get("num_adults"))),
                 kv("Enfants", val(t.get("num_children"))),
                 kv("Âges", val(ages_str)),
                 kv("Chambres", val(t.get("num_rooms"))),
                 kv("Type de chambre", val(t.get("room_type")))) +
            card("Prix trouvé",
                 kv("Montant", val(price_amt)),
                 kv("Base", val(BASIS_FR.get(ps.get("basis")))),
                 kv("Taxes incluses", val(taxes_str)),
                 kv("Source", val(t.get("source"))),
                 kv("Texte original", val(ps.get("raw"))))
        )

        profil = (
            "<div class='card full'><h3>Profilage</h3>"
            f"<div class='kv'><span class='k'>Confiance</span>"
            f"<span class='v'>{conf:.0%}</span></div>"
            f"<div class='meter'><i style='width:{conf*100:.0f}%;background:{conf_color}'></i></div>"
            "<div class='kv'><span class='k'>Infos à demander au client</span></div>"
            f"{missing_html}"
            + (f"<div class='kv'><span class='k'>Notes</span><span class='v'>{val(t.get('agent_notes'))}</span></div>"
               if t.get("agent_notes") else "")
            + "</div>"
        )

        convo = (
            "<div class='card full'><h3>Conversation</h3>"
            + _render_thread(c, name) + "</div>"
        )

        shots = c.screenshots or []
        if shots:
            imgs = "".join(
                                f"<img src='/admin/cases/{c.id}/screenshot/{i}' alt='capture {i+1}' "
            f"onclick=\"lightbox('/admin/cases/{c.id}/screenshot/{i}')\" "
                "style='max-width:100%;max-height:460px;display:block;margin:0 auto 10px;cursor:zoom-in;"
                "border-radius:10px;border:1px solid var(--line);background:rgba(3,18,27,.4)'>"
                for i in range(len(shots))
            )
            screenshot_card = (
                f"<div class='card'><h3>Capture(s) d'écran · {len(shots)}</h3>{imgs}"
                "<p class='sub'>Clique pour agrandir.</p></div>"
            )
        else:
            screenshot_card = ""

        status_form = (
            f"<form method='post' action='/admin/cases/{c.id}/status' "
            "style='display:flex;gap:8px;align-items:center;margin:0'>"
            f"<select name='status'>{opts}</select>"
            "<button>Mettre à jour</button></form>"
        )

        # Manual reply panel — send a personalized message (e.g. the offer)
        # straight to the Messenger customer, bypassing the bot.
        if c.channel == "messenger" and c.sender_ref:
            send_panel = (
                "<div class='card full'><h3>Réponse manuelle (sans le bot)</h3>"
                f"<form method='post' action='/admin/cases/{c.id}/send'>"
                "<textarea name='message' rows='3' placeholder='Écris ton message ou ton offre au client…' "
                "style='width:100%'></textarea>"
                "<button style='margin-top:10px'>Envoyer au client sur Messenger</button></form>"
                "<p class='sub'>Envoi direct via Messenger, sans déclencher le bot. "
                "Fonctionne dans la fenêtre de 24 h après le dernier message du client.</p></div>"
            )
        else:
            send_panel = ""

        raw = (
            "<details><summary>Voir les données brutes (TripRequest)</summary>"
            f"<pre>{escape(_json.dumps(t, indent=2, ensure_ascii=False))}</pre></details>"
        )

        body = (
            "<p><a href='/admin/cases'>&larr; Tous les dossiers</a></p>"
            "<div class='pagehdr'>"
            f"<h2 style='margin:0'>#{c.id} · {name} <span class='tag {c.status}'>{c.status}</span></h2>"
            f"{status_form}</div>"
            f"<div class='grid2'>{cards}{screenshot_card}</div>"
            f"<div class='grid2'>{profil}{convo}{send_panel}</div>"
            f"{raw}"
        )
    return render_page(body, "cases")


@app.post("/admin/cases/{case_id}/status", dependencies=[Depends(require_admin)])
async def admin_update_status(case_id: int, request: Request):
    form = await request.form()
    new_status = form.get("status")
    nxt = form.get("next") or f"/admin/cases/{case_id}"
    with SessionLocal() as db:
        c = db.get(Case, case_id)
        allowed = SUPPORT_STATUSES if (c and c.kind == "support") else STATUSES
        if c and new_status in allowed and new_status != c.status:
            log_activity(db, c.client_id, "status_change",
                         f"Statut : {c.status} → {new_status}", c.id)
            c.status = new_status
            c.awaiting_reply = False                    # we triaged it
            if new_status == "booked":                  # capture flight dates from the trip
                tt = c.trip or {}
                c.flight_depart = c.flight_depart or tt.get("departure_date")
                c.flight_return = c.flight_return or tt.get("return_date")
            db.commit()
    if not nxt.startswith("/admin/"):                   # only allow internal redirects
        nxt = f"/admin/cases/{case_id}"
    return RedirectResponse(nxt, status_code=303)


@app.post("/admin/cases/{case_id}/quote", dependencies=[Depends(require_admin)])
async def admin_case_quote(case_id: int, request: Request):
    form = await request.form()
    nxt = form.get("next") or f"/admin/cases/{case_id}"
    with SessionLocal() as db:
        c = db.get(Case, case_id)
        if c:
            c.quote_url = (form.get("quote_url") or "").strip() or None
            c.savings = (form.get("savings") or "").strip() or None
            log_activity(db, c.client_id, "note",
                         "Quote déposée" if c.quote_url else "Quote retirée", c.id)
            db.commit()
    if not nxt.startswith("/admin/"):
        nxt = f"/admin/cases/{case_id}"
    return RedirectResponse(nxt, status_code=303)


@app.post("/admin/cases/{case_id}/trip", dependencies=[Depends(require_admin)])
async def admin_case_trip(case_id: int, request: Request):
    """Update the trip's Voyage / Voyageurs / Prix fields from the inline editor,
    then recompute what's still missing and the (non-terminal) status."""
    form = await request.form()
    nxt = form.get("next") or f"/admin/cases/{case_id}"

    def g(k):
        return (form.get(k) or "").strip() or None

    def gi(k):
        v = (form.get(k) or "").strip()
        try:
            return int(float(v)) if v else None
        except ValueError:
            return None

    def gf(k):
        v = (form.get(k) or "").strip()
        try:
            return float(v) if v else None
        except ValueError:
            return None

    taxes_raw = (form.get("price_taxes") or "").strip()
    taxes = True if taxes_raw == "true" else (False if taxes_raw == "false" else None)
    with SessionLocal() as db:
        c = db.get(Case, case_id)
        if c:
            d = dict(c.trip or {})
            d.update({
                "destination": g("destination"),
                "hotel_name_raw": g("hotel_name_raw"),
                "origin_city": g("origin_city"),
                "origin_airport_iata": g("origin_airport_iata"),
                "departure_date": g("departure_date"),
                "return_date": g("return_date"),
                "nights": gi("nights"),
                "board": g("board") or "unknown",
                "operator": g("operator"),
                "num_adults": gi("num_adults"),
                "num_children": gi("num_children"),
                "num_rooms": gi("num_rooms"),
                "room_type": g("room_type"),
                "source": g("source"),
            })
            ps = dict(d.get("price_seen") or {})
            ps.update({"amount": gf("price_amount"), "currency": g("price_currency") or "CAD",
                       "basis": g("price_basis") or "unknown", "taxes_included": taxes,
                       "raw": g("price_raw")})
            d["price_seen"] = ps
            try:
                trip = TripRequest.model_validate(d)
            except Exception:  # noqa: BLE001
                trip = None
            if trip is not None:
                rem = trip.remaining_fields()
                c.trip = trip.model_dump()
                c.needs_clarification = rem
                c.customer_email = trip.customer_email or c.customer_email
                c.customer_phone = trip.customer_phone or c.customer_phone
                if c.status in ("new", "needs_info"):
                    c.status = "new" if not rem else "needs_info"
                if c.status == "booked":               # keep flights synced with trip dates
                    c.flight_depart = trip.departure_date
                    c.flight_return = trip.return_date
                log_activity(db, c.client_id, "note", "Dossier modifié (cartes voyage)", c.id)
                db.commit()
    if not nxt.startswith("/admin/"):
        nxt = f"/admin/cases/{case_id}"
    return RedirectResponse(nxt, status_code=303)


@app.post("/admin/cases/{case_id}/flights", dependencies=[Depends(require_admin)])
async def admin_case_flights(case_id: int, request: Request):
    form = await request.form()
    nxt = form.get("next") or f"/admin/cases/{case_id}"
    with SessionLocal() as db:
        c = db.get(Case, case_id)
        if c:
            c.flight_depart = (form.get("flight_depart") or "").strip() or None
            c.flight_return = (form.get("flight_return") or "").strip() or None
            log_activity(db, c.client_id, "note", "Dates de vol enregistrées", c.id)
            db.commit()
    if not nxt.startswith("/admin/"):
        nxt = f"/admin/cases/{case_id}"
    return RedirectResponse(nxt, status_code=303)


@app.get("/admin/cases/{case_id}/screenshot/{idx}", dependencies=[Depends(require_admin)])
def admin_case_screenshot(case_id: int, idx: int):
    with SessionLocal() as db:
        c = db.get(Case, case_id)
        shots = (c.screenshots if c else None) or []
        if not c or idx < 0 or idx >= len(shots):
            raise HTTPException(status_code=404, detail="Capture introuvable")
        shot = shots[idx]
    data, media_type = storage.read_screenshot(shot)
    if data is None:
        raise HTTPException(status_code=404, detail="Capture illisible")
    return Response(content=data, media_type=media_type)


@app.post("/admin/setup-greeting", dependencies=[Depends(require_admin)])
def admin_setup_greeting():
    """One-time: set the page's greeting text, Get Started button, and the three
    ice-breaker bubbles (rabais / question générale / conseiller humain)."""
    ok1, d1 = set_messenger_profile(
        settings.FB_PAGE_TOKEN, GREETING_TEXT, GET_STARTED_PAYLOAD,
        settings.FB_GRAPH_VERSION)
    ok2, d2 = set_ice_breakers(
        settings.FB_PAGE_TOKEN, ICE_BREAKERS, settings.FB_GRAPH_VERSION)
    ok3, d3 = set_persistent_menu(
        settings.FB_PAGE_TOKEN, PERSISTENT_MENU, settings.FB_GRAPH_VERSION)
    ok = ok1 and ok2 and ok3
    title = "✅ Accueil Messenger configuré" if ok else "❌ Configuration partielle / échec"
    bubbles = "".join(f"<li>{escape(ib['question'])}</li>" for ib in ICE_BREAKERS)
    body = (
        "<p><a href='/admin/config'>&larr; Retour à System Config</a></p>"
        f"<h2>{title}</h2>"
        "<p class='sub'>Salutation + bouton « Démarrer » + 3 bulles d'accueil + menu ☰ "
        "permanent envoyés à Meta. Ouvre la conversation avec ta page (ou efface "
        "l'historique) pour voir les bulles et le menu.</p>"
        f"<div class='card'><h3>Bulles & menu configurés</h3><ul>{bubbles}</ul>"
        "<p class='sub'>1) Rabais → outil de profilage · 2) Question générale → concierge IA · "
        "3) Conseiller → support humain (sans IA, notifié dans la cloche). "
        "Mêmes choix dans le menu ☰ permanent, plus des puces de triage si le client "
        "écrit sans cliquer.</p></div>"
        f"<details><summary>Réponse de Meta</summary><pre>greeting/get_started : {escape(d1)}\n\n"
        f"ice_breakers : {escape(d2)}\n\npersistent_menu : {escape(d3)}</pre></details>"
    )
    return render_page(body, "config")


@app.post("/admin/cases/{case_id}/send", dependencies=[Depends(require_admin)])
async def admin_case_send(case_id: int, request: Request):
    """Agent sends a personalized message straight to the Messenger customer."""
    form = await request.form()
    message = (form.get("message") or "").strip()
    nxt = form.get("next") or f"/admin/cases/{case_id}"
    with SessionLocal() as db:
        c = db.get(Case, case_id)
        sender = c.sender_ref if c else None
        client_id = c.client_id if c else None
    if sender and message and settings.FB_PAGE_TOKEN:
        ok = send_text(sender, message, settings.FB_PAGE_TOKEN, settings.FB_GRAPH_VERSION)
        log.info("Manual reply to case #%s: %s", case_id, "sent" if ok else "failed")
        if ok:
            preview = message if len(message) <= 80 else message[:77] + "…"
            now = datetime.utcnow().isoformat(timespec="seconds")
            with SessionLocal() as db:
                c = db.get(Case, case_id)
                if c:
                    c.awaiting_reply = False            # we replied
                    c.messages = _conversation(c) + [{"dir": "out", "text": message, "at": now}]
                log_activity(db, client_id, "reply_out", f"Message envoyé : {preview}", case_id)
                db.commit()
    if not nxt.startswith("/admin/"):
        nxt = f"/admin/cases/{case_id}"
    return RedirectResponse(nxt, status_code=303)


@app.post("/admin/reset", dependencies=[Depends(require_admin)])
def admin_reset():
    """Wipe ALL data (cases, clients, identities, activity). Disabled unless
    ALLOW_RESET is set."""
    if not settings.ALLOW_RESET:
        raise HTTPException(status_code=403,
                            detail="Reset désactivé. Mettre ALLOW_RESET=1 pour activer.")
    with SessionLocal() as db:
        # Best-effort: drop screenshot objects from R2 before wiping rows.
        if storage.r2_enabled():
            for c in db.query(Case).all():
                for s in (c.screenshots or []):
                    storage.delete_screenshot(s)
        if engine.dialect.name == "postgresql":
            db.execute(text(
                "TRUNCATE TABLE interactions, client_identities, cases, clients RESTART IDENTITY CASCADE"))
        else:
            # SQLite: delete children first to respect FKs.
            db.query(Interaction).delete()
            db.query(ClientIdentity).delete()
            db.query(Case).delete()
            db.query(Client).delete()
        db.commit()
    log.info("All CRM data wiped via /admin/reset")
    return RedirectResponse("/admin/cases", status_code=303)
