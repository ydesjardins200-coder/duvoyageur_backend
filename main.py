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

import logging
import urllib.request
from contextlib import asynccontextmanager
from html import escape

from fastapi import BackgroundTasks, Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

from auth import require_admin
from config import settings
from db import STATUSES, Case, SessionLocal, init_db
from facebook import extract_messages, send_text, valid_signature, verify_challenge
from parser import parse_trip
from trip_schema import TripRequest

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
        case = Case(
            channel=channel,
            status="needs_info" if trip.needs_clarification else "new",
            sender_ref=sender_ref,
            customer_email=trip.customer_email,
            parse_confidence=trip.parse_confidence,
            raw_message=trip.raw_message,
            trip=trip.model_dump(),
            needs_clarification=trip.needs_clarification or trip.missing_core_fields(),
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
def _ack_message(trip: TripRequest) -> str:
    """Customer-facing acknowledgment. Only ever asks for clean, core fields."""
    missing = trip.missing_core_fields()
    if missing:
        return ("Merci! On a bien reçu ton message. 🌴 Pour te trouver le meilleur "
                "rabais, peux-tu me confirmer : " + ", ".join(missing) + "?")
    return ("Merci! On a bien reçu ton forfait. 🌴 On regarde ça et on te revient "
            "par courriel avec ton rabais. 👍")


def process_messenger_message(sender: str | None, text: str, image_urls: list[str]) -> None:
    image_bytes, media_type = (None, "image/png")
    if image_urls:
        image_bytes, mt = _download_image(image_urls[0])
        media_type = mt or "image/png"
    try:
        trip = parse_trip(text, image_bytes=image_bytes, image_media_type=media_type)
    except Exception as e:  # noqa: BLE001
        # Never drop a customer message — store a fallback the agent can rescue.
        log.exception("Parse failed; storing fallback case: %s", e)
        trip = TripRequest(raw_message=text, source="messenger",
                           agent_notes=f"Parsing automatique échoué: {e}",
                           needs_clarification=["à traiter manuellement"])
    store_case("messenger", trip, sender_ref=sender)

    # Acknowledge the customer (keeps us inside Meta's 24h window). Optional:
    # only fires if a page token is set, and can never break this flow.
    if sender and settings.FB_PAGE_TOKEN:
        sent = send_text(sender, _ack_message(trip), settings.FB_PAGE_TOKEN,
                         settings.FB_GRAPH_VERSION)
        log.info("Ack reply to %s: %s", sender, "sent" if sent else "not sent")


# --------------------------------------------------------------------------- #
# Public endpoints
# --------------------------------------------------------------------------- #
@app.get("/")
def health():
    return {"ok": True, "service": "duvoyageur-backend"}


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

    # ACK immediately; parsing happens after the response is sent.
    return PlainTextResponse("EVENT_RECEIVED")


@app.post("/intake")
def intake_form(trip: TripRequest):
    """The Netlify form posts a TripRequest JSON body here."""
    trip.source = trip.source or "form"
    if trip.parse_confidence == 0.0:
        trip.parse_confidence = 1.0  # human-entered data is trusted
    case_id = store_case("form", trip)
    return {"ok": True, "case_id": case_id,
            "searchable": trip.is_searchable(),
            "needs": trip.missing_core_fields()}


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
 .muted{{color:#9aa4b2}} code{{background:#171a21;padding:1px 5px;border-radius:4px}}
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
        where = escape(str(t.get("hotel_name_raw") or t.get("destination") or "—"))
        needs = ", ".join(c.needs_clarification or []) or "—"
        rows.append(
            f"<tr><td><a href='/admin/cases/{c.id}'>#{c.id}</a></td>"
            f"<td><span class='tag {c.status}'>{c.status}</span></td>"
            f"<td>{escape(c.channel)}</td>"
            f"<td>{where}</td>"
            f"<td>{c.parse_confidence:.2f}</td>"
            f"<td class='muted'>{escape(needs)}</td>"
            f"<td class='muted'>{c.created_at:%Y-%m-%d %H:%M}</td></tr>"
        )
    body = (
        "<table><tr><th>#</th><th>Statut</th><th>Canal</th><th>Hôtel / Dest.</th>"
        "<th>Conf.</th><th>À demander</th><th>Reçu</th></tr>"
        + ("".join(rows) or "<tr><td colspan='7' class='muted'>Aucun dossier.</td></tr>")
        + "</table>"
    )
    return _PAGE.format(body=body)


@app.get("/admin/cases/{case_id}", response_class=HTMLResponse,
         dependencies=[Depends(require_admin)])
def admin_case_detail(case_id: int):
    with SessionLocal() as db:
        c = db.get(Case, case_id)
        if not c:
            return HTMLResponse(_PAGE.format(body="<p>Introuvable.</p>"), status_code=404)
        t = c.trip or {}
        import json as _json
        opts = "".join(
            f"<option value='{s}'{' selected' if s == c.status else ''}>{s}</option>"
            for s in STATUSES
        )
        body = (
            f"<p><a href='/admin/cases'>&larr; Tous les dossiers</a></p>"
            f"<h2>Dossier #{c.id} <span class='tag {c.status}'>{c.status}</span></h2>"
            f"<p class='muted'>{escape(c.channel)} · {c.created_at:%Y-%m-%d %H:%M} · "
            f"confiance {c.parse_confidence:.2f}</p>"
            f"<p><b>Message original:</b><br><code>{escape(c.raw_message or '—')}</code></p>"
            f"<p><b>À demander au client:</b> {escape(', '.join(c.needs_clarification or []) or '—')}</p>"
            f"<form method='post' action='/admin/cases/{c.id}/status'>"
            f"<label>Statut: <select name='status'>{opts}</select></label> "
            f"<button>Mettre à jour</button></form>"
            f"<h3>TripRequest</h3><pre><code>{escape(_json.dumps(t, indent=2, ensure_ascii=False))}</code></pre>"
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
