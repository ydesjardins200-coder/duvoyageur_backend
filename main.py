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
from sqlalchemy import text
from starlette.middleware.sessions import SessionMiddleware

from auth import NotAuthenticated, check_credentials, require_admin
from concierge import concierge_reply
from config import settings
from db import STATUSES, Case, SessionLocal, engine, find_open_case_for_sender, init_db
from facebook import (extract_messages, extract_postbacks, get_user_name,
                      send_text, set_messenger_profile, valid_signature, verify_challenge)
from parser import parse_trip
from trip_schema import ContactChannel, TripRequest, merge_trip_requests

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("duvoyageur")

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
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
        case = Case(
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

    # The message brought new info (or was a screenshot) -> keep profiling.
    rem = trip.remaining_fields()
    # Treat as a general question only if it looks like one, or there's nothing
    # left to profile (so a vague opener still gets the screenshot-first prompt).
    is_question = ("?" in text) or (not rem)
    if advanced or not text or not settings.CONCIERGE_ENABLED or not is_question:
        return bool(send_text(sender, _ack_message(trip, has_shot),
                              settings.FB_PAGE_TOKEN, settings.FB_GRAPH_VERSION))

    # No new info + looks like a question -> answer it (hybrid), then, if we're
    # still profiling, re-ask the pending question.
    answer = concierge_reply(text, trip)
    if rem:
        q = trip.next_question()
        reply = (answer + " " + q) if answer else "Merci ! 🌴 " + q
    else:
        reply = answer or ("Bonne question ! 🌴 Un conseiller va te revenir bientôt "
                           "avec ton offre et pourra répondre à ça. 👍")
    return bool(send_text(sender, reply, settings.FB_PAGE_TOKEN, settings.FB_GRAPH_VERSION))


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

    try:
        new_trip = parse_trip(text, images=downloaded)
    except Exception as e:  # noqa: BLE001
        # Never drop a customer message — store a fallback the agent can rescue.
        log.exception("Parse failed; storing fallback case: %s", e)
        new_trip = TripRequest(raw_message=text or "(capture d'écran)", source="messenger",
                               agent_notes=f"Parsing automatique échoué: {e}",
                               needs_clarification=["à traiter manuellement"])

    # Persistable screenshot records (base64 so they survive Facebook's expiring URLs).
    shots = [{
        "media_type": mt,
        "b64": base64.b64encode(b).decode("ascii"),
        "received_at": datetime.utcnow().isoformat(timespec="seconds"),
    } for (b, mt) in downloaded]

    # Find-or-merge: keep one evolving case per customer (progressive profiling).
    with SessionLocal() as db:
        existing = find_open_case_for_sender(db, sender)
        if existing:
            before_trip = TripRequest.model_validate(existing.trip)
            was_complete = not before_trip.remaining_fields()
            if was_complete and text and not downloaded:
                # Dossier already finalized + a text-only message => treat as a
                # question. Don't overwrite the trip; just log it for the agent.
                trip = before_trip.model_copy(deep=True)
                trip.raw_message = ((existing.raw_message + "\n---\n" + text)
                                    if existing.raw_message else text)
                existing.raw_message = trip.raw_message
                existing.trip = trip.model_dump()
                advanced = False
            else:
                trip = merge_trip_requests(before_trip, new_trip)
                advanced = _trip_changed(before_trip, trip)
                existing.trip = trip.model_dump()
                existing.needs_clarification = trip.needs_clarification
                existing.parse_confidence = trip.parse_confidence
                existing.raw_message = trip.raw_message
                existing.customer_email = trip.customer_email or existing.customer_email
                existing.customer_phone = trip.customer_phone or existing.customer_phone
                existing.status = "needs_info" if trip.needs_clarification else "new"
                if shots:                               # accumulate screenshots
                    existing.screenshots = (existing.screenshots or []) + shots
            db.commit()
            log.info("Merged into case #%s (sender %s, +%d shots, advanced=%s)",
                     existing.id, sender, len(shots), advanced)
        else:
            # Resolve the customer's Facebook name once, when the case is created.
            if sender and settings.FB_PAGE_TOKEN and not new_trip.customer_name:
                name = get_user_name(sender, settings.FB_PAGE_TOKEN, settings.FB_GRAPH_VERSION)
                if name:
                    new_trip.customer_name = name
            trip = new_trip
            rem = new_trip.remaining_fields()
            case = Case(
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
            )
            db.add(case)
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


def process_postback(sender: str | None, payload: str) -> None:
    """Handle button taps. On Get Started, fire the screenshot-first welcome."""
    if not (sender and settings.FB_PAGE_TOKEN) or payload != GET_STARTED_PAYLOAD:
        return
    first = ""
    name = get_user_name(sender, settings.FB_PAGE_TOKEN, settings.FB_GRAPH_VERSION)
    if name:
        first = " " + name.split()[0]
    greeting = (
        f"Salut{first} ! 🌴 Bienvenue chez Du Voyageur. Trouve ton forfait tout "
        "inclus et envoie-moi une capture d'écran 📸 — je te trouve le même voyage "
        "avec un rabais. Tu peux m'envoyer ta capture tout de suite !"
    )
    sent = send_text(sender, greeting, settings.FB_PAGE_TOKEN, settings.FB_GRAPH_VERSION)
    log.info("Get Started greeting to %s: %s", sender, "sent" if sent else "failed")


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

    shots = [{
        "media_type": media,
        "b64": base64.b64encode(img_bytes).decode("ascii"),
        "received_at": datetime.utcnow().isoformat(timespec="seconds"),
    }]
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
 header{{position:sticky;top:0;z-index:10;display:flex;justify-content:space-between;align-items:center;
   gap:12px;padding:14px 22px;background:rgba(6,33,47,.72);backdrop-filter:blur(12px);
   -webkit-backdrop-filter:blur(12px);border-bottom:1px solid var(--line)}}
 .brand{{display:flex;align-items:center;gap:11px}}
 .brand img{{width:34px;height:34px;border-radius:50%;box-shadow:0 0 0 1px var(--line)}}
 h1{{font-family:"Bricolage Grotesque",sans-serif;font-weight:800;font-size:18px;letter-spacing:-.02em;margin:0}}
 h2{{font-family:"Bricolage Grotesque",sans-serif;font-weight:700;font-size:22px;letter-spacing:-.02em;margin:6px 0 4px}}
 .logout{{color:var(--mist);font-size:13px;font-family:"Space Grotesk",monospace}} .logout:hover{{color:var(--foam);text-decoration:none}}
 main{{padding:22px;max-width:1100px;margin:auto}}
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
 .muted{{color:var(--mist)}} code{{background:rgba(3,18,27,.6);padding:1px 6px;border-radius:5px}}
 .grid2{{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:16px;margin:18px 0}}
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
</style></head><body>
<header><span class="brand"><img src="/static/logo.png" alt=""><h1>Du Voyageur — Dossiers</h1></span><a class="logout" href="/admin/logout">Déconnexion</a></header>
<main>{body}</main></body></html>"""


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
def admin_cases():
    with SessionLocal() as db:
        cases = db.query(Case).order_by(Case.created_at.desc()).limit(200).all()
    rows = []
    for c in cases:
        t = c.trip or {}
        name = escape(str(t.get("customer_name") or "Client inconnu"))
        where = escape(str(t.get("hotel_name_raw") or t.get("destination") or "—"))
        needs = ", ".join(c.needs_clarification or []) or "—"
        rows.append(
            f"<tr><td><a href='/admin/cases/{c.id}'>#{c.id}</a></td>"
            f"<td><b>{name}</b></td>"
            f"<td><span class='tag {c.status}'>{c.status}</span></td>"
            f"<td>{escape(c.channel)}</td>"
            f"<td>{where}</td>"
            f"<td>{c.parse_confidence:.2f}</td>"
            f"<td class='muted'>{escape(needs)}</td>"
            f"<td class='muted'>{c.created_at:%Y-%m-%d %H:%M}</td></tr>"
        )
    body = (
        "<table><tr><th>#</th><th>Client</th><th>Statut</th><th>Canal</th><th>Hôtel / Dest.</th>"
        "<th>Conf.</th><th>À demander</th><th>Reçu</th></tr>"
        + ("".join(rows) or "<tr><td colspan='8' class='muted'>Aucun dossier.</td></tr>")
        + "</table>"
    )
    body += (
        "<form method='post' action='/admin/setup-greeting' style='margin-top:24px;display:inline-block'>"
        "<button>Configurer l'accueil Messenger</button></form>"
        "<p class='sub'>Définit la salutation + le bouton « Get Started » de ta page (une seule fois).</p>"
    )
    if settings.ALLOW_RESET:
        body += (
            "<form method='post' action='/admin/reset' style='margin-top:24px' "
            "onsubmit=\"return confirm('Effacer TOUS les dossiers ? Action irréversible.')\">"
            "<button class='btn-danger'>Vider tous les dossiers (test)</button></form>"
        )
    return _PAGE.format(body=body)


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
            return HTMLResponse(_PAGE.format(body="<p>Introuvable.</p>"), status_code=404)
        t = c.trip or {}

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

        cards = (
            card("Client",
                 kv("Nom", val(t.get("customer_name"))),
                 kv("Courriel", val(t.get("customer_email"))),
                 kv("Téléphone", val(t.get("customer_phone"))),
                 kv("Reçoit l'offre par", val(CHANNEL_FR.get(t.get("preferred_channel")))),
                 kv("Canal", val(c.channel)),
                 kv("Reçu", c.created_at.strftime("%Y-%m-%d %H:%M"))) +
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
            f"<pre>{escape(c.raw_message or '—')}</pre></div>"
        )

        shots = c.screenshots or []
        if shots:
            imgs = "".join(
                f"<a href='/admin/cases/{c.id}/screenshot/{i}' target='_blank'>"
                f"<img src='/admin/cases/{c.id}/screenshot/{i}' alt='capture {i+1}' "
                f"style='max-width:100%;border-radius:10px;border:1px solid var(--line);margin-bottom:10px'></a>"
                for i in range(len(shots))
            )
            screenshot_card = (
                f"<div class='card full'><h3>Capture(s) d'écran · {len(shots)}</h3>{imgs}</div>"
            )
        else:
            screenshot_card = ""

        actions = (
            "<form method='post' action='/admin/cases/" + str(c.id) + "/status' style='margin-top:18px'>"
            f"<label class='muted'>Statut : </label><select name='status'>{opts}</select> "
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
            f"<h2>#{c.id} · {name} <span class='tag {c.status}'>{c.status}</span></h2>"
            f"<div class='grid2'>{cards}{screenshot_card}{profil}{send_panel}{convo}</div>"
            f"{actions}{raw}"
        )
    return _PAGE.format(body=body)


@app.post("/admin/cases/{case_id}/status", dependencies=[Depends(require_admin)])
async def admin_update_status(case_id: int, request: Request):
    form = await request.form()
    new_status = form.get("status")
    with SessionLocal() as db:
        c = db.get(Case, case_id)
        if c and new_status in STATUSES:
            c.status = new_status
            db.commit()
    return RedirectResponse(f"/admin/cases/{case_id}", status_code=303)


@app.get("/admin/cases/{case_id}/screenshot/{idx}", dependencies=[Depends(require_admin)])
def admin_case_screenshot(case_id: int, idx: int):
    with SessionLocal() as db:
        c = db.get(Case, case_id)
        shots = (c.screenshots if c else None) or []
        if not c or idx < 0 or idx >= len(shots):
            raise HTTPException(status_code=404, detail="Capture introuvable")
        shot = shots[idx]
    try:
        data = base64.b64decode(shot["b64"])
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=404, detail="Capture illisible")
    return Response(content=data, media_type=shot.get("media_type", "image/png"))


@app.post("/admin/setup-greeting", dependencies=[Depends(require_admin)])
def admin_setup_greeting():
    """One-time: set the page's greeting text + Get Started button on Meta."""
    ok, detail = set_messenger_profile(
        settings.FB_PAGE_TOKEN, GREETING_TEXT, GET_STARTED_PAYLOAD,
        settings.FB_GRAPH_VERSION)
    title = "✅ Accueil Messenger configuré" if ok else "❌ Échec de la configuration"
    body = (
        "<p><a href='/admin/cases'>&larr; Retour aux dossiers</a></p>"
        f"<h2>{title}</h2>"
        "<p class='sub'>Salutation d'accueil + bouton « Get Started » envoyés à Meta. "
        "Ouvre la conversation avec ta page pour voir l'écran d'accueil, puis tape "
        "« Démarrer » pour déclencher le message de bienvenue.</p>"
        f"<pre>{escape(detail)}</pre>"
    )
    return _PAGE.format(body=body)


@app.post("/admin/cases/{case_id}/send", dependencies=[Depends(require_admin)])
async def admin_case_send(case_id: int, request: Request):
    """Agent sends a personalized message straight to the Messenger customer."""
    form = await request.form()
    message = (form.get("message") or "").strip()
    with SessionLocal() as db:
        c = db.get(Case, case_id)
        sender = c.sender_ref if c else None
    if sender and message and settings.FB_PAGE_TOKEN:
        ok = send_text(sender, message, settings.FB_PAGE_TOKEN, settings.FB_GRAPH_VERSION)
        log.info("Manual reply to case #%s: %s", case_id, "sent" if ok else "failed")
    return RedirectResponse(f"/admin/cases/{case_id}", status_code=303)


@app.post("/admin/reset", dependencies=[Depends(require_admin)])
def admin_reset():
    """Wipe ALL cases. Disabled unless ALLOW_RESET is set (testing only)."""
    if not settings.ALLOW_RESET:
        raise HTTPException(status_code=403,
                            detail="Reset désactivé. Mettre ALLOW_RESET=1 pour activer.")
    with SessionLocal() as db:
        if engine.dialect.name == "postgresql":
            db.execute(text("TRUNCATE TABLE cases RESTART IDENTITY"))
        else:
            db.query(Case).delete()
        db.commit()
    log.info("All cases wiped via /admin/reset")
    return RedirectResponse("/admin/cases", status_code=303)
