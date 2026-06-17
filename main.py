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
GET  /admin                  -> /admin/cases
GET  /admin/cases            list of cases (Basic auth)
GET  /admin/cases/{id}       one case (Basic auth)
POST /admin/cases/{id}/status   update a case's status (Basic auth)

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
import time
import urllib.request
from contextlib import asynccontextmanager
from datetime import datetime
from html import escape

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from sqlalchemy import text

from auth import require_admin
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


# --------------------------------------------------------------------------- #
# Storage helper
# --------------------------------------------------------------------------- #
def store_case(channel: str, trip: TripRequest, sender_ref: str | None = None) -> int:
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


# --------------------------------------------------------------------------- #
# Admin panel (Basic auth)
# --------------------------------------------------------------------------- #
_PAGE = """<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Du Voyageur — Dossiers</title>
<style>
 body{{font:15px/1.5 system-ui,sans-serif;margin:0;background:#0f1115;color:#e6e6e6}}
 header{{padding:16px 20px;background:#171a21;border-bottom:1px solid #262b35}}
 h1{{font-size:18px;margin:0}} main{{padding:20px;max-width:1100px;margin:auto}}
 table{{width:100%;border-collapse:collapse;font-size:14px}}
 th,td{{text-align:left;padding:10px 12px;border-bottom:1px solid #262b35;vertical-align:top}}
 th{{color:#9aa4b2;font-weight:600}} a{{color:#7db4ff;text-decoration:none}}
 .tag{{display:inline-block;padding:2px 8px;border-radius:10px;font-size:12px;background:#262b35}}
 .new{{background:#1d3a5f}} .needs_info{{background:#5f491d}} .booked{{background:#1d5f33}}
 .quoted{{background:#3a1d5f}} .closed{{background:#33383f}}
 .muted{{color:#9aa4b2}} code{{background:#171a21;padding:1px 5px;border-radius:4px}}
 .grid2{{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:16px;margin:18px 0}}
 .card{{background:#171a21;border:1px solid #262b35;border-radius:14px;padding:16px 18px}}
 .card h3{{margin:0 0 12px;font-size:13px;letter-spacing:.06em;text-transform:uppercase;color:#7db4ff}}
 .kv{{display:flex;justify-content:space-between;gap:14px;padding:6px 0;border-bottom:1px solid #20242e}}
 .kv:last-child{{border-bottom:0}} .kv .k{{color:#9aa4b2}} .kv .v{{text-align:right;font-weight:500}}
 .sub{{font-size:12px;color:#9aa4b2;font-weight:400}}
 .chips{{display:flex;flex-wrap:wrap;gap:8px;margin-top:6px}}
 .chip{{background:#5f491d;color:#ffd9a8;padding:3px 10px;border-radius:999px;font-size:13px}}
 .ok{{color:#7be0a0;font-weight:600}}
 .meter{{height:10px;background:#20242e;border-radius:999px;overflow:hidden;margin:8px 0}}
 .meter > i{{display:block;height:100%;border-radius:999px}}
 .full{{grid-column:1 / -1}}
 select,button{{font-size:14px;padding:7px 10px;border-radius:8px;border:1px solid #2c3340;background:#0f1115;color:#e6e6e6}}
 button{{cursor:pointer;background:#1d3a5f;border-color:#274a73}}
 details summary{{cursor:pointer;color:#9aa4b2;margin-top:18px}}
 pre{{background:#0f1115;border:1px solid #262b35;border-radius:10px;padding:12px;overflow:auto}}
</style></head><body><header><h1>Du Voyageur — Dossiers</h1></header><main>{body}</main></body></html>"""


@app.get("/admin", dependencies=[Depends(require_admin)])
def admin_root():
    return RedirectResponse("/admin/cases")


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
        "<button style='background:#1d3a5f;color:#fff;border:0;padding:8px 14px;"
        "border-radius:8px;cursor:pointer'>Configurer l'accueil Messenger</button></form>"
        "<p class='sub'>Définit la salutation + le bouton « Get Started » de ta page (une seule fois).</p>"
    )
    if settings.ALLOW_RESET:
        body += (
            "<form method='post' action='/admin/reset' style='margin-top:24px' "
            "onsubmit=\"return confirm('Effacer TOUS les dossiers ? Action irréversible.')\">"
            "<button style='background:#5f1d1d;color:#fff;border:0;padding:8px 14px;"
            "border-radius:8px;cursor:pointer'>Vider tous les dossiers (test)</button></form>"
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
                f"style='max-width:100%;border-radius:10px;border:1px solid #262b35;margin-bottom:10px'></a>"
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
                "style='width:100%;font-family:inherit;font-size:14px;padding:10px;border-radius:10px;"
                "border:1px solid #2c3340;background:#0f1115;color:#e6e6e6'></textarea>"
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
