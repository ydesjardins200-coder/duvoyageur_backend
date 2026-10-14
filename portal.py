"""
portal.py
=========
Client-facing self-service portal ("espace client") with passwordless,
single-use magic-link auth — completely separate from the admin session.

How auth works
--------------
1. An agent (or an automated notification) issues a magic link for a client:
   `build_portal_login_url(client_id)` sets a fresh single-use nonce on the
   client and returns an absolute URL like
   `{PUBLIC_BASE_URL}/portail/login?token=<signed>`.
2. The client opens the link. `/portail/login` verifies the signed token
   (HMAC over SECRET_KEY, time-limited) AND that its nonce still matches the
   client's stored nonce. On success it clears the nonce (so the link can't be
   reused) and sets a signed `dv_portal` cookie scoped to `/portail`.
3. `/portail` reads that cookie and shows the client ONLY their own,
   client-safe data (no confidence scores, internal notes, or raw parser data).

Security notes
--------------
- The portal cookie is a DIFFERENT cookie (`dv_portal`, path=`/portail`) signed
  with a DIFFERENT salt than anything admin-related. A portal visitor can never
  reach `require_admin`, which checks the Starlette session's `admin` flag.
- Links are single-use (nonce) and short-lived (PORTAL_LINK_MAX_AGE). Issuing a
  new link overwrites the nonce, so only the most recent link works.
"""
from __future__ import annotations

import re
import secrets
import time
import logging
import unicodedata
from datetime import datetime
from html import escape

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from config import settings
from db import (Case, Client, SessionLocal, add_identity, find_client_by_identity, log_activity,
                normalize_email, normalize_phone, replace_primary_identity)
from trip_schema import TripRequest

import storage
from parser import parse_trip

router = APIRouter()
log = logging.getLogger("duvoyageur.portal")

PORTAL_COOKIE = "dv_portal"
HINT_COOKIE = "dv_portal_hint"          # non-authoritative "session present" flag
_COOKIE_PATH = "/portail"

_login_signer = URLSafeTimedSerializer(settings.SECRET_KEY, salt="dv-portal-login")
_session_signer = URLSafeTimedSerializer(settings.SECRET_KEY, salt="dv-portal-session")


# --------------------------------------------------------------------------- #
# Tokens & session
# --------------------------------------------------------------------------- #
def _issue_login_token(client_id: int, nonce: str) -> str:
    return _login_signer.dumps({"cid": client_id, "n": nonce})


def _read_login_token(token: str):
    """Return {'cid', 'n'} for a valid, unexpired token, else None."""
    try:
        return _login_signer.loads(token, max_age=settings.PORTAL_LINK_MAX_AGE)
    except (BadSignature, SignatureExpired, Exception):  # noqa: BLE001
        return None


def _make_session_value(client_id: int) -> str:
    return _session_signer.dumps({"cid": client_id})


def _set_session_cookie(resp, client_id: int) -> None:
    """Set/refresh the signed portal cookie (sliding expiry: each visit renews
    it, so an active client effectively stays logged in). Also sets a small
    non-authoritative 'hint' cookie (SameSite=None in prod) so the Netlify site,
    on another origin, can ask /portail/whoami whether to show 'Mon compte'."""
    resp.set_cookie(
        PORTAL_COOKIE, _make_session_value(client_id),
        max_age=settings.PORTAL_SESSION_MAX_AGE, path=_COOKIE_PATH,
        httponly=True, samesite="lax", secure=settings.SECURE_COOKIES)
    resp.set_cookie(
        HINT_COOKIE, _session_signer.dumps({"cid": client_id}),
        max_age=settings.PORTAL_SESSION_MAX_AGE, path=_COOKIE_PATH,
        httponly=True,
        samesite="none" if settings.SECURE_COOKIES else "lax",
        secure=settings.SECURE_COOKIES)


def current_portal_client_id(request: Request):
    """Client id from the signed portal cookie, or None if absent/invalid."""
    raw = request.cookies.get(PORTAL_COOKIE)
    if not raw:
        return None
    try:
        data = _session_signer.loads(raw, max_age=settings.PORTAL_SESSION_MAX_AGE)
        return int(data["cid"])
    except (BadSignature, SignatureExpired, KeyError, ValueError, Exception):  # noqa: BLE001
        return None


def _hint_client_id(request: Request):
    """Client id from the cross-site 'hint' cookie (rides SameSite=None), or
    None. Used where the SameSite=Lax session cookie isn't sent — i.e. the
    Netlify site asking /portail/whoami from another origin."""
    raw = request.cookies.get(HINT_COOKIE)
    if not raw:
        return None
    try:
        data = _session_signer.loads(raw, max_age=settings.PORTAL_SESSION_MAX_AGE)
        return int(data["cid"])
    except (BadSignature, SignatureExpired, KeyError, ValueError, Exception):  # noqa: BLE001
        return None


def _client_exists(cid) -> bool:
    """True iff a Client row still exists for cid. The single source of truth:
    a client wiped in the admin reads as gone everywhere, no stale flag."""
    if not cid:
        return False
    with SessionLocal() as db:
        return db.get(Client, cid) is not None


def _clear_session_cookies(resp):
    """Erase both portal cookies (auth + cross-site hint)."""
    resp.delete_cookie(PORTAL_COOKIE, path=_COOKIE_PATH)
    resp.delete_cookie(HINT_COOKIE, path=_COOKIE_PATH)
    return resp


def _logged_out(to: str = "/portail/connexion"):
    """Redirect that also clears a stale/zombie session (e.g. the client record
    was deleted in the admin). Self-heals the cookies so the visitor — and the
    cross-origin nav — stop seeing a phantom logged-in state."""
    return _clear_session_cookies(RedirectResponse(to, status_code=303))


def _gate(request: Request):
    """Guard for portal tabs. Returns (cid, None) when the visitor is logged in;
    otherwise (None, response) with a login redirect. There is deliberately NO
    identity/KYC gate: an incomplete profile only shows a soft banner."""
    cid = current_portal_client_id(request)
    if not cid:
        return None, RedirectResponse("/portail/connexion", status_code=303)
    with SessionLocal() as db:
        client = db.get(Client, cid)
        if not client:
            return None, _logged_out()
    return cid, None


def build_portal_login_url(client_id: int):
    """Set a fresh single-use nonce on the client and return an absolute magic
    link, or None if the client doesn't exist."""
    with SessionLocal() as db:
        client = db.get(Client, client_id)
        if not client:
            return None
        nonce = secrets.token_urlsafe(16)
        client.portal_nonce = nonce
        db.commit()
        token = _issue_login_token(client_id, nonce)
    return f"{settings.PUBLIC_BASE_URL}/portail/login?token={token}"


# --------------------------------------------------------------------------- #
# Client-safe view model
# --------------------------------------------------------------------------- #
# Friendly status: (label, css-class). Internal statuses collapse to client view.
_STATUS_FR = {
    "new": ("En préparation", "prep"),
    "needs_info": ("En préparation", "prep"),
    "quoted": ("Soumission prête", "quote"),
    "booked": ("Réservé", "booked"),
    "closed": ("Voyage terminé", "done"),
}


def _trip_where(t: dict) -> str:
    return str(t.get("hotel_name_raw") or t.get("destination") or "Ton voyage")


def _trip_dates(c, t: dict):
    dep = t.get("departure_date") or c.flight_depart
    ret = t.get("return_date") or c.flight_return
    if dep or ret:
        return f"{dep or '?'} → {ret or '?'}"
    return t.get("dates_raw")


def _trip_travelers(t: dict):
    a, k = t.get("num_adults"), t.get("num_children")
    bits = []
    if a:
        bits.append(f"{a} adulte" + ("s" if a > 1 else ""))
    if k:
        bits.append(f"{k} enfant" + ("s" if k > 1 else ""))
    return ", ".join(bits) if bits else None


_SITE = "https://duvoyageur.netlify.app"

# Destination keyword -> (hub slug, label). Ordered: more specific first, first
# substring match wins (matched against destination + hotel, accent-insensitive).
_HUBS = [
    ("punta cana", "voyage-punta-cana", "Punta Cana"),
    ("riviera maya", "voyage-cancun-riviera-maya", "Cancún / Riviera Maya"),
    ("cancun", "voyage-cancun-riviera-maya", "Cancún / Riviera Maya"),
    ("puerto plata", "voyage-puerto-plata-samana", "Puerto Plata / Samaná"),
    ("playa del carmen", "voyage-playa-del-carmen", "Playa del Carmen"),
    ("costa mujeres", "voyage-costa-mujeres", "Costa Mujeres"),
    ("cayo coco", "voyage-cayo-coco-cuba", "Cayo Coco"),
    ("cayo santa maria", "voyage-cayo-santa-maria-cuba", "Cayo Santa María"),
    ("cayo largo", "voyage-cayo-largo-cuba", "Cayo Largo"),
    ("cayo guillermo", "voyage-cayo-guillermo-cuba", "Cayo Guillermo"),
    ("montego bay", "voyage-montego-bay-jamaique", "Montego Bay"),
    ("ocho rios", "voyage-ocho-rios-jamaique", "Ocho Rios"),
    ("puerto vallarta", "voyage-puerto-vallarta", "Puerto Vallarta"),
    ("nuevo vallarta", "voyage-nuevo-vallarta-riviera-nayarit", "Nuevo Vallarta"),
    ("riviera nayarit", "voyage-nuevo-vallarta-riviera-nayarit", "Riviera Nayarit"),
    ("los cabos", "voyage-los-cabos", "Los Cabos"),
    ("juan dolio", "voyage-juan-dolio", "Juan Dolio"),
    ("cap cana", "voyage-cap-cana", "Cap Cana"),
    ("la romana", "voyage-la-romana", "La Romana"),
    ("playa blanca", "voyage-playa-blanca-panama", "Playa Blanca (Panama)"),
    ("saint-martin", "voyage-saint-martin", "Saint-Martin / Sint Maarten"),
    ("st-martin", "voyage-saint-martin", "Saint-Martin / Sint Maarten"),
    ("sint maarten", "voyage-saint-martin", "Saint-Martin / Sint Maarten"),
    ("turks", "voyage-turks-caicos", "Turks & Caicos"),
    ("caicos", "voyage-turks-caicos", "Turks & Caicos"),
    ("sainte-lucie", "voyage-sainte-lucie", "Sainte-Lucie"),
    ("sainte lucie", "voyage-sainte-lucie", "Sainte-Lucie"),
    ("negril", "voyage-negril-jamaique", "Negril"),
    ("nassau", "voyage-nassau-bahamas", "Nassau"),
    ("freeport", "voyage-freeport-bahamas", "Freeport"),
    ("varadero", "voyage-varadero-cuba", "Varadero"),
    ("holguin", "voyage-holguin-cuba", "Holguín"),
    ("trinidad", "voyage-trinidad-cuba", "Trinidad"),
    ("bayahibe", "voyage-bayahibe", "Bayahíbe"),
    ("samana", "voyage-samana", "Samaná"),
    ("cozumel", "voyage-cozumel", "Cozumel"),
    ("huatulco", "voyage-huatulco", "Huatulco"),
    ("cabo", "voyage-los-cabos", "Los Cabos"),
    ("costa rica", "voyage-costa-rica", "Costa Rica"),
    ("guanacaste", "voyage-costa-rica", "Costa Rica"),
    ("aruba", "voyage-aruba", "Aruba"),
    ("curacao", "voyage-curacao", "Curaçao"),
    ("antigua", "voyage-antigua", "Antigua"),
    ("grenade", "voyage-grenade", "Grenade"),
    ("carthagene", "voyage-carthagene-colombie", "Carthagène"),
    ("cartagena", "voyage-carthagene-colombie", "Carthagène"),
    ("belize", "voyage-belize", "Belize"),
    ("roatan", "voyage-roatan-honduras", "Roatán"),
    ("panama", "voyage-playa-blanca-panama", "Playa Blanca (Panama)"),
]


def _norm(s) -> str:
    s = unicodedata.normalize("NFD", str(s or ""))
    return "".join(ch for ch in s if unicodedata.category(ch) != "Mn").lower()


def _trip_hub(t: dict):
    """Match a trip's destination/hotel to one of our destination hub pages."""
    hay = _norm(f"{t.get('destination') or ''} {t.get('hotel_name_raw') or ''}")
    for kw, slug, label in _HUBS:
        if kw in hay:
            return slug, label
    return None


def _dep_date(c, t: dict):
    raw = t.get("departure_date") or getattr(c, "flight_depart", None)
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw)[:10]).date()
    except (ValueError, TypeError):
        return None


def _countdown_html(c, t: dict) -> str:
    """A friendly countdown banner for a booked trip with a known departure date."""
    d = _dep_date(c, t)
    if not d:
        return ""
    days = (d - datetime.utcnow().date()).days
    if days > 1:
        return (f"<div class='cdown'><div class='cdown-n'>J-{days}</div>"
                f"<div class='cdown-l'>Plus que {days} jours avant le grand départ ✈️</div></div>")
    if days == 1:
        return ("<div class='cdown'><div class='cdown-n'>J-1</div>"
                "<div class='cdown-l'>C'est demain ! Prépare tes valises 🧳</div></div>")
    if days == 0:
        return ("<div class='cdown'><div class='cdown-n'>Jour J</div>"
                "<div class='cdown-l'>C'est aujourd'hui ! Bon voyage 🌴</div></div>")
    return ("<div class='cdown live'><div class='cdown-n'>🌴</div>"
            "<div class='cdown-l'>Bon voyage ! Profite bien de ton séjour.</div></div>")


def _resources_html(t: dict) -> str:
    """Guides + the destination hub relevant to this trip (shown once booked)."""
    links = [
        ("🛂", "Documents et passeport", f"{_SITE}/guide-documents-passeport"),
        ("🧳", "Quoi apporter — la checklist", f"{_SITE}/guide-quoi-apporter"),
        ("💱", "Convertisseur de devises", f"{_SITE}/guide-convertisseur-devises"),
        ("💸", "Pourboires au tout-inclus", f"{_SITE}/guide-pourboires"),
    ]
    hub = _trip_hub(t)
    if hub:
        slug, label = hub
        links.append(("📍", f"Tout savoir sur {label}", f"{_SITE}/{slug}"))
    links.append(("📅", "Météo & meilleure période", f"{_SITE}/guide-meilleure-periode"))
    links.append(("🛃", "Ce que tu peux rapporter au retour", f"{_SITE}/guide-limites-douane"))
    items = "".join(
        f"<a class='res-i' href='{escape(url)}' target='_blank' rel='noopener'>"
        f"<span class='res-ic'>{ic}</span><span>{escape(lab)}</span>"
        "<span class='res-go'>→</span></a>"
        for ic, lab, url in links)
    return ("<div class='res'><div class='res-h'>📚 Pour bien préparer ton voyage</div>"
            f"{items}</div>")


def _trip_card(c) -> str:
    t = c.trip or {}
    label, cls = _STATUS_FR.get(c.status, ("En cours", "prep"))
    where = escape(_trip_where(t))
    rows = []
    if t.get("hotel_name_raw") and t.get("destination"):
        rows.append(("Destination", escape(str(t.get("destination")))))
    dates = _trip_dates(c, t)
    if dates:
        rows.append(("Dates", escape(str(dates))))
    pax = _trip_travelers(t)
    if pax:
        rows.append(("Voyageurs", escape(pax)))
    meta = "".join(
        f"<div class='row'><span class='k'>{k}</span><span class='v'>{v}</span></div>"
        for k, v in rows)

    action = ""
    if c.status == "quoted":
        eco = (f"<div class='eco'>Tu économises <b>{escape(c.savings)}</b> 💸</div>"
               if c.savings else "")
        btn = (f"<a class='btn' href=\"{escape(c.quote_url)}\" target='_blank' "
               "rel='noopener'>Voir ma soumission →</a>" if c.quote_url else "")
        action = f"{eco}{btn}"
    elif c.status == "booked":
        ref = (f"<div class='row'><span class='k'>Confirmation</span>"
               f"<span class='v'>{escape(c.booking_ref)}</span></div>"
               if c.booking_ref else "")
        eco = (f"<div class='eco'>Économie réalisée : <b>{escape(c.savings)}</b> 🎉</div>"
               if c.savings else "")
        cd = _countdown_html(c, t)
        res = _resources_html(t)
        action = (f"{cd}{ref}{eco}<div class='muted'>Confirmation détaillée envoyée "
                  f"par Tripbook. Bon voyage ! 🌴</div>{res}")
    elif c.status == "closed":
        action = "<div class='muted'>Voyage terminé — merci de voyager avec nous ! 🙌</div>"
    else:  # new / needs_info
        action = "<div class='muted'>On prépare ta meilleure offre — on te revient très vite. ✈️</div>"

    return (
        "<div class='tcard'>"
        f"<div class='tchdr'><h3>{where}</h3><span class='badge {cls}'>{label}</span></div>"
        f"{meta}{action}"
        "</div>")


def _review_block(c) -> str:
    """Either the submitted review (read-only) or a star-rating form for a
    finished trip."""
    rev = c.review or {}
    if rev.get("rating"):
        n = int(rev["rating"])
        stars = "★" * n + "☆" * (5 - n)
        txt = escape(rev.get("text") or "")
        body = f"<p class='rv-txt'>{txt}</p>" if txt else ""
        return (f"<div class='rv'><div class='rv-head'>Ton avis "
                f"<span class='rv-stars on'>{stars}</span></div>{body}</div>")
    radios = "".join(
        f"<input type='radio' id='r{c.id}_{n}' name='rating' value='{n}' required>"
        f"<label for='r{c.id}_{n}' title='{n}/5'>★</label>"
        for n in range(5, 0, -1))
    return (
        f"<form class='rv rvform' method='post' action='/portail/voyage/{c.id}/avis'>"
        "<div class='rv-head'>Comment s'est passé ton voyage ?</div>"
        f"<div class='stars'>{radios}</div>"
        "<textarea name='text' rows='2' placeholder='Raconte-nous (optionnel)…'></textarea>"
        "<label class='consent'><input type='checkbox' name='consent' value='1' checked>"
        "<span>J'autorise Du Voyageur à utiliser mon avis pour améliorer son service.</span></label>"
        "<button class='btn small' type='submit'>Publier mon avis</button>"
        "</form>")


def _review_card(c) -> str:
    where = escape(_trip_where(c.trip or {}))
    return (f"<div class='tcard'><div class='tchdr'><h3>{where}</h3>"
            "<span class='badge done'>Voyage terminé</span></div>"
            f"{_review_block(c)}</div>")


def _avis_page(client, cases, flash: str = "") -> str:
    closed = [c for c in cases if (c.kind or "trip") == "trip" and c.status == "closed"]
    note = f"<div class='note ok'>{flash}</div>" if flash else ""
    intro = ("<div class='hello'><h2>Mes avis</h2>"
             "<p class='lede'>Donne ton avis sur tes voyages passés. Avec ta "
             "permission, on s'en sert pour améliorer continuellement notre "
             "service. 🙌</p></div>")
    if not closed:
        cards = ("<div class='tcard empty'>Tes avis apparaîtront ici une fois "
                 "tes voyages terminés. 🌴</div>")
    else:
        cards = f"<div class='tgrid'>{''.join(_review_card(c) for c in closed)}</div>"
    return note + intro + cards


def _unread(client) -> int:
    return sum(1 for n in (client.notifications or []) if not n.get("read"))


def _notif_item(n) -> str:
    """One notification row. Support notifications (href -> a thread) open the
    case in a modal (data-case) instead of navigating; others stay plain links."""
    href = n.get("href") or ""
    text = escape(n.get("text") or "")
    when = _short_dt(n.get("at"))
    cls = "nf" if n.get("read") else "nf un"
    inner = f"<div class='nf-t'>{text}</div><div class='nf-at'>{when}</div>"
    m = re.match(r"/portail/service#thread-(\d+)", href)
    if m:
        return f"<button type='button' class='{cls}' data-case='{m.group(1)}'>{inner}</button>"
    if href:
        return f"<a class='{cls}' href='{escape(href)}'>{inner}</a>"
    return f"<div class='{cls}'>{inner}</div>"


def _bell_panel(client) -> str:
    """The bell with an inline dropdown of notifications — no page change. Newest
    first; support notifications open the case in a modal."""
    notifs = list(reversed(client.notifications or []))
    count = sum(1 for n in notifs if not n.get("read"))
    badge = f"<span class='bdot'>{count if count < 10 else '9+'}</span>" if count else ""
    items = ("".join(_notif_item(n) for n in notifs[:30]) if notifs
             else "<div class='nf-empty'>Aucune notification pour l'instant.</div>")
    return (
        "<div class='bell'>"
        f"<button type='button' class='bell-btn' id='pbellBtn' "
        f"aria-label='Notifications'>🔔{badge}</button>"
        "<div class='bell-panel' id='pbellPanel'>"
        f"<div class='nf-head'>Notifications</div>{items}</div></div>")


_MONEY_RE = re.compile(r"(\d[\d\s\u202f,.]*)")


def _parse_money(s) -> float:
    """Pull a numeric amount out of a savings string like '195 $' or '1 200,50$'."""
    if not s:
        return 0.0
    m = _MONEY_RE.search(str(s))
    if not m:
        return 0.0
    raw = m.group(1).replace("\u202f", "").replace(" ", "")
    # If both separators present, the last one is the decimal sep.
    if "," in raw and "." in raw:
        raw = raw.replace("." if raw.rfind(",") > raw.rfind(".") else ",", "")
        raw = raw.replace(",", ".")
    else:
        raw = raw.replace(",", ".")
    try:
        return float(raw)
    except ValueError:
        return 0.0


def _savings_total(cases) -> float:
    """Sum realized savings across booked + completed trips."""
    total = 0.0
    for c in cases:
        if (c.kind or "trip") == "trip" and c.status in ("booked", "closed"):
            total += _parse_money(c.savings)
    return total


def _fmt_money(v: float) -> str:
    return (f"{v:,.0f}".replace(",", "\u202f") if v == int(v)
            else f"{v:,.2f}".replace(",", "\u202f").replace(".", ",")) + " $"


def _identity_card(client) -> str:
    rows = []
    if client.display_name:
        rows.append(("Nom", escape(client.display_name)))
    if client.primary_email:
        rows.append(("Courriel", escape(client.primary_email)))
    body = "".join(f"<div class='idrow'><span class='ik'>{k}</span><span>{v}</span></div>"
                   for k, v in rows)
    return ("<div class='idcard'>"
            "<div class='idtop'><h3>Mon identité</h3>"
            "<a class='edit' href='/portail/profil'>Modifier</a></div>"
            f"{body}</div>")


def _accueil(client, cases, flash: str = "", chat: str = "") -> str:
    """Portal home = the AI travel agent, full screen (chatgpt.com layout).
    Trip/savings cards and the identity box were removed: bookings happen
    self-service in the Softvoyage engine and live in PC Voyages."""
    from portal_assistant import assistant_panel     # lazy: avoids import cycle
    note = f"<div class='note ok'>{flash}</div>" if flash else ""
    return note + assistant_panel(client, chat)


_MONTHS_FR = ["", "janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.",
              "août", "sept.", "oct.", "nov.", "déc."]


def _short_dt(iso) -> str:
    try:
        d = datetime.fromisoformat(iso)
        return f"{d.day} {_MONTHS_FR[d.month]} · {d.hour:02d}:{d.minute:02d}"
    except (ValueError, TypeError, IndexError):
        return ""


def _notifications_page(items) -> str:
    if not items:
        return ("<div class='hello'><h2>Notifications</h2></div>"
                "<div class='tcard empty'>Aucune notification pour l'instant. 🔔</div>")
    rows = []
    for n in items:
        text = escape(n.get("text") or "")
        at = _short_dt(n.get("at"))
        unread = "" if n.get("read") else " unread"
        inner = f"<div class='nf-t'>{text}</div><div class='nf-at'>{at}</div>"
        href = n.get("href")
        if href:
            rows.append(f"<a class='nf{unread}' href='{escape(href)}'>{inner}</a>")
        else:
            rows.append(f"<div class='nf{unread}'>{inner}</div>")
    return ("<div class='hello'><h2>Notifications</h2></div>"
            f"<div class='nflist'>{''.join(rows)}</div>")


# --------------------------------------------------------------------------- #
# Identity / KYC
# --------------------------------------------------------------------------- #
# (key, label, input type, required, half-width). email lives on the Client;
# everything else is stored in the `kyc` JSON blob. Signup is deliberately
# minimal: no phone, no postal address (older values stay in the DB untouched,
# they are just no longer asked for or shown). We never ask for or store
# passport / travel-document data.
_KYC_FIELDS = [
    ("legal_first_name", "Prénom", "text", True, True),
    ("legal_last_name", "Nom de famille", "text", True, True),
    ("email", "Courriel", "email", True, False),
    ("date_of_birth", "Date de naissance", "date", True, True),
]
_IDENTITY_KEYS = ("legal_first_name", "legal_last_name", "email", "date_of_birth")


def _kyc_value(client, key):
    if key == "email":
        return client.primary_email
    return (client.kyc or {}).get(key)


def kyc_status(client):
    """(filled, total, [missing labels]) over the required identity fields."""
    req = [f for f in _KYC_FIELDS if f[3]]
    missing = [label for (k, label, _t, _r, _h) in req if not _kyc_value(client, k)]
    return len(req) - len(missing), len(req), missing


def kyc_complete(client) -> bool:
    return not kyc_status(client)[2]


def _dob_field(value: str = "") -> str:
    """Three dropdowns (jj / mm / aaaa) for a date of birth — friendlier than the
    native mobile date picker. Posts dob_d / dob_m / dob_y; reconstruct with
    _dob_from_form()."""
    y = mm = dd = ""
    if value:
        p = str(value).split("-")
        if len(p) == 3 and all(p):
            y, mm, dd = p[0], p[1].zfill(2), p[2].zfill(2)
    days = "<option value=''>jj</option>" + "".join(
        f"<option value='{i:02d}'{' selected' if f'{i:02d}' == dd else ''}>{i:02d}</option>"
        for i in range(1, 32))
    months = "<option value=''>mm</option>" + "".join(
        f"<option value='{i:02d}'{' selected' if f'{i:02d}' == mm else ''}>{i:02d}</option>"
        for i in range(1, 13))
    cur = datetime.utcnow().year
    years = "<option value=''>aaaa</option>" + "".join(
        f"<option value='{yy}'{' selected' if str(yy) == y else ''}>{yy}</option>"
        for yy in range(cur, cur - 101, -1))
    return ("<div class='dobgrid'>"
            f"<select name='dob_d' aria-label='Jour' required>{days}</select>"
            f"<select name='dob_m' aria-label='Mois' required>{months}</select>"
            f"<select name='dob_y' aria-label='Année' required>{years}</select>"
            "</div>")


def _dob_from_form(form) -> str:
    d = (form.get("dob_d") or "").strip()
    m = (form.get("dob_m") or "").strip()
    y = (form.get("dob_y") or "").strip()
    return f"{y}-{m}-{d}" if (d and m and y) else ""


def _kyc_banner(client) -> str:
    done, total, missing = kyc_status(client)
    if not missing:
        return ""
    n = len(missing)
    return (
        "<a class='banner' href='/portail/profil'>"
        "<div class='banner-txt'><b>Complète ton profil</b>"
        f"<span>{n} champ{'s' if n > 1 else ''} à remplir ({escape(', '.join(missing).lower())}).</span></div>"
        "<span class='banner-cta'>Compléter →</span></a>")


def _profile_form(client, saved: bool = False) -> str:
    ok = "<div class='note ok'>Profil enregistré ✓</div>" if saved else ""

    def field(k, label, typ, req, half):
        val = escape(str(_kyc_value(client, k) or ""))
        miss = req and not _kyc_value(client, k)
        star = " <span class='req'>*</span>" if req else ""
        cls = "field half" if half else "field"
        if miss:
            cls += " miss"
        if k == "date_of_birth":
            return (f"<div class='{cls}'><label>{label}{star}</label>"
                    f"{_dob_field(_kyc_value(client, k) or '')}</div>")
        return (f"<div class='{cls}'><label>{label}{star}</label>"
                f"<input name='{k}' type='{typ}' value=\"{val}\" inputmode='"
                f"{'email' if typ == 'email' else ('tel' if typ == 'tel' else 'text')}'"
                f"{' required' if req else ''}></div>")

    def group(title, keys, sub=""):
        fields = "".join(field(*f) for f in _KYC_FIELDS if f[0] in keys)
        s = f" <span class='muted'>{sub}</span>" if sub else ""
        return (f"<div class='fset'><h3>{title}{s}</h3>"
                f"<div class='formgrid'>{fields}</div></div>")

    return (
        ok
        + "<form class='form' method='post' action='/portail/profil'>"
        + group("Identité", _IDENTITY_KEYS)
        + "<div class='actions'>"
          "<button class='btn block' type='submit'>Enregistrer mon profil</button>"
          "<a class='btn ghost' href='/portail'>Retour</a></div></form>")


# --------------------------------------------------------------------------- #
# New trip request
# --------------------------------------------------------------------------- #
_AIRPORTS = [("", "Choisir…"), ("YUL", "Montréal (YUL)"), ("YQB", "Québec (YQB)"),
             ("YOW", "Ottawa (YOW)"), ("YYZ", "Toronto (YYZ)"), ("AUTRE", "Autre")]
_CARRIERS = [("", "Je ne sais pas"), ("Transat", "Transat"), ("Sunwing", "Sunwing"),
             ("Vacances Air Canada", "Vacances Air Canada"),
             ("WestJet Vacations", "WestJet Vacations"), ("Autre", "Autre")]
_BASIS_OPTS = [("", "—"), ("per_person", "par personne"), ("total", "le total")]


def _opts(pairs, current=""):
    return "".join(f"<option value=\"{escape(v)}\"{' selected' if v == current else ''}>"
                   f"{escape(l)}</option>" for v, l in pairs)


def _new_trip_form() -> str:
    asel, csel, psel = _opts(_AIRPORTS), _opts(_CARRIERS), _opts(_BASIS_OPTS)
    upload = (
        "<div class='upload'>"
        "<input type='file' name='capture' id='capfile' accept='image/*' hidden>"
        "<label for='capfile' class='upbtn' id='uplabel'>📸 Envoie une capture de ton forfait"
        "<span>Le plus rapide : on lit l'hôtel, les dates, le prix et le transporteur "
        "pour toi. Touche pour choisir une image.</span></label>"
        "<div class='upprog' id='upprog' style='display:none'><div class='upbar' id='upbar'></div></div>"
        "<div class='upstatus' id='upstatus'></div></div>"
        "<div class='ordiv'><span>ou remplis les détails à la main</span></div>")
    form = (
        "<form class='form' method='post' action='/portail/nouveau-voyage' "
        "enctype='multipart/form-data'>" + upload
        + "<div class='fset'><h3>Ton forfait</h3><div class='formgrid'>"
        f"<div class='field half'><label>Aéroport de départ <span class='req'>*</span></label>"
        f"<select name='origin_airport' required>{asel}</select></div>"
        "<div class='field half'><label>Destination <span class='req'>*</span></label>"
        "<input name='destination' placeholder='ex. Punta Cana, République dominicaine…' required></div>"
        "<div class='field half'><label>Hôtel <span class='muted'>(si tu le connais)</span></label>"
        "<input name='hotel' placeholder='ex. Eurostars Grand Cayacoa'></div>"
        "<div class='field half'><label>Date de départ <span class='req'>*</span></label>"
        "<input name='depart' type='date' required></div>"
        "<div class='field half'><label>Date de retour</label>"
        "<input name='retour' type='date'></div>"
        "</div></div>"
        "<div class='fset'><h3>Voyageurs</h3><div class='formgrid'>"
        "<div class='field half'><label>Adultes <span class='req'>*</span></label>"
        "<input name='adults' type='number' min='1' value='2' inputmode='numeric' required></div>"
        "<div class='field half'><label>Enfants</label>"
        "<input name='children' type='number' min='0' value='0' inputmode='numeric'></div>"
        "<div class='field'><label>Âge des enfants</label>"
        "<input name='children_ages' placeholder='ex. 4, 9'></div>"
        "</div></div>"
        "<div class='fset'><h3>Prix &amp; voyagiste <span class='muted'>(si tu l'as)</span></h3>"
        "<div class='formgrid'>"
        "<div class='field half'><label>Prix trouvé</label>"
        "<input name='price' type='number' inputmode='decimal' placeholder='ex. 1450'></div>"
        f"<div class='field half'><label>Ce prix est…</label><select name='basis'>{psel}</select></div>"
        f"<div class='field half'><label>Transporteur</label><select name='operator'>{csel}</select></div>"
        "<div class='field half'><label>Lien du forfait</label>"
        "<input name='link' type='url' inputmode='url' placeholder='https://…'></div>"
        "<div class='field'><label>Précisions</label>"
        "<textarea name='notes' placeholder='préférences, occasion spéciale, budget…'></textarea></div>"
        "</div></div>"
        "<div class='actions'>"
        "<button class='btn block' type='submit'>Envoyer ma demande</button>"
        "<a class='btn ghost' href='/portail'>Annuler</a></div>"
        "</form>")
    return form + _CAPTURE_JS


_CAPTURE_JS = """<script>
(function(){
  var f=document.getElementById('capfile'),prog=document.getElementById('upprog'),
      bar=document.getElementById('upbar'),st=document.getElementById('upstatus'),
      lbl=document.getElementById('uplabel');
  if(!f)return;
  function setv(n,v){if(v==null||v==='')return;var e=document.querySelector('[name="'+n+'"]');if(e&&!e.value)e.value=v;}
  function setsel(n,v){var e=document.querySelector('select[name="'+n+'"]');if(!e||v==null)return;for(var i=0;i<e.options.length;i++){if(e.options[i].value===v){e.selectedIndex=i;return;}}}
  f.addEventListener('change',function(){
    var file=f.files&&f.files[0];if(!file)return;
    prog.style.display='block';bar.style.width='0%';st.textContent='Lecture de ta capture…';lbl.classList.add('busy');
    var fd=new FormData();fd.append('file',file);
    var x=new XMLHttpRequest();x.open('POST','/portail/nouveau-voyage/capture');
    x.upload.onprogress=function(e){if(e.lengthComputable)bar.style.width=Math.round(e.loaded/e.total*60)+'%';};
    x.onload=function(){bar.style.width='100%';lbl.classList.remove('busy');
      try{var r=JSON.parse(x.responseText);if(r.ok&&r.trip){var t=r.trip;
        setv('destination',t.destination);setv('hotel',t.hotel_name_raw||t.hotel_name_normalized);
        setv('depart',t.departure_date);setv('retour',t.return_date);
        setv('adults',t.num_adults);setv('children',t.num_children);
        if(t.origin_airport_iata)setsel('origin_airport',t.origin_airport_iata);
        if(t.operator)setsel('operator',t.operator);
        if(t.price_seen&&t.price_seen.amount){setv('price',t.price_seen.amount);
          if(t.price_seen.basis==='per_person'||t.price_seen.basis==='total')setsel('basis',t.price_seen.basis);}
        st.textContent='Capture lue ✓ — vérifie et complète au besoin.';
      }else{st.textContent='Capture reçue. Complète les champs au besoin.';}
      }catch(e){st.textContent='Capture reçue. Complète les champs au besoin.';}
      setTimeout(function(){prog.style.display='none';},700);};
    x.onerror=function(){lbl.classList.remove('busy');prog.style.display='none';st.textContent='Upload impossible — remplis à la main.';};
    x.send(fd);});
})();
</script>"""


# --------------------------------------------------------------------------- #
# Service request
# --------------------------------------------------------------------------- #
def _service_form(cases) -> str:
    trips = [c for c in cases if (c.kind or "trip") == "trip"]
    rel = ""
    if trips:
        opts = "<option value=''>— Aucun en particulier —</option>" + "".join(
            f"<option value=\"{escape(_trip_where(c.trip or {}))}\">"
            f"{escape(_trip_where(c.trip or {}))}</option>" for c in trips)
        rel = ("<div class='field'><label>Concernant un voyage ?</label>"
               f"<select name='related'>{opts}</select></div>")
    return (
        "<form class='form' method='post' action='/portail/service'>"
        "<div class='fset'><h3>Ton message</h3><div class='formgrid'>"
        "<div class='field'><label>Sujet</label>"
        "<input name='subject' placeholder='ex. Changement de dates, question sur ma soumission…'></div>"
        + rel +
        "<div class='field'><label>Message <span class='req'>*</span></label>"
        "<textarea name='message' rows='4' placeholder='Décris ta demande…' required></textarea></div>"
        "</div></div>"
        "<div class='actions'>"
        "<button class='btn block' type='submit'>Envoyer ma demande</button>"
        "<a class='btn ghost' href='/portail'>Retour</a></div>"
        "</form>")


def _msg_bubble(m) -> str:
    out = m.get("dir") == "out"
    who = "Du Voyageur" if out else "Toi"
    at = _short_dt(m.get("at"))
    return (f"<div class='msg {'out' if out else 'in'}'>"
            f"<div class='msg-who'>{who} · {at}</div>"
            f"<div class='msg-b'>{escape(m.get('text') or '')}</div></div>")


def _service_stars_form(c) -> str:
    """Service feedback (1-5★) reusing Case.review. Shows the form once a request
    is resolved and not yet rated, or the submitted rating afterwards."""
    rev = c.review or {}
    if rev.get("rating"):
        n = int(rev["rating"])
        stars = "★" * n + "☆" * (5 - n)
        txt = escape(rev.get("text") or "")
        body = f"<p class='rv-txt'>{txt}</p>" if txt else ""
        return (f"<div class='rv'><div class='rv-head'>Ton évaluation "
                f"<span class='rv-stars on'>{stars}</span></div>{body}</div>")
    radios = "".join(
        f"<input type='radio' id='sf{c.id}_{n}' name='rating' value='{n}' required>"
        f"<label for='sf{c.id}_{n}' title='{n}/5'>★</label>"
        for n in range(5, 0, -1))
    return (
        f"<form class='rv rvform' method='post' action='/portail/service/{c.id}/feedback'>"
        "<div class='rv-head'>Comment évalues-tu notre service&nbsp;?</div>"
        f"<div class='stars'>{radios}</div>"
        "<textarea name='text' rows='2' placeholder='Un commentaire (optionnel)…'></textarea>"
        "<button class='btn small' type='submit'>Envoyer mon évaluation</button>"
        "</form>")


def _thread_foot(c) -> str:
    """The footer of one support thread: reply + resolve while open, or the
    feedback stars once resolved. Shared by the accordion and the modal."""
    if c.status == "resolved":
        return _service_stars_form(c)
    return (
        f"<form class='svc-reply' method='post' action='/portail/service/{c.id}/repondre'>"
        "<textarea name='message' rows='2' placeholder='Écris ta réponse…' "
        "required></textarea>"
        "<button class='btn small' type='submit'>Répondre</button></form>"
        f"<form class='svc-resolve' method='post' "
        f"action='/portail/service/{c.id}/resoudre'>"
        "<button class='btn ghost small' type='submit'>Marquer comme résolue</button>"
        "</form>")


def _thread_modal_html(c) -> str:
    """One support thread rendered for the in-page modal (no navigation): header,
    messages, and the reply/resolve or feedback footer."""
    msgs = c.messages or []
    resolved = c.status == "resolved"
    label, cls = ("Résolue", "done") if resolved else ("En cours", "quote")
    first = next((m.get("text") for m in msgs if m.get("dir") == "in"),
                 c.raw_message or "Demande de service")
    title = escape((first or "Demande de service").splitlines()[0][:60])
    bubbles = ("".join(_msg_bubble(m) for m in msgs)
               or "<div class='muted'>Demande envoyée. On te répond bientôt.</div>")
    return (
        "<div class='pm-head'>"
        f"<span class='pm-title'>{title}</span>"
        f"<span class='badge {cls}'>{label}</span></div>"
        f"<div class='msgs'>{bubbles}</div>{_thread_foot(c)}")


def _service_threads(cases) -> str:
    threads = sorted([c for c in cases if (c.kind or "") == "support"],
                     key=lambda x: x.created_at or datetime.min, reverse=True)
    if not threads:
        return ""
    out = ["<h3 class='acc-h'>Tes demandes</h3>"]
    for c in threads:
        msgs = c.messages or []
        first = next((m.get("text") for m in msgs if m.get("dir") == "in"),
                     c.raw_message or "Demande de service")
        preview = escape((first or "").splitlines()[0][:70])
        resolved = c.status == "resolved"
        label, cls = ("Résolue", "done") if resolved else ("En cours", "quote")
        bubbles = ("".join(_msg_bubble(m) for m in msgs)
                   or "<div class='muted'>Demande envoyée. On te répond bientôt.</div>")
        foot = _thread_foot(c)
        out.append(
            f"<details class='thread' id='thread-{c.id}'><summary>"
            f"<span class='th-sum'>{preview}</span>"
            f"<span class='badge {cls}'>{label}</span></summary>"
            f"<div class='msgs'>{bubbles}</div>{foot}</details>")
    out.append(
        "<script>(function(){var h=location.hash;"
        "if(h&&h.indexOf('#thread-')===0){var d=document.getElementById(h.slice(1));"
        "if(d){d.open=true;setTimeout(function(){d.scrollIntoView({block:'center'});},60);}}"
        "})();</script>")
    return "".join(out)


# --------------------------------------------------------------------------- #
# Shell + nav
# --------------------------------------------------------------------------- #
def _nav(active: str, locked: bool = False) -> str:
    home = f"<a class='pill psite' href='{_SITE}'>← Site principal</a>"
    if locked:                       # KYC incomplete -> no tabs, but still a way out
        return f"<nav class='pnav'>{home}</nav>"
    items = [("accueil", "/portail", "Accueil"),
             ("recherche", "/portail/recherche", "Rechercher"),
             ("aide", "/portail/service", "Aide"),
             ("profil", "/portail/profil", "Mon profil")]
    links = "".join(
        f"<a class='pill{' on' if k == active else ''}' href='{href}'>{label}</a>"
        for k, href, label in items)
    return f"<nav class='pnav'>{links}{home}</nav>"


# --------------------------------------------------------------------------- #
# Shell
# --------------------------------------------------------------------------- #
def _shell(title: str, body: str, logged_in: bool = False, nav: str = "",
           bell: str = "", full: bool = False) -> str:
    logout = ("<a class='logout' href='/portail/logout'>Déconnexion</a>"
              if logged_in else "")
    # The notification bell was retired with the admin queue (bookings and
    # support now live in PC Voyages / Messenger); `bell` is accepted and ignored.
    actions = f"<div class='topact'>{logout}</div>" if logged_in else logout
    return _PORTAL_PAGE.format(title=escape(title), body=body, logout=actions, nav=nav,
                               page=" chatpage" if full else "")


def _info_page(title: str, message: str, logged_in: bool = False) -> str:
    body = (f"<div class='infobox'><h2>{escape(title)}</h2>"
            f"<p>{message}</p></div>")
    return _shell(title, body, logged_in)


def _confirm_page(token: str, name) -> str:
    """Interstitial shown on the GET — a human clicks the button to POST and log
    in. Link-preview crawlers do GET only, so they never consume the token."""
    hello = f"Bonjour {escape(name)} 👋" if name else "Bienvenue 👋"
    body = (
        "<div class='infobox'>"
        f"<h2>{hello}</h2>"
        "<p>Clique ci-dessous pour ouvrir ton espace client en toute sécurité.</p>"
        "<form method='post' action='/portail/login' style='margin-top:18px'>"
        f"<input type='hidden' name='token' value=\"{escape(token)}\">"
        "<button class='btn' type='submit'>Accéder à mon espace →</button>"
        "</form></div>")
    return _shell("Connexion", body)


def _expired_page():
    return HTMLResponse(_info_page(
        "Lien expiré",
        "Ce lien n'est plus valide (il a peut-être expiré ou déjà été "
        "utilisé). Écris-nous sur Messenger et on t'en renvoie un tout neuf. 🙂"))


def _invalid_page():
    return HTMLResponse(_info_page(
        "Lien invalide",
        "Ce lien n'est plus valide. Écris-nous sur Messenger pour en "
        "recevoir un nouveau. 🙂"))


def _check_token(token: str):
    """Return (client_id, client_name) if the token is signed, unexpired AND its
    nonce still matches the client; else None. Does NOT consume the nonce."""
    data = _read_login_token(token) if token else None
    if not data:
        return None
    cid, nonce = data.get("cid"), data.get("n")
    with SessionLocal() as db:
        client = db.get(Client, cid) if cid else None
        if not client or not client.portal_nonce or not secrets.compare_digest(
                client.portal_nonce, nonce or ""):
            return None
        return cid, client.display_name


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@router.get("/portail/login", response_class=HTMLResponse)
def portal_login_confirm(request: Request, token: str = ""):
    """GET shows a confirmation page (no consumption) so link-preview bots that
    only fetch the URL can't burn the single-use token before the human clicks."""
    if not token or not _read_login_token(token):
        # Already logged in? Send them straight in. Otherwise it's a dead link.
        if current_portal_client_id(request):
            return RedirectResponse("/portail", status_code=303)
        return _expired_page()
    checked = _check_token(token)
    if not checked:
        if current_portal_client_id(request):
            return RedirectResponse("/portail", status_code=303)
        return _invalid_page()
    _cid, name = checked
    return HTMLResponse(_confirm_page(token, name))


@router.post("/portail/login")
async def portal_login_consume(request: Request):
    """POST consumes the single-use token (only a human clicking the button gets
    here), burns the nonce, and establishes the portal session."""
    form = await request.form()
    token = (form.get("token") or "").strip()
    if not token or not _read_login_token(token):
        return _expired_page()
    cid, nonce = (_read_login_token(token) or {}).get("cid"), (_read_login_token(token) or {}).get("n")
    with SessionLocal() as db:
        client = db.get(Client, cid) if cid else None
        if not client or not client.portal_nonce or not secrets.compare_digest(
                client.portal_nonce, nonce or ""):
            return _invalid_page()
        client.portal_nonce = None          # single use — burn it now
        db.commit()
    resp = RedirectResponse("/portail", status_code=303)
    _set_session_cookie(resp, cid)
    return resp


@router.get("/portail", response_class=HTMLResponse)
def portal_home(request: Request, new: int = 0, avis: int = 0, c: str = ""):
    cid = current_portal_client_id(request)
    if not cid:
        return RedirectResponse("/portail/connexion", status_code=303)
    flash = ""
    if new:
        flash = "Demande envoyée ✓ — on te revient bientôt avec ton rabais !"
    elif avis:
        flash = "Merci pour ton avis ✓"
    with SessionLocal() as db:
        client = db.get(Client, cid)
        if not client:
            return _logged_out()
        cases = list(client.requests)
        body = _kyc_banner(client) + _accueil(client, cases, flash=flash, chat=c)
        bell = _bell_panel(client)
    resp = HTMLResponse(_shell("Accueil", body, logged_in=True,
                               nav=_nav("accueil"), bell=bell, full=True))
    _set_session_cookie(resp, cid)          # sliding expiry on every visit
    return resp


@router.get("/portail/notifications", response_class=HTMLResponse)
def portal_notifications_retired(request: Request):
    # The notification bell was retired; old links land on the home page.
    return RedirectResponse("/portail", status_code=303)


def _legacy_portal_notifications(request: Request):
    cid, gate = _gate(request)
    if gate:
        return gate
    with SessionLocal() as db:
        client = db.get(Client, cid)
        notifs = list(client.notifications or [])
        if any(not n.get("read") for n in notifs):       # mark all read on view
            client.notifications = [{**n, "read": True} for n in notifs]
            db.commit()
            notifs = client.notifications
        items = list(reversed(notifs))                   # newest first
        body = _notifications_page(items)
    resp = HTMLResponse(_shell("Notifications", body, logged_in=True,
                               nav=_nav("accueil"), bell=_bell_panel(client)))
    _set_session_cookie(resp, cid)
    return resp


@router.get("/portail/profil", response_class=HTMLResponse)
def portal_profile(request: Request, saved: int = 0):
    cid = current_portal_client_id(request)
    if not cid:
        return RedirectResponse("/portail/connexion", status_code=303)
    with SessionLocal() as db:
        client = db.get(Client, cid)
        if not client:
            return _logged_out()
        lede = "Ces infos nous servent à réserver tes voyages au bon nom."
        body = (f"<div class='hello'><h2>Mon profil</h2><p class='lede'>{lede}</p></div>"
                + _profile_form(client, saved=bool(saved)))
        bell = _bell_panel(client)
    resp = HTMLResponse(_shell("Mon profil", body, logged_in=True,
                               nav=_nav("profil"), bell=bell))
    _set_session_cookie(resp, cid)
    return resp


@router.post("/portail/profil")
async def portal_profile_save(request: Request):
    cid = current_portal_client_id(request)
    if not cid:
        return RedirectResponse("/portail", status_code=303)
    form = await request.form()
    with SessionLocal() as db:
        client = db.get(Client, cid)
        if client:
            kyc = dict(client.kyc or {})
            for k, _label, _typ, _req, _half in _KYC_FIELDS:
                v = _dob_from_form(form) if k == "date_of_birth" else (form.get(k) or "").strip()
                if k == "email":
                    em = normalize_email(v)
                    if em:
                        client.primary_email = em
                        replace_primary_identity(db, client, "email", em)
                elif k == "date_of_birth" and not v:
                    continue            # never wipe a stored DOB with a blank form
                else:
                    kyc[k] = v or None
            client.kyc = kyc
            # Legal name is authoritative: it replaces the (often fake) Facebook
            # display name in the admin once the client submits it.
            full = " ".join(x for x in (kyc.get("legal_first_name"),
                                        kyc.get("legal_last_name")) if x)
            if full:
                client.display_name = full
            log_activity(db, cid, "note", "Identité mise à jour (espace client)", None)
            db.commit()
    return RedirectResponse("/portail/profil?saved=1", status_code=303)


@router.get("/portail/nouveau-voyage", response_class=HTMLResponse)
def portal_new_trip(request: Request):
    cid, gate = _gate(request)
    if gate:
        return gate
    with SessionLocal() as db:
        bell = _bell_panel(db.get(Client, cid))
    body = ("<div class='hello'><h2>Nouvelle demande de voyage</h2>"
            "<p class='lede'>Dis-nous ce que tu cherches — on retrouve le même "
            "forfait avec ton rabais. 🌴</p></div>" + _new_trip_form())
    resp = HTMLResponse(_shell("Nouveau voyage", body, logged_in=True,
                               nav=_nav("nouveau"), bell=bell))
    _set_session_cookie(resp, cid)
    return resp


@router.post("/portail/nouveau-voyage/capture")
async def portal_new_trip_capture(request: Request):
    """Parse an uploaded deal screenshot and return the extracted fields so the
    form can pre-fill itself (with an upload progress bar client-side). Reads
    only — nothing is saved here; the screenshot is stored on submit."""
    cid, gate = _gate(request)
    if gate:
        return JSONResponse({"ok": False, "error": "auth"}, status_code=403)
    form = await request.form()
    up = form.get("file")
    if up is None or not hasattr(up, "read"):
        return JSONResponse({"ok": False, "error": "no_file"})
    data = await up.read()
    media = getattr(up, "content_type", None) or "image/png"
    try:
        trip = parse_trip("(capture espace client)", images=[(data, media)])
        return JSONResponse({"ok": True, "trip": trip.model_dump()})
    except Exception as e:  # noqa: BLE001
        log.warning("Portal screenshot parse failed: %s", e)
        return JSONResponse({"ok": False, "error": "parse_failed", "trip": {}})


@router.post("/portail/nouveau-voyage")
async def portal_new_trip_save(request: Request):
    cid, gate = _gate(request)
    if gate:
        return gate
    form = await request.form()

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

    # Optional deal screenshot -> store it on the case (like the web form).
    shot = None
    up = form.get("capture")
    if up is not None and hasattr(up, "read"):
        try:
            data = await up.read()
            if data:
                shot = storage.make_screenshot(data, getattr(up, "content_type", None) or "image/png")
        except Exception as e:  # noqa: BLE001 — never lose a submission over storage
            log.warning("Portal capture store failed: %s", e)

    notes = g("notes")
    link = g("link")
    if link:
        notes = f"{notes}\nLien du forfait : {link}" if notes else f"Lien du forfait : {link}"

    airport = (form.get("origin_airport") or "").strip()
    operator = (form.get("operator") or "").strip() or None   # "" = Je ne sais pas

    with SessionLocal() as db:
        client = db.get(Client, cid)
        if not client:
            return RedirectResponse("/portail", status_code=303)
        d = {
            "source": "portail",
            "customer_name": client.display_name,
            "customer_email": client.primary_email,
            "customer_phone": client.primary_phone,
            "destination": g("destination"),
            "hotel_name_raw": g("hotel"),
            "departure_date": g("depart"),
            "return_date": g("retour"),
            "num_adults": gi("adults"),
            "num_children": gi("children"),
            "operator": operator,
            "raw_message": notes,
        }
        if airport in ("YUL", "YQB", "YOW", "YYZ"):
            d["origin_airport_iata"] = airport
        elif airport == "AUTRE":
            d["origin_city"] = "Autre (à préciser)"
        ages = [int(a) for a in re.split(r"[,\s]+", form.get("children_ages", "") or "")
                if a.strip().isdigit()]
        if ages:
            d["passengers"] = [{"age": a} for a in ages]
        amt = gf("price")
        if amt is not None:
            d["price_seen"] = {"amount": amt, "currency": "CAD",
                               "basis": (form.get("basis") or "").strip() or "unknown"}
        try:
            trip = TripRequest.model_validate({k: v for k, v in d.items() if v is not None})
        except Exception:  # noqa: BLE001
            return RedirectResponse("/portail/nouveau-voyage", status_code=303)
        rem = trip.remaining_fields()
        case = Case(
            client_id=client.id, channel="portal",
            status="new",                      # a fresh lead the agent must action
            customer_email=client.primary_email, customer_phone=client.primary_phone,
            parse_confidence=1.0, raw_message=notes,
            trip=trip.model_dump(), needs_clarification=rem,
            screenshots=[shot] if shot else [], messages=[])
        db.add(case)
        db.flush()
        log_activity(db, client.id, "request_created",
                     "Nouvelle demande via espace client"
                     + (" (avec capture)" if shot else ""), case.id)
        db.commit()
    return RedirectResponse("/portail?new=1", status_code=303)


@router.get("/portail/avis", response_class=HTMLResponse)
def portal_reviews_retired(request: Request):
    # "Mes avis" was retired (no more trip data in the backend).
    return RedirectResponse("/portail", status_code=303)


def _legacy_portal_avis(request: Request, ok: int = 0):
    cid, gate = _gate(request)
    if gate:
        return gate
    with SessionLocal() as db:
        client = db.get(Client, cid)
        cases = list(client.requests)
        flash = "Merci pour ton avis ✓" if ok else ""
        body = _avis_page(client, cases, flash=flash)
        bell = _bell_panel(client)
    resp = HTMLResponse(_shell("Mes avis", body, logged_in=True, nav=_nav("avis"), bell=bell))
    _set_session_cookie(resp, cid)
    return resp


@router.post("/portail/voyage/{case_id}/avis")
async def portal_review_save(case_id: int, request: Request):
    cid, gate = _gate(request)
    if gate:
        return gate
    form = await request.form()
    try:
        rating = int((form.get("rating") or "").strip())
    except ValueError:
        rating = 0
    text = (form.get("text") or "").strip()
    consent = bool(form.get("consent"))
    if rating < 1 or rating > 5:
        return RedirectResponse("/portail/avis", status_code=303)
    with SessionLocal() as db:
        case = db.get(Case, case_id)
        # Ownership + state guard: must be this client's own, finished trip.
        if (case and case.client_id == cid and case.status == "closed"
                and not (case.review or {}).get("rating")):
            case.review = {"rating": rating, "text": text or None, "consent": consent,
                           "at": datetime.utcnow().isoformat(timespec="seconds")}
            if consent:                     # remember the global permission too
                client = db.get(Client, cid)
                if client:
                    client.review_consent = True
            log_activity(db, cid, "note",
                         f"Avis client : {rating}/5 (espace client)", case_id)
            db.commit()
    return RedirectResponse("/portail/avis?ok=1", status_code=303)


@router.get("/portail/service", response_class=HTMLResponse)
def portal_service(request: Request, sent: int = 0):
    cid, gate = _gate(request)
    if gate:
        return gate
    with SessionLocal() as db:
        client = db.get(Client, cid)
        cases = list(client.requests)
        # Support now happens on Facebook Messenger (the admin ticket queue is
        # no longer worked). Older portal tickets stay readable below.
        old = _service_threads(cases) if any(
            (c.kind or "trip") == "support" for c in cases) else ""
        body = (
            "<div class='hello'><h2>Besoin d'aide ?</h2>"
            "<p class='lede'>Une question sur ta réservation, un changement, un pépin ? "
            "Écris-nous sur Messenger, un conseiller te répond.</p></div>"
            f"<a class='msgr' href=\"{escape(settings.MESSENGER_URL)}\" target='_blank' "
            "rel='noopener'>"
            "<svg width='26' height='26' viewBox='0 0 24 24' aria-hidden='true'><path fill='#fff' "
            "d='M12 2C6.36 2 2 6.13 2 11.7c0 2.91 1.19 5.44 3.14 7.17.16.15.26.35.27.57l.05 1.78a.8.8 "
            "0 0 0 1.12.71l1.98-.87a.8.8 0 0 1 .53-.04c.91.25 1.87.38 2.91.38 5.64 0 10-4.13 10-9.7S17.64 "
            "2 12 2zm6 7.46-2.94 4.66a1.5 1.5 0 0 1-2.17.4l-2.34-1.75a.6.6 0 0 0-.72 0l-3.16 2.4c-.42.32-.97-.18-.69-.63"
            "L8.92 9.88a1.5 1.5 0 0 1 2.17-.4l2.34 1.75c.21.16.51.16.72 0l3.16-2.4c.42-.32.97.18.69.63z'/></svg>"
            "<div><b>Écrire sur Messenger</b><span>Ouvre une conversation avec notre équipe.</span></div>"
            "<span class='sc-go'>→</span></a>"
            "<p class='muted' style='margin-top:14px'>Pour une question générale (destination, "
            "saison, bagages), ton <a href='/portail'>assistant voyage</a> répond tout de suite.</p>"
            + (("<h3 class='acc-h' style='margin-top:26px'>Tes anciennes demandes</h3>" + old) if old else ""))
        bell = _bell_panel(client)
    resp = HTMLResponse(_shell("Aide", body, logged_in=True, nav=_nav("aide"), bell=bell))
    _set_session_cookie(resp, cid)
    return resp


@router.post("/portail/service")
async def portal_service_save(request: Request):
    # The ticket form is gone (support moved to Messenger); a stale tab
    # posting here just lands on the Aide page.
    return RedirectResponse("/portail/service", status_code=303)


async def _legacy_portal_service_save(request: Request):  # kept for reference
    cid, gate = _gate(request)
    if gate:
        return gate
    form = await request.form()
    subject = (form.get("subject") or "").strip()
    message = (form.get("message") or "").strip()
    related = (form.get("related") or "").strip()
    if not message:
        return RedirectResponse("/portail/service", status_code=303)
    now = datetime.utcnow().isoformat(timespec="seconds")
    parts = []
    if subject:
        parts.append(f"Sujet : {subject}")
    if related:
        parts.append(f"Concernant : {related}")
    parts.append(message)
    body_text = "\n".join(parts)
    with SessionLocal() as db:
        client = db.get(Client, cid)
        if not client:
            return RedirectResponse("/portail", status_code=303)
        # Append to an open support case if one exists, else open a new one.
        case = (db.query(Case)
                .filter(Case.client_id == cid, Case.kind == "support",
                        Case.status != "resolved")
                .order_by(Case.created_at.desc()).first())
        if case is None:
            case = Case(
                client_id=cid, channel="portal", status="open", kind="support",
                raw_message=body_text,
                trip={"customer_name": client.display_name} if client.display_name else {},
                needs_clarification=[], screenshots=[],
                messages=[{"dir": "in", "text": body_text, "at": now}])
            db.add(case)
            db.flush()
            log_activity(db, cid, "request_created",
                         "Demande de service (espace client)", case.id)
        else:
            case.messages = (case.messages or []) + [{"dir": "in", "text": body_text, "at": now}]
            case.raw_message = (case.raw_message + "\n---\n" + body_text
                                if case.raw_message else body_text)
            log_activity(db, cid, "note", "Nouveau message de service (espace client)", case.id)
        case.awaiting_reply = True
        db.commit()
    return RedirectResponse("/portail/service?sent=1", status_code=303)


@router.post("/portail/service/{case_id}/repondre")
async def portal_service_reply(case_id: int, request: Request):
    """Client replies inside one specific support thread (reopens it if it had
    been resolved) and flags it for our attention."""
    cid, gate = _gate(request)
    if gate:
        return gate
    form = await request.form()
    message = (form.get("message") or "").strip()
    if not message:
        return RedirectResponse(f"/portail/service#thread-{case_id}", status_code=303)
    now = datetime.utcnow().isoformat(timespec="seconds")
    with SessionLocal() as db:
        case = db.get(Case, case_id)
        if not case or case.client_id != cid or case.kind != "support":
            return RedirectResponse("/portail/service", status_code=303)
        if case.status == "resolved":            # replying reopens the request
            case.status = "open"
        case.messages = (case.messages or []) + [{"dir": "in", "text": message, "at": now}]
        case.raw_message = (case.raw_message + "\n---\n" + message
                            if case.raw_message else message)
        case.awaiting_reply = True
        log_activity(db, cid, "note", "Réponse du client (espace client)", case.id)
        db.commit()
    return RedirectResponse(f"/portail/service?sent=1#thread-{case_id}", status_code=303)


@router.post("/portail/service/{case_id}/resoudre")
async def portal_service_resolve(case_id: int, request: Request):
    """Client marks their own support request as resolved."""
    cid, gate = _gate(request)
    if gate:
        return gate
    with SessionLocal() as db:
        case = db.get(Case, case_id)
        if not case or case.client_id != cid or case.kind != "support":
            return RedirectResponse("/portail/service", status_code=303)
        case.status = "resolved"
        case.awaiting_reply = False
        log_activity(db, cid, "status_change", "Demande résolue par le client", case.id)
        db.commit()
    return RedirectResponse(f"/portail/service#thread-{case_id}", status_code=303)


@router.post("/portail/service/{case_id}/feedback")
async def portal_service_feedback(case_id: int, request: Request):
    """Client rates our service (1-5★) on a resolved support request. Stored in
    Case.review, the same field trip reviews use."""
    cid, gate = _gate(request)
    if gate:
        return gate
    form = await request.form()
    try:
        rating = int((form.get("rating") or "").strip())
    except ValueError:
        rating = 0
    text = (form.get("text") or "").strip()
    if rating < 1 or rating > 5:
        return RedirectResponse(f"/portail/service#thread-{case_id}", status_code=303)
    now = datetime.utcnow().isoformat(timespec="seconds")
    with SessionLocal() as db:
        case = db.get(Case, case_id)
        if not case or case.client_id != cid or case.kind != "support":
            return RedirectResponse("/portail/service", status_code=303)
        case.review = {"rating": rating, "text": text[:1000], "at": now}
        log_activity(db, cid, "note", f"Évaluation service {rating}/5 (espace client)", case.id)
        db.commit()
    return RedirectResponse(f"/portail/service?sent=2#thread-{case_id}", status_code=303)


@router.get("/portail/service/{case_id}/fragment", response_class=HTMLResponse)
def portal_service_fragment(case_id: int, request: Request):
    """HTML of one support thread, served for the in-page modal (no navigation)."""
    cid, gate = _gate(request)
    if gate:
        return gate
    with SessionLocal() as db:
        case = db.get(Case, case_id)
        if not case or case.client_id != cid or case.kind != "support":
            return HTMLResponse("<div class='pm-load'>Demande introuvable.</div>",
                                status_code=404)
        return HTMLResponse(_thread_modal_html(case))


@router.post("/portail/notifications/read")
async def portal_notifications_read(request: Request):
    """Mark every notification read — called by JS when the bell dropdown opens."""
    cid, gate = _gate(request)
    if gate:
        return JSONResponse({"ok": False}, status_code=401)
    with SessionLocal() as db:
        client = db.get(Client, cid)
        if (client and client.notifications
                and any(not n.get("read") for n in client.notifications)):
            client.notifications = [{**n, "read": True} for n in client.notifications]
            db.commit()
    return JSONResponse({"ok": True})


@router.get("/portail/logout")
def portal_logout():
    # Log out and send the visitor back to the public site home (Netlify), not to
    # a standalone confirmation page. Both portal cookies are cleared on the way.
    return _clear_session_cookies(RedirectResponse(_SITE, status_code=303))


def _is_logged_in(request: Request) -> bool:
    """True only if the visitor has a portal session AND the underlying client
    record still exists. Cross-origin (Netlify) we only get the SameSite=None
    hint cookie; same-origin we also have the signed session cookie. Either way
    we resolve a cid and confirm it in the DB, so a client wiped in the admin
    reads as logged-out and the nav shows 'Connexion', not a dead 'Mon compte'."""
    cid = current_portal_client_id(request) or _hint_client_id(request)
    return _client_exists(cid)


@router.get("/portail/whoami")
def portal_whoami(request: Request):
    """Plain JSON login state (same-origin use / debugging)."""
    logged_in = _is_logged_in(request)
    resp = JSONResponse({"logged_in": logged_in})
    resp.headers["Cache-Control"] = "no-store"
    if not logged_in and (request.cookies.get(HINT_COOKIE)
                          or request.cookies.get(PORTAL_COOKIE)):
        _clear_session_cookies(resp)            # self-heal a zombie session
    return resp


_CB_RE = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]{0,40}")


@router.get("/portail/whoami.js")
def portal_whoami_js(request: Request, cb: str = "__dvAuth"):
    """JSONP: the Netlify site (another origin) loads this as a <script>. The
    SameSite=None hint cookie rides along cross-site, carrying a signed cid we
    verify against the DB — so the nav reflects reality (a deleted account shows
    'Connexion'). A stale cookie gets cleared here too, healing the zombie."""
    cb = cb if _CB_RE.fullmatch(cb or "") else "__dvAuth"
    logged_in = _is_logged_in(request)
    state = "true" if logged_in else "false"
    js = f"window.{cb}&&window.{cb}({state});"
    resp = Response(js, media_type="application/javascript",
                    headers={"Cache-Control": "no-store"})
    if not logged_in and (request.cookies.get(HINT_COOKIE)
                          or request.cookies.get(PORTAL_COOKIE)):
        _clear_session_cookies(resp)            # self-heal a zombie session
    return resp


# --------------------------------------------------------------------------- #
# Self-service re-login with email + date of birth (lower-assurance; rate-limited)
# --------------------------------------------------------------------------- #
_LOGIN_HITS: dict = {}          # ip -> [monotonic timestamps of failed attempts]
_LOGIN_WINDOW = 900             # 15 minutes
_LOGIN_MAX = 6                  # failed attempts before a temporary lockout


def _rate_ok(ip: str) -> bool:
    now = time.monotonic()
    hits = [t for t in _LOGIN_HITS.get(ip, []) if now - t < _LOGIN_WINDOW]
    _LOGIN_HITS[ip] = hits
    return len(hits) < _LOGIN_MAX


def _rate_hit(ip: str) -> None:
    _LOGIN_HITS.setdefault(ip, []).append(time.monotonic())


def _login_form(error: str = "") -> str:
    err = f"<div class='note err'>{escape(error)}</div>" if error else ""
    return (
        err
        + "<div class='hello'><h2>Connexion</h2>"
        "<p class='lede'>Accède à ton espace en un clic.</p></div>"
        + _social_buttons()
        + "<form class='form' method='post' action='/portail/connexion'>"
        "<div class='fset'><div class='formgrid'>"
        "<div class='field'><label>Courriel</label>"
        "<input name='email' type='email' inputmode='email' autocomplete='email' required></div>"
        "<div class='field'><label>Date de naissance</label>"
        f"{_dob_field()}</div>"
        "</div></div>"
        "<div class='actions'><button class='btn block' type='submit'>Me connecter</button></div>"
        "</form>"
        "<p class='muted' style='text-align:center;margin-top:16px'>Pas encore de compte ? "
        "<a href='/portail/inscription'>Crée-le en 1 minute</a>.<br>Tu arrives de "
        "Messenger ? Ouvre « 🔐 Mon espace client » dans le menu ☰ pour recevoir "
        "ton lien.</p>")


@router.get("/portail/connexion", response_class=HTMLResponse)
def portal_login_page(request: Request):
    if current_portal_client_id(request):
        return RedirectResponse("/portail", status_code=303)
    return HTMLResponse(_shell("Connexion", _login_form(), logged_in=False))


@router.post("/portail/connexion")
async def portal_login_credentials(request: Request):
    ip = request.client.host if request.client else "?"
    if not _rate_ok(ip):
        return HTMLResponse(_shell("Connexion", _login_form(
            "Trop de tentatives. Réessaie dans quelques minutes."), logged_in=False))
    form = await request.form()
    email = normalize_email((form.get("email") or "").strip())
    dob = _dob_from_form(form)
    cid = None
    if email and dob:
        with SessionLocal() as db:
            client = db.query(Client).filter(Client.primary_email == email).first()
            stored = str((client.kyc or {}).get("date_of_birth") or "") if client else ""
            if client and stored and secrets.compare_digest(stored, dob):
                cid = client.id
    if not cid:
        _rate_hit(ip)
        return HTMLResponse(_shell("Connexion", _login_form(
            "Courriel ou date de naissance invalide."), logged_in=False))
    resp = RedirectResponse("/portail", status_code=303)
    _set_session_cookie(resp, cid)
    return resp


# --------------------------------------------------------------------------- #
# Self-service signup — minimal: name, email, date of birth (or Google/Facebook)
# --------------------------------------------------------------------------- #
_SIGNUP_HITS: dict = {}
_SIGNUP_WINDOW = 3600           # 1 hour
_SIGNUP_MAX = 5                 # new accounts per hour per IP
SOCIAL_KINDS = ("google", "facebook")


def _signup_ok(ip: str) -> bool:
    now = time.monotonic()
    hits = [t for t in _SIGNUP_HITS.get(ip, []) if now - t < _SIGNUP_WINDOW]
    _SIGNUP_HITS[ip] = hits
    return len(hits) < _SIGNUP_MAX


def _signup_hit(ip: str) -> None:
    _SIGNUP_HITS.setdefault(ip, []).append(time.monotonic())


_G_ICON = ("<svg width='18' height='18' viewBox='0 0 48 48' aria-hidden='true'>"
           "<path fill='#FFC107' d='M43.6 20.5H42V20H24v8h11.3C33.7 32.7 29.2 36 24 36c-6.6 0-12-5.4-12-12s5.4-12 12-12c3.1 0 5.8 1.2 7.9 3l5.7-5.7C34 6.1 29.3 4 24 4 12.9 4 4 12.9 4 24s8.9 20 20 20 20-8.9 20-20c0-1.3-.1-2.4-.4-3.5z'/>"
           "<path fill='#FF3D00' d='M6.3 14.7l6.6 4.8C14.7 15.1 19 12 24 12c3.1 0 5.8 1.2 7.9 3l5.7-5.7C34 6.1 29.3 4 24 4 16.3 4 9.7 8.3 6.3 14.7z'/>"
           "<path fill='#4CAF50' d='M24 44c5.2 0 9.9-2 13.4-5.2l-6.2-5.2C29.2 35.1 26.7 36 24 36c-5.2 0-9.6-3.3-11.3-7.9l-6.5 5C9.5 39.6 16.2 44 24 44z'/>"
           "<path fill='#1976D2' d='M43.6 20.5H42V20H24v8h11.3c-.8 2.2-2.2 4.2-4.1 5.6l6.2 5.2C36.9 39.2 44 34 44 24c0-1.3-.1-2.4-.4-3.5z'/></svg>")
_F_ICON = ("<svg width='18' height='18' viewBox='0 0 24 24' aria-hidden='true'>"
           "<path fill='#fff' d='M24 12.07C24 5.4 18.63 0 12 0S0 5.4 0 12.07C0 18.1 4.39 23.1 10.13 24v-8.44H7.08v-3.49h3.04V9.41c0-3.02 1.79-4.69 4.53-4.69 1.31 0 2.68.24 2.68.24v2.97h-1.51c-1.49 0-1.96.93-1.96 1.89v2.25h3.33l-.53 3.49h-2.8V24C19.61 23.1 24 18.1 24 12.07z'/></svg>")


def _social_buttons(verb: str = "Continuer") -> str:
    """Google / Facebook buttons — each one only appears once its OAuth keys
    are configured on Railway, so nothing breaks before setup."""
    btns = []
    if settings.GOOGLE_CLIENT_ID and settings.GOOGLE_CLIENT_SECRET:
        btns.append("<a class='sbtn sg' href='/portail/auth/google'>"
                    f"{_G_ICON}<span>{verb} avec Google</span></a>")
    if settings.FB_LOGIN_APP_ID and settings.FB_LOGIN_APP_SECRET:
        btns.append("<a class='sbtn sf' href='/portail/auth/facebook'>"
                    f"{_F_ICON}<span>{verb} avec Facebook</span></a>")
    if not btns:
        return ""
    return ("<div class='social'>" + "".join(btns) + "</div>"
            "<div class='orsep'><span>ou avec ton courriel</span></div>")


def _signup_form(values=None, error: str = "") -> str:
    values = values or {}
    err = f"<div class='note err'>{escape(error)}</div>" if error else ""

    def field(k, label, typ, req, half):
        val = escape(str(values.get(k) or ""))
        cls = "field half" if half else "field"
        if k == "date_of_birth":
            return (f"<div class='{cls}'><label>{label}</label>"
                    f"{_dob_field(values.get(k) or '')}"
                    "<small class='hint'>Elle te sert à te reconnecter avec ton courriel.</small></div>")
        auto = {"legal_first_name": "given-name", "legal_last_name": "family-name",
                "email": "email"}.get(k, "on")
        return (f"<div class='{cls}'><label>{label}</label>"
                f"<input name='{k}' type='{typ}' value=\"{val}\" autocomplete='{auto}' "
                f"inputmode='{'email' if typ == 'email' else 'text'}' required></div>")

    fields = "".join(field(*f) for f in _KYC_FIELDS)
    return (
        err
        + "<div class='hello'><h2>Créer mon compte</h2>"
        "<p class='lede'>30 secondes et ton espace est prêt — tu pourras suivre "
        "tes voyages, tes soumissions et tes économies.</p></div>"
        + _social_buttons()
        + "<form class='form' method='post' action='/portail/inscription'>"
        f"<div class='fset'><div class='formgrid'>{fields}</div></div>"
        "<div class='actions'>"
        "<button class='btn block' type='submit'>Créer mon compte</button></div>"
        "</form>"
        "<p class='muted' style='text-align:center;margin-top:16px'>Déjà un compte ? "
        "<a href='/portail/connexion'>Connecte-toi</a></p>")


def _claimable(db, client) -> bool:
    """A record found by email may be 'claimed' by a signup only if it is a
    guest dossier (created by a /demande form or Messenger): no date of birth
    on file and no Google/Facebook login attached. Otherwise someone could take
    over a real account just by knowing its email."""
    if (client.kyc or {}).get("date_of_birth"):
        return False
    from db import ClientIdentity
    return not (db.query(ClientIdentity)
                .filter(ClientIdentity.client_id == client.id,
                        ClientIdentity.kind.in_(SOCIAL_KINDS)).first())


@router.get("/portail/inscription", response_class=HTMLResponse)
def portal_signup_page(request: Request, email: str = "", name: str = "",
                       prenom: str = "", nom: str = ""):
    if current_portal_client_id(request):
        return RedirectResponse("/portail", status_code=303)
    # Prefill from a /demande hand-off (?email=&name=…): a single "name" is split
    # into prénom/nom as a starting point the client can adjust.
    if name and not (prenom or nom):
        parts = name.split()
        prenom = parts[0] if parts else ""
        nom = " ".join(parts[1:]) if len(parts) > 1 else ""
    values = {"email": email.strip(),
              "legal_first_name": prenom.strip(), "legal_last_name": nom.strip()}
    values = {k: v for k, v in values.items() if v}
    return HTMLResponse(_shell("Inscription", _signup_form(values), logged_in=False))


@router.post("/portail/inscription")
async def portal_signup_save(request: Request):
    ip = request.client.host if request.client else "?"
    if not _signup_ok(ip):
        return HTMLResponse(_shell("Inscription", _signup_form(
            error="Trop de créations de compte. Réessaie dans une heure."), logged_in=False))
    form = await request.form()
    values = {k: (form.get(k) or "").strip() for k, *_ in _KYC_FIELDS}
    values["date_of_birth"] = _dob_from_form(form)
    email = normalize_email(values.get("email"))
    values["email"] = email or ""

    if not all(values.get(k) for k in _IDENTITY_KEYS):
        return HTMLResponse(_shell("Inscription", _signup_form(
            values, "Remplis tous les champs."), logged_in=False))

    with SessionLocal() as db:
        existing = (db.query(Client).filter(Client.primary_email == email).first()
                    or find_client_by_identity(db, "email", email))
        if existing and not _claimable(db, existing):
            return HTMLResponse(_shell("Inscription", _signup_form(
                values, "Ce courriel a déjà un compte. Connecte-toi plutôt."),
                logged_in=False))

        legal = " ".join(x for x in (values["legal_first_name"],
                                     values["legal_last_name"]) if x)
        if existing:
            # Guest dossier: claim it — fill the identity, keep its request(s).
            client = existing
            kyc = dict(client.kyc or {})
            kyc.update({k: values[k] for k in _IDENTITY_KEYS if k != "email"})
            client.kyc = kyc
            client.primary_email = email
            client.display_name = legal
            client.preferred_channel = client.preferred_channel or "email"
            replace_primary_identity(db, client, "email", email)
            log_activity(db, client.id, "note",
                         "Compte réclamé via inscription (dossier invité existant)")
        else:
            kyc = {k: values[k] for k in _IDENTITY_KEYS if k != "email"}
            client = Client(display_name=legal, primary_email=email,
                            preferred_channel="email", kyc=kyc)
            db.add(client)
            db.flush()
            add_identity(db, client, "email", email)
            log_activity(db, client.id, "request_created",
                         "Compte créé (inscription espace client)")
        db.commit()
        cid = client.id
    _signup_hit(ip)
    resp = RedirectResponse("/portail", status_code=303)
    _set_session_cookie(resp, cid)
    return resp


_PORTAL_PAGE = """<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#03121b">
<title>Du Voyageur — {title}</title>
<link rel="icon" type="image/png" href="/static/logo.png">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,700;12..96,800&family=Inter:wght@400;500;600&family=Space+Grotesk:wght@500;600&display=swap" rel="stylesheet">
<style>
 :root{{
   --abyss:#03121b;--deep:#0a3346;--pacific:#19d3e6;--lagoon:#3df0c5;
   --surf:#9bf6ec;--gold:#ffd23f;--foam:#eafcff;--mist:#94b8c6;
   --line:rgba(155,246,236,.16);--glow:rgba(25,211,230,.55);
   --field:rgba(3,18,27,.55);
 }}
 *{{box-sizing:border-box}}
 html{{-webkit-text-size-adjust:100%}}
 body{{margin:0;min-height:100vh;font-family:"Inter",system-ui,sans-serif;color:var(--foam);
   font-size:16px;line-height:1.5;-webkit-font-smoothing:antialiased;
   background:
     radial-gradient(90% 60% at 85% -8%, rgba(25,211,230,.18), transparent 60%),
     linear-gradient(180deg, rgba(3,18,27,.93), rgba(3,18,27,.98)),
     url("/static/login-bg.webp") center/cover fixed no-repeat;}}
 a{{color:var(--pacific)}}
 h1,h2,h3{{font-family:"Bricolage Grotesque",sans-serif;letter-spacing:-.02em}}
 :focus-visible{{outline:2px solid var(--pacific);outline-offset:2px;border-radius:6px}}
 /* Header */
 .top{{display:flex;align-items:center;gap:14px;flex-wrap:wrap;
   padding:14px 18px;padding-top:max(14px,env(safe-area-inset-top));
   border-bottom:1px solid var(--line);
   background:linear-gradient(180deg, rgba(8,33,47,.72), rgba(8,33,47,.35));
   backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);
   position:sticky;top:0;z-index:5}}
 .brand{{display:flex;align-items:center;gap:11px;min-width:0;flex:none}}
 .brand img{{width:40px;height:40px;border-radius:50%;box-shadow:0 0 0 1px var(--line);flex:none}}
 .brand b{{font-family:"Bricolage Grotesque",sans-serif;font-weight:800;font-size:17px;display:block;line-height:1.1}}
 .brand span{{font-family:"Space Grotesk",monospace;text-transform:uppercase;letter-spacing:.2em;
   font-size:10px;color:var(--pacific);display:block;margin-top:2px}}
 .logout{{font-size:13px;color:var(--mist);text-decoration:none;white-space:nowrap;
   padding:8px 10px;border-radius:10px}}
 .logout:hover,.logout:active{{color:var(--foam)}}
 .topact{{display:flex;align-items:center;gap:6px;margin-left:auto;flex:none}}
 @media(max-width:760px){{.top{{row-gap:10px}}
   .brand{{order:1}} .topact{{order:2}} .pnav{{order:3;flex-basis:100%}}}}
 .bell{{position:relative;text-decoration:none;font-size:20px;line-height:1;
   padding:8px 8px;border-radius:10px;display:inline-flex}}
 .bdot{{position:absolute;top:1px;right:0;min-width:17px;height:17px;padding:0 4px;
   border-radius:999px;background:#ff5a6e;color:#fff;font-size:10px;font-weight:700;
   font-family:"Space Grotesk",monospace;display:flex;align-items:center;justify-content:center;
   box-shadow:0 0 0 2px rgba(8,33,47,.9)}}
 /* Accueil */
 .idcard{{background:linear-gradient(180deg, rgba(20,62,82,.5), rgba(8,33,47,.6));
   border:1px solid var(--line);border-radius:18px;padding:18px 20px;margin-bottom:22px}}
 .idcard .idtop{{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:10px}}
 .idcard h3{{font-size:18px;margin:0}}
 .idcard .edit{{font-size:13px;color:var(--pacific);text-decoration:none;white-space:nowrap}}
 .idrow{{display:flex;gap:8px;font-size:14px;padding:5px 0;color:var(--foam)}}
 .idrow .ik{{color:var(--mist);min-width:88px;flex:none}}
 .savings{{display:flex;align-items:center;justify-content:space-between;gap:14px;
   background:linear-gradient(120deg, rgba(61,240,197,.14), rgba(25,211,230,.06));
   border:1px solid rgba(61,240,197,.4);border-radius:18px;padding:18px 22px;margin-bottom:22px}}
 .savings .sv-l{{color:var(--surf);font-size:13px;text-transform:uppercase;letter-spacing:.12em;font-family:"Space Grotesk",monospace}}
 .savings .sv-n{{font-family:"Bricolage Grotesque",sans-serif;font-weight:800;font-size:26px;color:var(--lagoon)}}
 .toprow{{display:flex;gap:14px;align-items:stretch;margin-bottom:22px}}
 .toprow .idcard{{flex:1 1 60%;margin-bottom:0}}
 .toprow .savings{{flex:1 1 40%;margin-bottom:0}}
 @media(max-width:640px){{.toprow{{flex-direction:column;gap:0}}
   .toprow .idcard{{margin-bottom:14px}}}}
 .searchcta{{display:flex;align-items:center;justify-content:space-between;gap:14px;
   padding:16px 20px;margin:0 0 18px;border-radius:18px;text-decoration:none;color:#03121b;
   background:linear-gradient(120deg,var(--pacific),var(--lagoon));
   box-shadow:0 14px 34px -14px var(--glow);transition:transform .15s}}
 .searchcta:hover{{transform:translateY(-1px)}}
 .searchcta b{{display:block;font-family:"Bricolage Grotesque",sans-serif;font-size:18px}}
 .searchcta span{{font-size:13px;opacity:.85}}
 .searchcta .sc-go{{font-size:24px;font-weight:800;opacity:1}}
 .assist{{background:linear-gradient(180deg, rgba(20,62,82,.5), rgba(8,33,47,.6));
   border:1px solid var(--line);border-radius:18px;padding:18px;margin-bottom:22px}}
 .a-head{{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;margin-bottom:12px}}
 .a-head h3{{margin:0;font-size:18px}}
 .a-sub{{font-size:12px;color:var(--mist)}}
 .areset{{background:none;border:1px solid var(--line);color:var(--mist);border-radius:999px;
   padding:6px 12px;font-size:12px;cursor:pointer;white-space:nowrap;font-family:inherit}}
 .areset:hover{{color:var(--foam)}}
 .a-log{{display:flex;flex-direction:column;gap:10px;max-height:min(52vh,460px);overflow-y:auto;
   padding:4px 2px 8px;scroll-behavior:smooth}}
 .msg{{max-width:85%;padding:11px 14px;border-radius:16px;font-size:15px;line-height:1.5;
   white-space:pre-wrap;word-wrap:break-word}}
 .msg.ai{{align-self:flex-start;background:rgba(6,33,47,.7);border:1px solid var(--line);
   border-bottom-left-radius:5px}}
 .msg.me{{align-self:flex-end;background:rgba(25,211,230,.18);border:1px solid rgba(25,211,230,.35);
   border-bottom-right-radius:5px}}
 .msg.typing::after{{content:"● ● ●";letter-spacing:2px;color:var(--mist);
   animation:blink 1.2s infinite}}
 @keyframes blink{{50%{{opacity:.3}}}}
 .msg.ai b{{color:var(--surf)}}
 .pcards{{margin:12px 0 2px;white-space:normal}}
 .pc-head{{font-family:"Bricolage Grotesque",sans-serif;font-weight:700;font-size:15px;margin-bottom:8px}}
 .pcard{{display:flex;gap:14px;padding:12px;border-radius:14px;background:rgba(3,18,27,.55);
   border:1px solid var(--line);margin-bottom:10px}}
 .pc-img{{width:132px;height:132px;flex:none;object-fit:cover;border-radius:11px;background:rgba(155,246,236,.06)}}
 .pc-body{{min-width:0;flex:1;display:flex;flex-direction:column;gap:3px}}
 .pc-name{{font-weight:700;font-size:16px;color:var(--foam);text-decoration:underline;
   text-decoration-color:rgba(155,246,236,.35);text-underline-offset:3px}}
 .pc-sub{{font-size:12.5px;color:var(--mist)}}
 .pc-price{{font-family:"Bricolage Grotesque",sans-serif;font-weight:800;font-size:21px;margin-top:4px}}
 .pc-price span{{font-size:14px;font-weight:600;color:var(--mist);margin-left:2px}}
 .pc-total{{font-size:14px}}
 .pc-total b{{color:var(--foam)}}
 .pc-foot{{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-top:6px}}
 .pc-src{{font-size:11.5px;color:var(--mist);text-decoration:none;border:1px solid var(--line);
   border-radius:999px;padding:2px 9px}}
 .pc-go{{font-size:13px;font-weight:600;color:#03121b;text-decoration:none;border-radius:999px;
   padding:5px 12px;background:linear-gradient(120deg,var(--pacific),var(--lagoon))}}
 .pc-head{{display:flex;align-items:center;justify-content:space-between;gap:10px}}
 .pc-cmpbtn{{background:none;border:1px solid rgba(25,211,230,.45);color:var(--pacific);border-radius:999px;
   padding:4px 12px;font:600 12px Inter,sans-serif;cursor:pointer}}
 .pc-cmpbtn:hover{{border-color:var(--pacific)}}
 .pc-cmp{{overflow-x:auto;margin:0 0 10px;border:1px solid var(--line);border-radius:12px;background:rgba(3,18,27,.55)}}
 .pc-cmp table{{border-collapse:collapse;width:100%;font-size:13px;min-width:420px}}
 .pc-cmp th,.pc-cmp td{{padding:7px 10px;text-align:left;border-bottom:1px solid var(--line);vertical-align:top}}
 .pc-cmp thead th{{font-family:"Bricolage Grotesque",sans-serif;font-size:13.5px;color:var(--foam)}}
 .pc-cmp tbody th{{color:var(--mist);font-weight:500;white-space:nowrap}}
 .pc-cmp tr:last-child th,.pc-cmp tr:last-child td{{border-bottom:0}}
 .pc-ours{{font-size:11.5px;color:var(--gold);text-decoration:none;border:1px solid rgba(255,210,63,.4);
   border-radius:999px;padding:2px 9px}}
 .guides{{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin-top:10px;white-space:normal}}
 .guides span{{font-size:11px;text-transform:uppercase;letter-spacing:.12em;color:var(--mist);
   font-family:"Space Grotesk",monospace;margin-right:2px}}
 .guides a{{font-size:12.5px;color:var(--foam);text-decoration:none;border-radius:999px;padding:4px 11px;
   background:rgba(61,240,197,.12);border:1px solid rgba(61,240,197,.35)}}
 .guides a:hover{{border-color:var(--lagoon)}}
 .rate{{display:flex;gap:6px;margin-top:10px}}
 .rbtn{{background:none;border:1px solid var(--line);border-radius:999px;padding:3px 10px;font-size:14px;
   cursor:pointer;opacity:.65;filter:grayscale(.6)}}
 .rbtn:hover,.rbtn.on{{opacity:1;filter:none;border-color:var(--pacific)}}
 .refine{{display:flex;flex-wrap:wrap;gap:8px;margin-top:10px;white-space:normal}}
 .a-acts{{display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end}}
 @media(max-width:560px){{.a-head{{flex-direction:column;align-items:stretch}}.a-acts{{justify-content:flex-start}}}}
 .a-toast{{margin:-4px 0 10px;padding:9px 12px;border-radius:11px;font-size:13.5px;
   background:rgba(61,240,197,.12);border:1px solid rgba(61,240,197,.4);color:var(--lagoon)}}
 .slot:empty{{display:none}}
 .slot{{white-space:normal;margin:6px 0 10px}}
 .dcards{{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:10px;margin:8px 0;white-space:normal}}
 .dcard{{border:1px solid var(--line);border-radius:14px;overflow:hidden;background:rgba(3,18,27,.55);display:flex;flex-direction:column}}
 .dc-pic{{margin:0;position:relative;height:130px;background:rgba(155,246,236,.06)}}
 .dc-pic img{{width:100%;height:100%;object-fit:cover;display:block}}
 .dc-pic figcaption{{position:absolute;left:6px;bottom:6px;font-size:10.5px;color:#fff;background:rgba(3,18,27,.7);
   padding:2px 7px;border-radius:999px}}
 .dc-nopic{{display:flex;align-items:center;justify-content:center;font-size:34px;
   background:linear-gradient(135deg,rgba(25,211,230,.18),rgba(61,240,197,.08))}}
 .dc-body{{padding:10px 12px 12px;position:relative;display:flex;flex-direction:column;gap:4px;flex:1}}
 .dc-rank{{position:absolute;top:-14px;right:10px;width:26px;height:26px;border-radius:50%;
   background:var(--pacific);color:#03121b;font-weight:800;font-size:13px;display:flex;align-items:center;justify-content:center}}
 .dc-name{{font-family:"Bricolage Grotesque",sans-serif;font-weight:800;font-size:17px}}
 .dc-hook{{font-size:13px;color:var(--surf);font-weight:600}}
 .dc-row{{font-size:12.5px;line-height:1.4}}
 .dc-row span{{color:var(--mist);margin-right:4px}}
 .dc-foot{{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin-top:auto;padding-top:6px}}
 .dc-guide{{font-size:12.5px;color:var(--pacific);text-decoration:none}}
 .dc-more{{font-size:12px;padding:5px 10px}}
 .slot .pc-head > span{{display:none}}
 .slot .pc-head{{justify-content:flex-end}}
 @media(max-width:560px){{
   .dcards{{grid-template-columns:none;grid-auto-flow:column;grid-auto-columns:82%;overflow-x:auto;
     scroll-snap-type:x mandatory;padding-bottom:6px;-webkit-overflow-scrolling:touch}}
   .dcard{{scroll-snap-align:start}}
   .dc-pic{{height:110px}}}}
 .qset{{margin-top:12px;padding:12px;border-radius:14px;border:1px solid rgba(25,211,230,.35);
   background:rgba(25,211,230,.06);white-space:normal}}
 .qhead{{font-family:"Bricolage Grotesque",sans-serif;font-weight:700;font-size:14px;margin-bottom:6px}}
 .qgrp{{margin:8px 0}}
 .qq{{font-size:13.5px;margin-bottom:6px}}
 .qopts{{display:flex;flex-wrap:wrap;gap:6px}}
 .qopt{{background:rgba(3,18,27,.6);border:1px solid var(--line);color:var(--foam);border-radius:999px;
   padding:7px 12px;font:500 13px Inter,sans-serif;cursor:pointer}}
 .qopt:hover{{border-color:var(--pacific)}}
 .qopt.on{{background:var(--pacific);color:#03121b;border-color:var(--pacific)}}
 .qsend{{margin-top:6px;font-size:13px;padding:8px 14px}}
 .qsend:disabled{{opacity:.5}}
 .pc-rate{{font-size:12.5px;color:var(--gold);margin-top:2px}}
 a.pc-cmpbtn{{text-decoration:none;display:inline-block;margin-left:6px}}
 .pc-conf{{font-size:12px;color:var(--gold);margin-top:2px}}
 @media(max-width:520px){{.pcard{{flex-direction:column}}.pc-img{{width:100%;height:170px}}}}
 .astatus{{font-size:13px;color:var(--mist);font-style:italic}}
 .astatus:empty{{display:none}}
 .srcs{{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin-top:10px;padding-top:9px;
   border-top:1px solid var(--line);white-space:normal}}
 .srcs span{{font-size:11px;text-transform:uppercase;letter-spacing:.12em;color:var(--mist);
   font-family:"Space Grotesk",monospace;margin-right:2px}}
 .srcs a{{font-size:12px;color:var(--pacific);text-decoration:none;max-width:220px;overflow:hidden;
   text-overflow:ellipsis;white-space:nowrap;border:1px solid var(--line);border-radius:999px;padding:3px 9px}}
 .srcs a:hover{{border-color:var(--pacific)}}
 .chips{{display:flex;flex-wrap:wrap;gap:8px;margin:6px 0 4px}}
 .chip{{background:rgba(6,33,47,.6);border:1px solid rgba(25,211,230,.35);color:var(--foam);
   border-radius:999px;padding:8px 13px;font-size:13px;cursor:pointer;font-family:inherit}}
 .chip:hover{{border-color:var(--pacific)}}
 .a-form{{display:flex;gap:8px;align-items:flex-end;margin-top:12px}}
 .a-form textarea{{flex:1;resize:none;min-height:48px;max-height:140px;padding:12px 14px;
   border-radius:14px;border:1px solid var(--line);background:var(--field);color:var(--foam);
   font:inherit;font-size:16px;line-height:1.4}}
 .a-form textarea:focus{{outline:none;border-color:var(--pacific)}}
 .a-send{{min-height:48px;flex:none}}
 .a-send:disabled{{opacity:.6}}
 .engine{{border-radius:18px;overflow:hidden;background:#fff;border:1px solid var(--line)}}
 .engine iframe{{display:block;width:100%;height:max(1100px,85vh);border:0}}
 .msgr{{display:flex;align-items:center;gap:14px;padding:18px 20px;border-radius:18px;
   text-decoration:none;color:#fff;background:linear-gradient(120deg,#0a7cff,#a334fa);
   box-shadow:0 14px 34px -14px rgba(10,124,255,.6);transition:transform .15s}}
 .msgr:hover{{transform:translateY(-1px)}}
 .msgr b{{display:block;font-family:"Bricolage Grotesque",sans-serif;font-size:18px}}
 .msgr span{{font-size:13px;opacity:.9}}
 .msgr .sc-go{{margin-left:auto;font-size:24px;font-weight:800;opacity:1}}
 .acc-h{{font-family:"Bricolage Grotesque",sans-serif;font-weight:700;font-size:15px;
   margin:24px 0 12px;color:var(--surf);letter-spacing:-.01em}}
 .acc-h:first-of-type{{margin-top:8px}}
 /* Notifications */
 .nflist{{display:grid;gap:10px}}
 .nf{{display:block;text-decoration:none;color:var(--foam);
   background:linear-gradient(180deg, rgba(20,62,82,.5), rgba(8,33,47,.6));
   border:1px solid var(--line);border-radius:14px;padding:14px 16px}}
 .nf.unread{{border-color:rgba(25,211,230,.5);background:linear-gradient(120deg, rgba(25,211,230,.1), rgba(8,33,47,.6))}}
 .nf-t{{font-size:14px;line-height:1.45}}
 .nf-at{{font-size:12px;color:var(--mist);margin-top:5px;font-family:"Space Grotesk",monospace}}
 /* Service threads (toggle) */
 .thread{{background:linear-gradient(180deg, rgba(20,62,82,.5), rgba(8,33,47,.6));
   border:1px solid var(--line);border-radius:14px;margin-bottom:10px;overflow:hidden}}
 .thread>summary{{list-style:none;cursor:pointer;display:flex;align-items:center;
   justify-content:space-between;gap:12px;padding:15px 17px;font-size:14px}}
 .thread>summary::-webkit-details-marker{{display:none}}
 .thread>summary::after{{content:'⌄';color:var(--mist);font-size:18px;transition:transform .2s}}
 .thread[open]>summary::after{{transform:rotate(180deg)}}
 .th-sum{{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
 .thread>summary .badge{{flex:none}}
 .msgs{{padding:4px 14px 16px;display:flex;flex-direction:column;gap:10px}}
 .msg{{max-width:88%;padding:10px 13px;border-radius:14px;font-size:14px;line-height:1.45}}
 .msg.in{{align-self:flex-start;background:rgba(155,246,236,.1);border:1px solid var(--line);border-bottom-left-radius:5px}}
 .msg.out{{align-self:flex-end;background:linear-gradient(120deg, rgba(25,211,230,.18), rgba(61,240,197,.12));
   border:1px solid rgba(61,240,197,.3);border-bottom-right-radius:5px}}
 .msg-who{{font-size:11px;color:var(--mist);margin-bottom:4px;font-family:"Space Grotesk",monospace}}
 .msg-b{{white-space:pre-wrap;word-break:break-word}}
 .svc-reply{{padding:0 14px 6px;display:flex;flex-direction:column;gap:8px}}
 .svc-reply textarea{{width:100%;font:inherit;font-size:16px;color:var(--foam);min-height:54px;
   padding:10px 12px;border-radius:12px;border:1px solid var(--line);
   background:rgba(8,33,47,.55);resize:vertical}}
 .svc-reply textarea:focus{{outline:none;border-color:var(--pacific);box-shadow:0 0 0 3px rgba(25,211,230,.18)}}
 .svc-reply .btn{{align-self:flex-start}}
 .svc-resolve{{padding:0 14px 14px}}
 /* Pill nav */
 .pnav{{flex:1 1 auto;min-width:0;display:flex;gap:8px;overflow-x:auto;
   -webkit-overflow-scrolling:touch;scrollbar-width:none}}
 .pnav::-webkit-scrollbar{{display:none}}
 .pill{{flex:none;font-size:14px;font-weight:600;text-decoration:none;color:var(--mist);
   padding:9px 16px;border-radius:999px;border:1px solid var(--line);
   background:rgba(8,33,47,.4)}}
 .pill.on{{color:#02161c;background:linear-gradient(120deg,var(--pacific),var(--lagoon));border-color:transparent}}
 .psite{{margin-left:auto;color:var(--pacific);border-color:rgba(25,211,230,.4);background:rgba(25,211,230,.08)}}
 .psite:hover,.psite:active{{color:#02161c;background:var(--pacific);border-color:transparent}}
 /* Layout */
 .wrap{{max-width:980px;margin:0 auto;padding:22px 18px 64px;
   padding-left:max(18px,env(safe-area-inset-left));padding-right:max(18px,env(safe-area-inset-right))}}
 .hello h2{{font-weight:800;font-size:26px;margin:6px 0 2px}}
 .lede{{color:var(--mist);margin:0 0 20px;font-size:14px}}
 /* Banner */
 .banner{{display:flex;align-items:center;justify-content:space-between;gap:14px;
   text-decoration:none;color:var(--foam);margin:0 0 18px;
   background:linear-gradient(120deg, rgba(255,210,63,.14), rgba(255,210,63,.06));
   border:1px solid rgba(255,210,63,.42);border-radius:16px;padding:15px 18px}}
 .banner-txt b{{display:block;font-family:"Bricolage Grotesque",sans-serif;font-weight:700;margin-bottom:2px}}
 .banner-txt span{{color:var(--mist);font-size:13px}}
 .banner-cta{{color:var(--gold);font-weight:600;white-space:nowrap;flex:none}}
 /* Trip cards */
 .tgrid{{display:grid;gap:16px}}
 .tcard{{background:linear-gradient(180deg, rgba(20,62,82,.5), rgba(8,33,47,.6));
   border:1px solid var(--line);border-radius:18px;padding:20px 22px;
   box-shadow:0 24px 60px -28px rgba(0,0,0,.8)}}
 .tcard.empty{{color:var(--mist);text-align:center}}
 .tchdr{{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:10px;flex-wrap:wrap}}
 .tchdr h3{{font-weight:700;font-size:19px;margin:0}}
 .badge{{font-family:"Space Grotesk",monospace;text-transform:uppercase;letter-spacing:.08em;
   font-size:11px;padding:5px 11px;border-radius:999px;white-space:nowrap}}
 .badge.prep{{background:rgba(148,184,198,.16);color:var(--surf)}}
 .badge.quote{{background:rgba(255,210,63,.16);color:var(--gold)}}
 .badge.booked{{background:rgba(61,240,197,.16);color:var(--lagoon)}}
 .badge.done{{background:rgba(148,184,198,.12);color:var(--mist)}}
 .row{{display:flex;justify-content:space-between;gap:14px;padding:8px 0;border-top:1px solid var(--line);font-size:14px}}
 .row .k{{color:var(--mist);flex:none}}
 .row .v{{font-weight:600;text-align:right;word-break:break-word}}
 .eco{{margin:12px 0 4px;font-size:15px}}
 .muted{{color:var(--mist);font-size:13px;margin-top:10px;font-weight:400}}
 /* Buttons */
 .btn{{display:inline-flex;align-items:center;justify-content:center;gap:6px;
   font-family:"Bricolage Grotesque",sans-serif;font-weight:700;font-size:15px;
   min-height:48px;padding:12px 22px;border-radius:999px;text-decoration:none;cursor:pointer;
   color:#02161c;border:0;background:linear-gradient(120deg,var(--pacific),var(--lagoon));
   box-shadow:0 12px 30px -12px var(--glow);transition:transform .15s,box-shadow .15s}}
 .btn:hover{{transform:translateY(-1px);box-shadow:0 16px 36px -12px var(--glow)}}
 .btn.block{{width:100%}}
 .btn.ghost{{background:transparent;color:var(--foam);border:1px solid var(--line);box-shadow:none}}
 .actions{{display:flex;gap:12px;margin-top:22px;flex-wrap:wrap}}
 .actions .btn{{flex:1;min-width:160px}}
 /* Forms */
 .form{{margin-top:6px}}
 .fset{{background:linear-gradient(180deg, rgba(20,62,82,.4), rgba(8,33,47,.5));
   border:1px solid var(--line);border-radius:18px;padding:18px 18px 20px;margin-bottom:16px}}
 .fset>h3{{font-size:15px;margin:0 0 14px;font-weight:700}}
 .fset>h3 .muted{{font-size:12px;margin:0}}
 .formgrid{{display:grid;grid-template-columns:1fr 1fr;gap:14px}}
 .field{{display:flex;flex-direction:column;min-width:0}}
 .field.half{{grid-column:span 1}}
 .field:not(.half){{grid-column:1 / -1}}
 .field label{{font-size:12px;color:var(--mist);margin:0 0 6px;font-weight:500}}
 .field .req{{color:var(--gold)}}
 .field input,.field select,.field textarea{{width:100%;font:inherit;font-size:16px;color:var(--foam);
   min-height:48px;padding:12px 13px;border-radius:12px;
   border:1px solid var(--line);background:var(--field);appearance:none;-webkit-appearance:none}}
 .field textarea{{min-height:96px;resize:vertical;line-height:1.5}}
 .field select{{background-image:linear-gradient(45deg,transparent 50%,var(--mist) 50%),linear-gradient(135deg,var(--mist) 50%,transparent 50%);
   background-position:calc(100% - 18px) 21px,calc(100% - 13px) 21px;background-size:5px 5px,5px 5px;background-repeat:no-repeat;padding-right:36px}}
 .field input::placeholder,.field textarea::placeholder{{color:#6f93a3}}
 .field input:focus,.field select:focus,.field textarea:focus{{outline:none;border-color:var(--pacific);box-shadow:0 0 0 3px rgba(25,211,230,.18)}}
 .field.miss input,.field.miss select{{border-color:rgba(255,210,63,.55)}}
 .dobgrid{{display:grid;grid-template-columns:1fr 1fr 1.35fr;gap:8px}}
 .dobgrid select{{padding:12px 26px 12px 11px;
   background-position:calc(100% - 13px) 21px,calc(100% - 8px) 21px}}
 .hint{{font-size:12px;color:var(--mist);margin:6px 0 0}}
 /* Screenshot upload */
 .upload{{margin-bottom:8px}}
 .upbtn{{display:flex;flex-direction:column;gap:5px;cursor:pointer;text-align:center;
   border:1.5px dashed rgba(155,246,236,.35);border-radius:16px;padding:24px 18px;
   background:rgba(8,33,47,.45);font-family:"Bricolage Grotesque",sans-serif;font-weight:700;
   font-size:15px;color:var(--foam);transition:border-color .15s,background .15s}}
 .upbtn span{{font-family:"Inter",sans-serif;font-weight:400;font-size:12px;color:var(--mist);line-height:1.45}}
 .upbtn:hover{{border-color:var(--pacific);background:rgba(25,211,230,.06)}}
 .upbtn.busy{{opacity:.65;border-style:solid}}
 .upprog{{height:8px;border-radius:999px;background:rgba(8,33,47,.8);overflow:hidden;margin:10px 0 0}}
 .upbar{{height:100%;width:0;border-radius:999px;
   background:linear-gradient(90deg,var(--pacific),var(--lagoon));transition:width .2s ease}}
 .upstatus{{font-size:12px;color:var(--surf);margin-top:8px;text-align:center;min-height:14px}}
 .ordiv{{display:flex;align-items:center;gap:12px;color:var(--mist);font-size:12px;margin:16px 0}}
 .ordiv::before,.ordiv::after{{content:'';flex:1;height:1px;background:var(--line)}}
 /* Progress */
 .prog{{height:8px;border-radius:999px;background:rgba(8,33,47,.7);overflow:hidden;margin:2px 0 6px}}
 .prog span{{display:block;height:100%;border-radius:999px;
   background:linear-gradient(90deg,var(--pacific),var(--lagoon));transition:width .4s ease}}
 .social{{display:flex;flex-direction:column;gap:10px;margin:0 0 6px}}
 .sbtn{{display:flex;align-items:center;justify-content:center;gap:10px;min-height:50px;
   border-radius:14px;font-weight:600;font-size:15px;text-decoration:none;
   transition:transform .15s, box-shadow .15s}}
 .sbtn:hover{{transform:translateY(-1px)}}
 .sbtn.sg{{background:#fff;color:#1f1f1f}}
 .sbtn.sf{{background:#1877F2;color:#fff}}
 .orsep{{display:flex;align-items:center;gap:12px;color:var(--mist);font-size:13px;margin:16px 0}}
 .orsep::before,.orsep::after{{content:"";flex:1;height:1px;background:var(--line)}}
 .hint{{display:block;color:var(--mist);font-size:12px;margin-top:6px}}
 .note{{border-radius:12px;padding:11px 14px;font-size:14px;margin:0 0 16px}}
 .note.ok{{background:rgba(61,240,197,.12);border:1px solid rgba(61,240,197,.4);color:var(--lagoon)}}
 .note.err{{background:rgba(255,90,110,.12);border:1px solid rgba(255,90,110,.45);color:#ff9aa6}}
 /* Reviews */
 .btn.small{{min-height:42px;padding:10px 18px;font-size:14px;margin-top:12px;width:auto}}
 .rv{{margin-top:12px;border-top:1px solid var(--line);padding-top:13px}}
 .rv-head{{font-size:13px;color:var(--mist);margin-bottom:8px}}
 .rv-stars.on{{color:var(--gold);letter-spacing:3px;font-size:15px}}
 .rv-txt{{margin:6px 0 0;font-size:14px;line-height:1.5}}
 .rvform textarea{{width:100%;font:inherit;font-size:16px;color:var(--foam);min-height:64px;
   padding:11px 12px;border-radius:12px;border:1px solid var(--line);background:var(--field);resize:vertical}}
 .rvform textarea:focus{{outline:none;border-color:var(--pacific);box-shadow:0 0 0 3px rgba(25,211,230,.18)}}
 .stars{{display:inline-flex;flex-direction:row-reverse;justify-content:flex-end;margin-bottom:10px}}
 .stars input{{position:absolute;width:1px;height:1px;opacity:0}}
 .stars label{{font-size:32px;line-height:1;color:rgba(155,246,236,.22);cursor:pointer;padding:2px 3px;transition:color .12s}}
 .stars label:hover,.stars label:hover ~ label,.stars input:checked ~ label{{color:var(--gold)}}
 .stars input:focus-visible + label{{outline:2px solid var(--pacific);outline-offset:2px;border-radius:5px}}
 .consent{{display:flex;gap:9px;align-items:flex-start;margin:12px 0 2px;font-size:13px;color:var(--mist);cursor:pointer}}
 .consent input{{width:20px;height:20px;margin:0;flex:none;accent-color:var(--lagoon)}}
 .consent span{{line-height:1.4}}
 /* Info / confirm pages */
 .infobox{{max-width:460px;margin:56px auto;text-align:center;
   background:linear-gradient(180deg, rgba(20,62,82,.5), rgba(8,33,47,.6));
   border:1px solid var(--line);border-radius:20px;padding:34px 26px}}
 .infobox h2{{font-weight:800;margin:0 0 10px}}
 .infobox p{{color:var(--mist);font-size:15px;line-height:1.55;margin:0}}
 .foot{{text-align:center;color:var(--mist);opacity:.7;font-size:11px;margin-top:34px}}
 /* Mobile */
 @media (max-width:560px){{
   .wrap{{padding:18px 15px 56px}}
   .hello h2{{font-size:23px}}
   .tcard,.fset{{padding:17px 16px}}
   .formgrid{{grid-template-columns:1fr;gap:13px}}
   .field.half{{grid-column:1 / -1}}
   .actions{{flex-direction:column-reverse}}
   .actions .btn{{width:100%}}
   .brand span{{font-size:9px;letter-spacing:.16em}}
 }}
 @media (prefers-reduced-motion: reduce){{
   *{{transition:none !important;animation:none !important}}
 }}
 .bell-btn{{background:none;border:0;cursor:pointer;font-size:20px;line-height:1;position:relative;padding:4px;color:inherit}}
 .bell-panel{{position:absolute;right:0;top:44px;width:330px;max-width:88vw;max-height:72vh;overflow:auto;
   background:var(--abyss,#04141d);border:1px solid var(--line);border-radius:16px;
   box-shadow:0 24px 60px -18px rgba(0,0,0,.6);padding:6px;display:none;z-index:60}}
 .bell-panel.open{{display:block}}
 .nf-head{{font-family:"Space Grotesk",monospace;font-size:12px;text-transform:uppercase;
   letter-spacing:.08em;color:var(--mist);padding:8px 10px 6px}}
 .nf{{display:block;width:100%;text-align:left;background:none;border:0;border-radius:11px;
   padding:10px 11px;cursor:pointer;color:var(--foam);text-decoration:none;font:inherit}}
 .nf:hover{{background:rgba(25,211,230,.08)}}
 .nf.un{{background:rgba(25,211,230,.06)}}
 .nf.un .nf-t::before{{content:'';display:inline-block;width:7px;height:7px;border-radius:50%;
   background:var(--pacific);margin-right:7px;vertical-align:middle}}
 .nf-t{{font-size:14px;line-height:1.35}}
 .nf-at{{font-size:11px;color:var(--mist);margin-top:3px}}
 .nf-empty{{padding:14px 12px;color:var(--mist);font-size:14px}}
 .pm-ov{{position:fixed;inset:0;background:rgba(2,12,18,.62);backdrop-filter:blur(4px);
   -webkit-backdrop-filter:blur(4px);display:none;align-items:flex-end;justify-content:center;z-index:80}}
 .pm-ov.open{{display:flex}}
 .pm{{background:var(--abyss,#04141d);border:1px solid var(--line);border-radius:20px 20px 0 0;
   width:100%;max-width:620px;max-height:86vh;overflow:auto;padding:18px 16px 24px;position:relative}}
 @media(min-width:620px){{.pm-ov{{align-items:center}}.pm{{border-radius:20px}}}}
 .pm-x{{position:absolute;top:12px;right:14px;background:none;border:0;color:var(--mist);
   font-size:22px;cursor:pointer;line-height:1}}
 .pm-head{{display:flex;align-items:center;gap:10px;margin:2px 34px 14px 2px}}
 .pm-title{{font-weight:700;font-size:16px;flex:1;min-width:0}}
 .pm-load{{color:var(--mist);padding:24px;text-align:center}}
 .cdown{{display:flex;flex-direction:column;align-items:center;text-align:center;
   background:linear-gradient(135deg,rgba(255,210,63,.16),rgba(61,240,197,.10));
   border:1px solid rgba(255,210,63,.3);border-radius:14px;padding:16px;margin:12px 0}}
 .cdown-n{{font-family:"Space Grotesk",monospace;font-size:34px;font-weight:800;
   color:var(--gold);line-height:1}}
 .cdown-l{{font-size:13px;color:var(--foam);margin-top:6px}}
 .cdown.live{{background:rgba(61,240,197,.1);border-color:rgba(61,240,197,.3)}}
 .cdown.live .cdown-n{{font-size:30px;color:var(--lagoon)}}
 .res{{margin:14px 0 2px}}
 .res-h{{font-size:13px;font-weight:700;color:var(--mist);margin-bottom:8px}}
 .res-i{{display:flex;align-items:center;gap:10px;padding:11px 12px;border-radius:11px;
   background:rgba(6,33,47,.5);border:1px solid var(--line);text-decoration:none;
   color:var(--foam);margin-bottom:7px;font-size:14px}}
 .res-i:hover{{border-color:rgba(25,211,230,.4)}}
 .res-ic{{font-size:18px}}
 .res-go{{margin-left:auto;color:var(--surf)}}

 /* ---- Full-screen agent (chatgpt.com layout) ---- */
 body.chatpage{{display:flex;flex-direction:column;min-height:100vh;min-height:100dvh}}
 .chatpage .wrap{{width:100%;max-width:820px;padding-bottom:0;flex:1;display:flex;flex-direction:column}}
 .chatpage .foot{{display:none}}
 .chat{{display:flex;flex-direction:column;flex:1;background:none;border:0;padding:0;margin:0}}
 .c-bar{{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:0 0 6px}}
 .c-bar .c-title{{font-family:"Bricolage Grotesque",sans-serif;font-weight:700;font-size:15px;color:var(--surf);margin-right:auto}}
 .c-search{{display:inline-flex;align-items:center;gap:6px;font-size:13px;font-weight:600;color:#03121b;text-decoration:none;
   border-radius:999px;padding:7px 14px;background:linear-gradient(120deg,var(--pacific),var(--lagoon))}}
 .chat .a-log{{max-height:none;overflow:visible;gap:22px;padding:14px 0 24px;flex:1}}
 .chat .msg{{font-size:15.5px;line-height:1.62;max-width:100%}}
 .chat .msg.ai{{background:none;border:0;border-radius:0;padding:0;align-self:stretch}}
 .chat .msg.me{{max-width:78%;background:rgba(155,246,236,.1);border:0;border-radius:20px;padding:10px 16px}}
 .chat .msg.ai b{{color:var(--foam)}}
 .c-empty{{flex:1;display:flex;flex-direction:column;justify-content:center;align-items:center;text-align:center;
   padding:6vh 0 2vh}}
 .c-empty h2{{font-size:clamp(28px,5vw,38px);font-weight:800;margin:0 0 8px}}
 .c-empty p{{color:var(--mist);margin:0 0 22px;font-size:15px;max-width:520px}}
 .chat.empty .a-log{{display:none}}
 .chat:not(.empty) .c-empty{{display:none}}
 .chat .chips{{justify-content:center;margin:12px 0 0}}
 .chat:not(.empty) .chips{{justify-content:flex-start;margin:0 0 10px}}
 .c-dock{{position:sticky;bottom:0;z-index:4;padding:10px 0 max(12px,env(safe-area-inset-bottom));
   background:linear-gradient(180deg,rgba(3,18,27,0),rgba(3,18,27,.97) 22px)}}
 .chat.empty .c-dock{{position:static;background:none;width:100%}}
 .chat .a-form{{margin:0;align-items:flex-end;background:rgba(10,40,55,.9);border:1px solid var(--line);
   border-radius:26px;padding:6px 6px 6px 16px;box-shadow:0 10px 40px -16px rgba(0,0,0,.8)}}
 .chat .a-form:focus-within{{border-color:rgba(25,211,230,.55)}}
 .chat .a-form textarea{{border:0;background:none;padding:10px 0;min-height:44px}}
 .chat .a-send{{min-height:40px;width:40px;padding:0;border-radius:50%;font-size:18px}}
 .c-note{{font-size:11.5px;color:var(--mist);text-align:center;margin-top:7px}}
 .chat .a-toast{{margin:0 0 8px}}
 .chat .rate{{margin-top:6px}}
 .chat .rbtn{{border:0;padding:3px 7px}}
 .chat .srcs{{border-top:0;padding-top:0}}
 @media(max-width:560px){{.chatpage .wrap{{padding:10px 14px 0}}.chat .msg{{font-size:15px}}
   .c-empty{{padding:3vh 0 1vh}}.c-bar .c-title{{display:none}}}}
 /* Inline source pills + preview card */
 .cite{{position:relative;display:inline-block;vertical-align:baseline;white-space:nowrap;outline:none}}
 .cite-a{{display:inline-flex;align-items:center;gap:3px;font-size:11.5px;font-weight:500;line-height:1;
   color:var(--mist);text-decoration:none;background:rgba(155,246,236,.1);border-radius:999px;padding:4px 8px;
   position:relative;top:-1px}}
 .cite-a:hover,.cite:focus-within .cite-a,.cite.open .cite-a{{background:var(--foam);color:#03121b}}
 .cite-n{{opacity:.75}}
 .cpop{{display:none;position:absolute;z-index:20;left:0;bottom:calc(100% + 6px);width:300px;max-width:82vw;
   white-space:normal;background:#0b2a3a;border:1px solid rgba(155,246,236,.25);border-radius:14px;
   box-shadow:0 18px 50px -12px rgba(0,0,0,.85);padding:6px;max-height:340px;overflow:auto}}
 .cite:hover .cpop,.cite:focus-within .cpop,.cite.open .cpop{{display:block}}
 .cite.down .cpop{{bottom:auto;top:calc(100% + 6px)}}
 .cite.right .cpop{{left:auto;right:0}}
 @media(max-width:640px),(hover:none){{
   .cpop,.cite.down .cpop,.cite.right .cpop{{position:fixed;left:12px;right:12px;top:auto;bottom:96px;width:auto;max-width:none;
     max-height:50vh}}
   .cite:hover .cpop{{display:none}}
   .cite.open .cpop{{display:block}}}}
 .cp-item{{display:flex;flex-direction:column;gap:3px;padding:8px;border-radius:10px;text-decoration:none;color:var(--foam)}}
 .cp-item:hover{{background:rgba(155,246,236,.08)}}
 .cp-item+.cp-item{{border-top:1px solid var(--line)}}
 .cp-img{{width:100%;height:120px;object-fit:cover;border-radius:8px;margin-bottom:4px;background:rgba(155,246,236,.06)}}
 .cp-host{{font-size:11.5px;color:var(--mist)}}
 .cp-title{{font-size:13.5px;line-height:1.3;color:var(--foam);font-weight:600}}
 .cp-snip{{font-size:12.5px;line-height:1.4;color:var(--mist);display:-webkit-box;-webkit-line-clamp:3;
   -webkit-box-orient:vertical;overflow:hidden}}

 /* Hotel list (chatgpt.com style) */
 .hlist .pc-head{{margin-bottom:4px}}
 .hcard{{display:flex;gap:16px;padding:16px 0;border-bottom:1px solid var(--line);white-space:normal}}
 .hcard:last-child{{border-bottom:0}}
 .hcard.on .hc-name{{color:var(--pacific)}}
 .hc-pic{{flex:none;width:168px;height:128px;border-radius:14px;overflow:hidden;padding:0;border:0;cursor:pointer;
   position:relative;background:rgba(155,246,236,.06)}}
 .hc-pic img{{width:100%;height:100%;object-fit:cover;display:block;transition:transform .25s}}
 .hc-pic:hover img{{transform:scale(1.04)}}
 .hc-nopic{{display:flex;align-items:center;justify-content:center;font-size:34px;
   background:linear-gradient(135deg,rgba(25,211,230,.18),rgba(61,240,197,.08))}}
 .hc-n{{position:absolute;right:6px;bottom:6px;font-size:11px;font-weight:600;color:#fff;background:rgba(3,18,27,.72);
   padding:2px 7px;border-radius:999px}}
 .hc-body{{min-width:0;flex:1;display:flex;flex-direction:column;gap:4px}}
 .hc-name{{all:unset;cursor:pointer;font-family:"Bricolage Grotesque",sans-serif;font-weight:700;font-size:18px;
   color:var(--foam);text-decoration:underline;text-decoration-color:rgba(155,246,236,.3);text-underline-offset:4px}}
 .hc-name:hover{{text-decoration-color:var(--pacific)}}
 .hc-name:focus-visible{{outline:2px solid var(--pacific);border-radius:4px}}
 .hc-sub{{font-size:13px;font-weight:600;color:var(--surf)}}
 .hc-loc{{font-size:12.5px;color:var(--mist)}}
 .hc-desc{{font-size:14.5px;line-height:1.55;color:var(--foam);margin-top:2px}}
 .hc-price{{font-size:14px;line-height:1.5;margin-top:2px}}
 .hc-price b{{color:var(--lagoon)}}
 .hc-conf{{font-size:12.5px;color:var(--gold)}}
 .hc-foot{{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-top:6px}}
 .hc-go{{font-size:12.5px;font-weight:600;color:var(--pacific);text-decoration:none}}
 @media(max-width:560px){{.hcard{{flex-direction:column;gap:10px}}.hc-pic{{width:100%;height:190px}}}}
 /* Hotel detail panel */
 .hpanel{{position:fixed;top:0;right:0;bottom:0;width:min(440px,100vw);z-index:40;overflow-y:auto;
   background:#071e2a;border-left:1px solid var(--line);box-shadow:-24px 0 60px -20px rgba(0,0,0,.8);white-space:normal}}
 .hpanel[hidden],.hp-ov[hidden]{{display:none}}
 .hp-top{{position:sticky;top:0;z-index:3;height:0}}
 .hp-x{{position:absolute;top:12px;right:12px;width:36px;height:36px;border-radius:50%;border:0;cursor:pointer;
   background:rgba(3,18,27,.75);color:#fff;font-size:15px}}
 .hp-ov{{position:fixed;inset:0;z-index:39;background:rgba(2,12,18,.55)}}
 @media(min-width:1180px){{.hp-ov{{display:none}}body.has-panel.chatpage .wrap{{margin-right:460px;margin-left:max(20px,calc((100vw - 460px - 820px)/2))}}}}
 .hd{{position:relative}}
 .hd-gal{{display:flex;overflow-x:auto;scroll-snap-type:x mandatory;height:280px;scrollbar-width:none;background:rgba(155,246,236,.05)}}
 .hd-gal::-webkit-scrollbar{{display:none}}
 .hd-nogal{{align-items:center;justify-content:center;color:var(--mist);font-size:13px;height:120px}}
 .hd-shot{{flex:none;width:100%;height:100%;margin:0;scroll-snap-align:start;position:relative}}
 .hd-shot img{{width:100%;height:100%;object-fit:cover;display:block}}
 .hd-shot figcaption{{position:absolute;left:10px;bottom:10px;font-size:11px;color:#fff;background:rgba(3,18,27,.72);
   padding:3px 8px;border-radius:999px}}
 .hd-arr{{position:absolute;top:122px;width:34px;height:34px;border-radius:50%;border:0;cursor:pointer;
   background:rgba(255,255,255,.88);color:#03121b;font-size:22px;line-height:1}}
 .hd-arr.pv{{left:10px}}.hd-arr.nx{{right:10px}}
 .hd-body{{padding:18px 20px 28px;display:flex;flex-direction:column;gap:12px}}
 .hd-name{{margin:0;font-size:26px;font-weight:800}}
 .hd-meta{{font-size:14px;color:var(--mist);margin-top:-6px}}
 .hd-btns{{display:flex;flex-wrap:wrap;gap:8px}}
 .hd-b{{font-size:14px;font-weight:600;text-decoration:none;color:var(--foam);border:1px solid var(--line);
   border-radius:999px;padding:9px 16px}}
 .hd-b:hover{{border-color:var(--pacific)}}
 .hd-main{{color:#03121b;border:0;background:linear-gradient(120deg,var(--pacific),var(--lagoon))}}
 .hd-row{{display:flex;gap:10px;font-size:14px;padding:10px 0;border-top:1px solid var(--line)}}
 .hd-row+.hd-row{{margin-top:-12px}}
 .hd-desc{{font-size:15px;line-height:1.6;border-top:1px solid var(--line);padding-top:12px}}
 .hd-desc p{{margin:0 0 10px}}.hd-desc ul{{margin:0 0 10px;padding-left:20px}}
 .hd-bemol b{{color:var(--gold)}}
 .hd-conf{{color:var(--gold);font-size:13px}}
 .hd-srcs{{display:flex;flex-wrap:wrap;gap:6px;align-items:center}}
 .hd-srcs span{{font-size:11px;text-transform:uppercase;letter-spacing:.12em;color:var(--mist);font-family:"Space Grotesk",monospace}}
 .hd-srcs a{{font-size:12px;color:var(--pacific);text-decoration:none;border:1px solid var(--line);border-radius:999px;padding:3px 9px}}
 .hd-note{{font-size:11.5px;color:var(--mist);margin:0}}
 @media(max-width:640px){{.hpanel{{top:auto;height:92vh;width:100vw;border-left:0;border-radius:20px 20px 0 0}}
   .hd-gal{{height:240px}}.hd-arr{{top:102px}}}}

 /* Price comparison + budget + map */
 .ptab,.btab{{margin:8px 0 4px;white-space:normal}}
 .ptab table,.btab table{{width:100%;border-collapse:collapse;font-size:14px}}
 .ptab th,.ptab td,.btab th,.btab td{{text-align:left;padding:10px 8px;border-bottom:1px solid var(--line);vertical-align:top}}
 .ptab thead th{{font-size:11.5px;text-transform:uppercase;letter-spacing:.08em;color:var(--mist);font-weight:500}}
 .ptab tbody th{{font-weight:600}}
 .pt-tot{{font-weight:700;white-space:nowrap}}
 .pt-tot small{{display:block;font-weight:400;font-size:12px;color:var(--mist);white-space:normal}}
 .pt-est{{display:inline-block;margin-left:6px;font-size:10.5px;font-weight:600;color:var(--gold);
   border:1px solid rgba(255,210,63,.45);border-radius:999px;padding:1px 6px;vertical-align:middle}}
 .pt-src{{font-size:12px;color:var(--mist);text-decoration:none;background:rgba(155,246,236,.1);border-radius:999px;padding:3px 8px}}
 .pt-note{{font-size:12px;color:var(--mist);margin:8px 0 0}}
 .btab{{max-width:460px;border:1px solid var(--line);border-radius:14px;padding:12px 14px;background:rgba(3,18,27,.45)}}
 .bt-h{{font-family:"Bricolage Grotesque",sans-serif;font-weight:700;font-size:14px;margin-bottom:4px}}
 .btab td{{text-align:right;font-variant-numeric:tabular-nums}}
 .bt-tot th,.bt-tot td{{font-weight:800;border-bottom:0;color:var(--lagoon)}}
 .hmap{{margin-top:14px}}
 .hm-h{{font-family:"Bricolage Grotesque",sans-serif;font-weight:700;font-size:15px;margin-bottom:8px}}
 .hmap img{{width:100%;border-radius:14px;display:block;border:1px solid var(--line)}}
 .hm-k{{display:flex;flex-wrap:wrap;gap:6px 14px;font-size:12.5px;color:var(--mist);margin-top:6px}}
 .hd-map{{width:100%;height:220px;border:0;border-radius:14px}}
 @media(max-width:560px){{.ptab{{overflow-x:auto}}.ptab table{{min-width:480px}}}}

 .hc-badges{{display:flex;flex-wrap:wrap;gap:6px;margin:2px 0}}
 .hc-badge{{font-size:11.5px;font-weight:600;color:var(--surf);background:rgba(155,246,236,.1);border-radius:999px;padding:3px 9px}}
 .hc-pts{{margin:2px 0 0;padding-left:18px;font-size:13.5px;color:var(--foam);line-height:1.5}}
 .hc-more{{all:unset;cursor:pointer;font-size:12.5px;font-weight:600;color:var(--foam);border:1px solid var(--line);border-radius:999px;padding:3px 10px}}
 .hc-more:hover{{border-color:var(--pacific)}}
 .hd-desc h4{{margin:12px 0 4px;font-size:13px;text-transform:uppercase;letter-spacing:.08em;color:var(--mist);font-family:"Space Grotesk",monospace}}

 .asteps{{margin:0 0 10px;font-size:13px;color:var(--mist);white-space:normal}}
 .asteps summary{{cursor:pointer;list-style:none;display:inline-flex;align-items:center;gap:6px}}
 .asteps summary::before{{content:"▸";transition:transform .15s}}
 .asteps[open] summary::before{{transform:rotate(90deg)}}
 .asteps ol{{margin:6px 0 0;padding-left:20px;line-height:1.6}}
 .hc-sel{{display:inline-flex;align-items:center;gap:5px;font-size:12.5px;color:var(--mist);cursor:pointer}}
 .hc-sel input{{accent-color:var(--pacific);width:15px;height:15px;margin:0}}
 .hp-cmp{{overflow-x:auto;margin:6px 0 12px}}
 .hp-cmp table{{min-width:0}}

 .chat .chips.starters{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;width:100%;margin:14px 0 0}}
 .chat:not(.empty) .chips.starters{{display:none}}
 .chip.starter{{display:flex;flex-direction:column;align-items:flex-start;gap:3px;text-align:left;border-radius:16px;
   padding:13px 15px;white-space:normal;background:rgba(10,40,55,.7);border-color:var(--line)}}
 .chip.starter:hover{{border-color:var(--pacific);background:rgba(25,211,230,.08)}}
 .chip.starter .st-ic{{font-size:18px;line-height:1}}
 .chip.starter b{{font-size:14px;color:var(--foam)}}
 .chip.starter .st-q{{font-size:12.5px;line-height:1.45;color:var(--mist)}}
 @media(max-width:560px){{.chat .chips.starters{{grid-template-columns:1fr;gap:8px}}.chip.starter{{padding:11px 13px}}}}

 .aprog{{margin:2px 0 14px;max-width:420px;transition:opacity .5s}}
 .aprog.done{{opacity:0}}
 .ap-lbl{{font-size:13.5px;color:var(--surf);margin-bottom:7px}}
 .ap-bar{{height:6px;border-radius:999px;background:rgba(155,246,236,.12);overflow:hidden}}
 .ap-bar span{{display:block;height:100%;width:0;border-radius:999px;
   background:linear-gradient(90deg,var(--pacific),var(--lagoon));transition:width .6s ease}}
 .msg.ai.typing:has(.aprog)::after{{content:none}}
 .chat.empty .c-dock{{display:flex;flex-direction:column}}
 .chat.empty .c-dock .chips{{order:3}}
 .chat.empty .c-dock .c-note{{order:2}}

 /* Tabbed answer */
 .rtabs{{white-space:normal;margin-top:2px}}
 .rt-nav{{display:flex;gap:6px;overflow-x:auto;scrollbar-width:none;padding:2px 0 10px;margin-bottom:6px;
   border-bottom:1px solid var(--line);position:sticky;top:70px;z-index:3;
   background:linear-gradient(180deg,rgba(3,18,27,.97),rgba(3,18,27,.9))}}
 .rt-nav::-webkit-scrollbar{{display:none}}
 .rt-btn{{flex:none;font:600 13.5px Inter,sans-serif;color:var(--mist);background:none;border:1px solid var(--line);
   border-radius:999px;padding:8px 14px;cursor:pointer}}
 .rt-btn:hover{{color:var(--foam);border-color:rgba(25,211,230,.5)}}
 .rt-btn.on{{color:#03121b;background:linear-gradient(120deg,var(--pacific),var(--lagoon));border-color:transparent}}
 .rt-pane{{white-space:pre-wrap;padding-top:6px}}
 .rt-pane[hidden]{{display:none}}
 .rt-pane .slot,.rt-pane .pcards,.rt-pane .ptab,.rt-pane .btab,.rt-pane .dcards{{white-space:normal}}
 @media(max-width:760px){{.rt-nav{{top:118px}}}}

 .crit{{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:0 0 12px}}
 .crit-h{{font-size:11px;text-transform:uppercase;letter-spacing:.12em;color:var(--mist);font-family:"Space Grotesk",monospace;margin-right:4px}}
 .crit-c{{font-size:13px;color:var(--foam);background:rgba(155,246,236,.1);border:1px solid var(--line);border-radius:999px;padding:5px 11px}}
 .note.warn{{background:rgba(255,210,63,.1);border:1px solid rgba(255,210,63,.4);color:var(--gold)}}

 /* ---- Layout v3: recent-chats sidebar + wider, denser column ---- */
 .chatpage .wrap{{max-width:none;width:100%;padding:0;margin:0}}
 body.has-panel.chatpage .wrap{{margin:0}}
 .cwrap{{display:grid;grid-template-columns:248px minmax(0,1fr);flex:1;min-height:0}}
 .csb{{position:sticky;top:70px;align-self:start;height:calc(100vh - 70px);height:calc(100dvh - 70px);overflow-y:auto;
   border-right:1px solid var(--line);padding:12px 10px;background:rgba(3,18,27,.55);white-space:normal}}
 .csb-new{{display:block;text-align:center;font-size:13.5px;font-weight:600;color:#03121b;text-decoration:none;border-radius:12px;
   padding:9px 10px;background:linear-gradient(120deg,var(--pacific),var(--lagoon));margin-bottom:12px}}
 .csb-h{{font-size:11px;text-transform:uppercase;letter-spacing:.12em;color:var(--mist);font-family:"Space Grotesk",monospace;margin:4px 6px 6px}}
 .csb-g{{font-size:11.5px;color:var(--mist);margin:10px 6px 4px}}
 .csb-i{{display:flex;align-items:center;border-radius:9px}}
 .csb-i a{{flex:1;min-width:0;font-size:13.5px;color:var(--foam);text-decoration:none;padding:7px 8px;white-space:nowrap;
   overflow:hidden;text-overflow:ellipsis}}
 .csb-i:hover,.csb-i.on{{background:rgba(155,246,236,.1)}}
 .csb-x{{visibility:hidden;background:none;border:0;color:var(--mist);cursor:pointer;font-size:12px;padding:6px 8px}}
 .csb-i:hover .csb-x{{visibility:visible}}
 .csb-empty{{font-size:12.5px;color:var(--mist);padding:6px}}
 .csb-tog{{display:none}}
 .chatpage .chat{{width:100%;max-width:960px;margin:0 auto;padding:0 28px}}
 .chat .c-bar{{margin:10px 0 2px}}
 .chat .a-log{{gap:16px;padding:8px 0 16px}}
 .chat .msg{{font-size:15px;line-height:1.58}}
 .hcard{{padding:12px 0;gap:14px}}
 .hc-pic{{width:150px;height:112px}}
 .hc-desc{{font-size:14px}}
 .rt-nav{{padding-bottom:8px;margin-bottom:4px}}
 @media(min-width:1180px){{body.has-panel .cwrap{{margin-right:440px}}.hpanel{{width:440px}}}}
 @media(max-width:900px){{
   .cwrap{{grid-template-columns:minmax(0,1fr)}}
   .csb{{position:fixed;left:0;top:0;bottom:0;height:100%;width:280px;z-index:45;transform:translateX(-100%);
     transition:transform .2s;background:#071e2a;box-shadow:20px 0 50px -20px rgba(0,0,0,.8)}}
   .csb.open{{transform:none}}
   .csb-tog{{display:inline-block}}
   .chatpage .chat{{padding:0 14px}}}}
 /* Brief form (progressive profiling) */
 .brief{{margin-top:10px;padding:14px 16px;border-radius:16px;border:1px solid rgba(25,211,230,.35);
   background:rgba(25,211,230,.05);white-space:normal;display:flex;flex-direction:column;gap:12px}}
 .bf-h{{font-family:"Bricolage Grotesque",sans-serif;font-weight:700;font-size:15px}}
 .bf-known{{display:flex;flex-wrap:wrap;gap:6px;align-items:center;white-space:normal}}
 .bf-l{{font-size:11px;text-transform:uppercase;letter-spacing:.1em;color:var(--mist);margin-right:2px}}
 .bf-k{{font-size:12.5px;background:rgba(155,246,236,.1);border-radius:999px;padding:4px 10px}}
 .bf-f label{{display:block;font-size:12.5px;color:var(--mist);margin-bottom:6px;font-weight:600}}
 .bf-opts{{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:6px}}
 .bf-o{{background:rgba(3,18,27,.6);border:1px solid var(--line);color:var(--foam);border-radius:999px;padding:7px 12px;
   font:500 13px Inter,sans-serif;cursor:pointer}}
 .bf-o:hover{{border-color:var(--pacific)}}
 .bf-o.on{{background:var(--pacific);color:#03121b;border-color:var(--pacific)}}
 .bf-in,.bf-age{{background:var(--field);border:1px solid var(--line);color:var(--foam);border-radius:10px;padding:8px 10px;
   font:inherit;font-size:14px;max-width:260px;width:100%}}
 .bf-in:focus,.bf-age:focus{{outline:none;border-color:var(--pacific)}}
 .bf-pax{{display:flex;flex-wrap:wrap;gap:18px;font-size:14px}}
 .bf-pax span{{display:inline-flex;align-items:center;gap:8px}}
 .bf-pax b{{min-width:16px;text-align:center}}
 .bf-m,.bf-p{{width:30px;height:30px;border-radius:50%;border:1px solid var(--line);background:none;color:var(--foam);cursor:pointer;font-size:16px}}
 .bf-ages{{display:flex;flex-wrap:wrap;gap:8px;margin-top:8px}}
 .bf-ages .bf-age{{max-width:150px}}
 .bf-go{{align-self:flex-start;min-height:42px;padding:10px 20px;font-size:14px}}
 .bf-skip{{align-self:flex-start;background:none;border:0;color:var(--mist);font-size:12.5px;cursor:pointer;text-decoration:underline;padding:0}}
</style></head><body class="pg{page}">
 <div class="top">
   <div class="brand"><img src="/static/logo.png" alt="Du Voyageur">
     <div><b>Du Voyageur</b><span>Espace client</span></div></div>
   {nav}
   {logout}
 </div>
 <div class="wrap">{body}
   <div class="foot">Du Voyageur · Permis d'agence 700495</div>
 </div>
 <div class="pm-ov" id="pmOv"><div class="pm" id="pm">
   <button type="button" class="pm-x" id="pmX" aria-label="Fermer">&times;</button>
   <div id="pmBody"><div class="pm-load">Chargement…</div></div>
 </div></div>
 <script>
 (function(){{
  var btn=document.getElementById('pbellBtn');
  var pan=document.getElementById('pbellPanel');
  function markRead(){{
   try{{fetch('/portail/notifications/read',{{method:'POST'}});}}catch(e){{}}
   var d=btn&&btn.querySelector('.bdot'); if(d)d.remove();
   var u=pan?pan.querySelectorAll('.nf.un'):[];
   for(var i=0;i<u.length;i++)u[i].classList.remove('un');
  }}
  if(btn&&pan){{
   btn.addEventListener('click',function(e){{
    e.stopPropagation();
    if(pan.classList.toggle('open'))markRead();
   }});
   document.addEventListener('click',function(e){{
    if(!e.target.closest('.bell'))pan.classList.remove('open');
   }});
  }}
  var ov=document.getElementById('pmOv');
  var body=document.getElementById('pmBody');
  function openCase(id){{
   if(pan)pan.classList.remove('open');
   body.innerHTML='<div class="pm-load">Chargement…</div>';
   ov.classList.add('open');
   fetch('/portail/service/'+id+'/fragment').then(function(r){{return r.text();}})
    .then(function(h){{body.innerHTML=h;}})
    .catch(function(){{body.innerHTML='<div class="pm-load">Impossible de charger la demande. '
      +'<a href="/portail/service">Ouvrir la page</a></div>';}});
  }}
  document.addEventListener('click',function(e){{
   var t=e.target.closest('[data-case]');
   if(t){{e.preventDefault();openCase(t.getAttribute('data-case'));}}
  }});
  function closeModal(){{ov.classList.remove('open');}}
  document.getElementById('pmX').addEventListener('click',closeModal);
  ov.addEventListener('click',function(e){{if(e.target===ov)closeModal();}});
  document.addEventListener('keydown',function(e){{if(e.key==='Escape')closeModal();}});
 }})();
 </script>
</body></html>"""
