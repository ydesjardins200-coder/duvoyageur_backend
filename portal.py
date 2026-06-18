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
from datetime import datetime
from html import escape

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from config import settings
from db import Case, Client, SessionLocal, add_identity, log_activity, normalize_email, normalize_phone
from trip_schema import TripRequest

router = APIRouter()

PORTAL_COOKIE = "dv_portal"
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
    it, so an active client effectively stays logged in)."""
    resp.set_cookie(
        PORTAL_COOKIE, _make_session_value(client_id),
        max_age=settings.PORTAL_SESSION_MAX_AGE, path=_COOKIE_PATH,
        httponly=True, samesite="lax", secure=settings.SECURE_COOKIES)


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


def _gate(request: Request):
    """Guard for KYC-protected tabs. Returns (cid, None) when the visitor is
    logged in AND has completed their identity; otherwise (None, response) with
    a redirect to the profile (or a login prompt)."""
    cid = current_portal_client_id(request)
    if not cid:
        return None, HTMLResponse(_info_page(
            "Espace client",
            "Ouvre ton lien personnel, ou redemande-le sur Messenger "
            "(menu ☰ → « 🔐 Mon espace client »). 🔐"))
    with SessionLocal() as db:
        client = db.get(Client, cid)
        if not client:
            return None, HTMLResponse(_info_page("Espace client", "Dossier introuvable. 🙂"))
        if not kyc_complete(client):
            return None, RedirectResponse("/portail/profil", status_code=303)
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
        action = f"{ref}{eco}<div class='muted'>Confirmation détaillée envoyée par Tripbook. Bon voyage ! 🌴</div>"
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
    if client.primary_phone:
        rows.append(("Téléphone", escape(client.primary_phone)))
    city = (client.kyc or {}).get("city")
    if city:
        rows.append(("Ville", escape(str(city))))
    body = "".join(f"<div class='idrow'><span class='ik'>{k}</span><span>{v}</span></div>"
                   for k, v in rows)
    return ("<div class='idcard'>"
            "<div class='idtop'><h3>Mon identité</h3>"
            "<a class='edit' href='/portail/profil'>Modifier</a></div>"
            f"{body}</div>")


def _accueil(client, cases, flash: str = "") -> str:
    name = escape(client.display_name or "")
    hello = f"Bonjour {name} 👋" if name else "Bonjour 👋"
    trips = [c for c in cases if (c.kind or "trip") == "trip"]
    future = [c for c in trips if c.status != "closed"]
    past = [c for c in trips if c.status == "closed"]
    total = _savings_total(trips)
    note = f"<div class='note ok'>{flash}</div>" if flash else ""

    sav = ("<div class='savings'><div><div class='sv-l'>Tes économies</div>"
           f"<div class='sv-n'>{_fmt_money(total)}</div></div>"
           "<div style='font-size:30px'>💸</div></div>") if total > 0 else ""

    cta = ("<div style='margin:0 0 6px'>"
           "<a class='btn' href='/portail/nouveau-voyage'>+ Nouvelle demande de voyage</a></div>")

    fut = ("<h3 class='acc-h'>Voyages à venir</h3><div class='tgrid'>"
           + "".join(_trip_card(c) for c in future) + "</div>") if future else (
           "<h3 class='acc-h'>Voyages à venir</h3>"
           "<div class='tcard empty'>Aucun voyage en cours. Lance une demande ci-dessus ! 🌴</div>")
    pst = ("<h3 class='acc-h'>Voyages passés</h3><div class='tgrid'>"
           + "".join(_trip_card(c) for c in past) + "</div>") if past else ""

    return (
        f"<div class='hello'><h2>{hello}</h2></div>"
        + note + _identity_card(client) + sav + cta + fut + pst)


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
# (key, label, input type, required, half-width). email/phone live on the
# Client; everything else is stored in the `kyc` JSON blob. We never ask for or
# store passport / travel-document data.
_KYC_FIELDS = [
    ("legal_first_name", "Prénom légal", "text", True, True),
    ("legal_last_name", "Nom légal", "text", True, True),
    ("date_of_birth", "Date de naissance", "date", True, True),
    ("phone", "Téléphone", "tel", True, True),
    ("email", "Courriel", "email", True, False),
    ("address", "Adresse", "text", True, False),
    ("city", "Ville", "text", True, True),
    ("province", "Province / État", "text", True, True),
    ("postal_code", "Code postal", "text", True, True),
    ("country", "Pays", "text", True, True),
]


def _kyc_value(client, key):
    if key == "email":
        return client.primary_email
    if key == "phone":
        return client.primary_phone
    return (client.kyc or {}).get(key)


def kyc_status(client):
    """(filled, total, [missing labels]) over the required identity fields."""
    req = [f for f in _KYC_FIELDS if f[3]]
    missing = [label for (k, label, _t, _r, _h) in req if not _kyc_value(client, k)]
    return len(req) - len(missing), len(req), missing


def kyc_complete(client) -> bool:
    return not kyc_status(client)[2]


def _kyc_banner(client) -> str:
    done, total, missing = kyc_status(client)
    if not missing:
        return ""
    n = len(missing)
    return (
        "<a class='banner' href='/portail/profil'>"
        "<div class='banner-txt'><b>Complète ton identité</b>"
        f"<span>{n} champ{'s' if n > 1 else ''} à remplir pour qu'on puisse réserver tes voyages.</span></div>"
        "<span class='banner-cta'>Compléter →</span></a>")


def _profile_form(client, saved: bool = False, locked: bool = False) -> str:
    done, total, _missing = kyc_status(client)
    pct = int(done / total * 100) if total else 100
    ok = "<div class='note ok'>Profil enregistré ✓</div>" if saved else ""
    lock = ("<div class='banner'><div class='banner-txt'>"
            "<b>🔒 Complète ton identité pour débloquer ton espace</b>"
            "<span>Tes voyages, demandes et avis s'ouvrent dès que ton identité est complète.</span>"
            "</div></div>") if locked else ""
    prog = (f"<div class='prog'><span style='width:{pct}%'></span></div>"
            f"<p class='lede'>{done}/{total} champs remplis.</p>")

    def field(k, label, typ, req, half):
        val = escape(str(_kyc_value(client, k) or ""))
        miss = req and not _kyc_value(client, k)
        star = " <span class='req'>*</span>" if req else ""
        cls = "field half" if half else "field"
        if miss:
            cls += " miss"
        return (f"<div class='{cls}'><label>{label}{star}</label>"
                f"<input name='{k}' type='{typ}' value=\"{val}\" inputmode='"
                f"{'email' if typ == 'email' else ('tel' if typ == 'tel' else 'text')}'"
                f"{' required' if req else ''}></div>")

    def group(title, keys, sub=""):
        fields = "".join(field(*f) for f in _KYC_FIELDS if f[0] in keys)
        s = f" <span class='muted'>{sub}</span>" if sub else ""
        return (f"<div class='fset'><h3>{title}{s}</h3>"
                f"<div class='formgrid'>{fields}</div></div>")

    back = ("" if locked
            else "<a class='btn ghost' href='/portail'>Retour</a>")
    return (
        lock + ok + prog
        + "<form class='form' method='post' action='/portail/profil'>"
        + group("Identité", ("legal_first_name", "legal_last_name", "date_of_birth"))
        + group("Coordonnées", ("phone", "email", "address", "city",
                                "province", "postal_code", "country"))
        + "<div class='actions'>"
          "<button class='btn block' type='submit'>Enregistrer mon profil</button>"
          + back + "</div></form>")


# --------------------------------------------------------------------------- #
# New trip request
# --------------------------------------------------------------------------- #
_BOARD_OPTS = [("", "Peu importe"), ("all_inclusive", "Tout inclus"),
               ("breakfast", "Petit-déjeuner"), ("half_board", "Demi-pension"),
               ("full_board", "Pension complète"), ("room_only", "Chambre seulement")]
_BASIS_OPTS = [("", "—"), ("per_person", "par personne"), ("total", "pour le groupe")]


def _new_trip_form() -> str:
    bsel = "".join(f"<option value='{v}'>{l}</option>" for v, l in _BOARD_OPTS)
    psel = "".join(f"<option value='{v}'>{l}</option>" for v, l in _BASIS_OPTS)
    return (
        "<form class='form' method='post' action='/portail/nouveau-voyage'>"
        "<div class='fset'><h3>Destination</h3><div class='formgrid'>"
        "<div class='field'><label>Où veux-tu aller ? <span class='req'>*</span></label>"
        "<input name='destination' placeholder='ex. Punta Cana, Cancún, Varadero…' required></div>"
        "<div class='field'><label>Un hôtel en particulier ?</label>"
        "<input name='hotel_name_raw' placeholder='optionnel'></div>"
        "</div></div>"
        "<div class='fset'><h3>Quand &amp; d'où</h3><div class='formgrid'>"
        "<div class='field'><label>Tu pars d'où ? <span class='req'>*</span></label>"
        "<input name='origin_city' placeholder='ville ou aéroport (ex. Montréal)' required></div>"
        "<div class='field half'><label>Date de départ <span class='req'>*</span></label>"
        "<input name='departure_date' type='date' required></div>"
        "<div class='field half'><label>Date de retour <span class='req'>*</span></label>"
        "<input name='return_date' type='date' required></div>"
        "</div></div>"
        "<div class='fset'><h3>Voyageurs</h3><div class='formgrid'>"
        "<div class='field half'><label>Adultes <span class='req'>*</span></label>"
        "<input name='num_adults' type='number' min='1' value='2' inputmode='numeric' required></div>"
        "<div class='field half'><label>Enfants</label>"
        "<input name='num_children' type='number' min='0' value='0' inputmode='numeric'></div>"
        "<div class='field half'><label>Âges des enfants</label>"
        "<input name='children_ages' placeholder='ex. 4, 9'></div>"
        "<div class='field half'><label>Chambres</label>"
        "<input name='num_rooms' type='number' min='1' inputmode='numeric'></div>"
        "</div></div>"
        "<div class='fset'><h3>Détails <span class='muted'>(optionnel)</span></h3><div class='formgrid'>"
        f"<div class='field half'><label>Type de forfait</label><select name='board'>{bsel}</select></div>"
        "<div class='field half'><label>Prix que t'as vu</label>"
        "<input name='price_amount' type='number' inputmode='decimal' placeholder='ex. 1450'></div>"
        f"<div class='field half'><label>Ce prix, c'est…</label><select name='price_basis'>{psel}</select></div>"
        "<div class='field'><label>Autre chose à nous dire ?</label>"
        "<textarea name='notes' placeholder='préférences, occasion spéciale, budget…'></textarea></div>"
        "</div></div>"
        "<div class='actions'>"
        "<button class='btn block' type='submit'>Envoyer ma demande</button>"
        "<a class='btn ghost' href='/portail'>Annuler</a></div>"
        "</form>")


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
        out.append(
            "<details class='thread'><summary>"
            f"<span class='th-sum'>{preview}</span>"
            f"<span class='badge {cls}'>{label}</span></summary>"
            f"<div class='msgs'>{bubbles}</div></details>")
    return "".join(out)


# --------------------------------------------------------------------------- #
# Shell + nav
# --------------------------------------------------------------------------- #
def _nav(active: str, locked: bool = False) -> str:
    if locked:                       # KYC incomplete -> no tabs, profile only
        return ""
    items = [("accueil", "/portail", "Accueil"),
             ("nouveau", "/portail/nouveau-voyage", "Nouveau voyage"),
             ("aide", "/portail/service", "Aide"),
             ("avis", "/portail/avis", "Mes avis"),
             ("profil", "/portail/profil", "Mon profil")]
    links = "".join(
        f"<a class='pill{' on' if k == active else ''}' href='{href}'>{label}</a>"
        for k, href, label in items)
    return f"<nav class='pnav'>{links}</nav>"


# --------------------------------------------------------------------------- #
# Shell
# --------------------------------------------------------------------------- #
def _shell(title: str, body: str, logged_in: bool = False, nav: str = "",
           bell: int = 0) -> str:
    bell_html = ""
    if logged_in:
        badge = f"<span class='bdot'>{bell if bell < 10 else '9+'}</span>" if bell else ""
        bell_html = ("<a class='bell' href='/portail/notifications' "
                     f"aria-label='Notifications'>🔔{badge}</a>")
    logout = ("<a class='logout' href='/portail/logout'>Déconnexion</a>"
              if logged_in else "")
    actions = f"<div class='topact'>{bell_html}{logout}</div>" if logged_in else logout
    return _PORTAL_PAGE.format(title=escape(title), body=body, logout=actions, nav=nav)


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
def portal_home(request: Request, new: int = 0, avis: int = 0):
    cid = current_portal_client_id(request)
    if not cid:
        return HTMLResponse(_info_page(
            "Espace client",
            "Pour accéder à ton espace, ouvre le lien personnel qu'on t'a "
            "envoyé. Tu peux aussi le redemander à tout moment sur Messenger : "
            "menu ☰ → « 🔐 Mon espace client ». 🔐"))
    flash = ""
    if new:
        flash = "Demande envoyée ✓ — on te revient bientôt avec ton rabais !"
    elif avis:
        flash = "Merci pour ton avis ✓"
    with SessionLocal() as db:
        client = db.get(Client, cid)
        if not client:
            return HTMLResponse(_info_page(
                "Espace client",
                "On n'a pas retrouvé ton dossier. Écris-nous sur Messenger. 🙂"))
        cases = list(client.requests)
        if not kyc_complete(client):
            return RedirectResponse("/portail/profil", status_code=303)
        body = _accueil(client, cases, flash=flash)
        bell = _unread(client)
    resp = HTMLResponse(_shell("Accueil", body, logged_in=True,
                               nav=_nav("accueil"), bell=bell))
    _set_session_cookie(resp, cid)          # sliding expiry on every visit
    return resp


@router.get("/portail/notifications", response_class=HTMLResponse)
def portal_notifications(request: Request):
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
                               nav=_nav("accueil"), bell=0))
    _set_session_cookie(resp, cid)
    return resp


@router.get("/portail/profil", response_class=HTMLResponse)
def portal_profile(request: Request, saved: int = 0):
    cid = current_portal_client_id(request)
    if not cid:
        return HTMLResponse(_info_page(
            "Espace client",
            "Pour accéder à ton profil, ouvre ton lien personnel ou redemande-le "
            "sur Messenger (menu ☰ → « 🔐 Mon espace client »). 🔐"))
    with SessionLocal() as db:
        client = db.get(Client, cid)
        if not client:
            return HTMLResponse(_info_page("Espace client", "Dossier introuvable. 🙂"))
        locked = not kyc_complete(client)
        lede = ("Bienvenue ! Complète ton identité pour ouvrir ton espace."
                if locked else "Ces infos nous servent à réserver tes voyages au bon nom.")
        body = (f"<div class='hello'><h2>Mon profil</h2><p class='lede'>{lede}</p></div>"
                + _profile_form(client, saved=bool(saved), locked=locked))
        bell = _unread(client)
    resp = HTMLResponse(_shell("Mon profil", body, logged_in=True,
                               nav=_nav("profil", locked=locked), bell=bell))
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
                v = (form.get(k) or "").strip()
                if k == "email":
                    em = normalize_email(v)
                    if em:
                        client.primary_email = em
                        add_identity(db, client, "email", em)
                elif k == "phone":
                    ph = normalize_phone(v)
                    if ph:
                        client.primary_phone = ph
                        add_identity(db, client, "phone", ph)
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
        bell = _unread(db.get(Client, cid))
    body = ("<div class='hello'><h2>Nouvelle demande de voyage</h2>"
            "<p class='lede'>Dis-nous ce que tu cherches — on retrouve le même "
            "forfait avec ton rabais. 🌴</p></div>" + _new_trip_form())
    resp = HTMLResponse(_shell("Nouveau voyage", body, logged_in=True,
                               nav=_nav("nouveau"), bell=bell))
    _set_session_cookie(resp, cid)
    return resp


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
            "hotel_name_raw": g("hotel_name_raw"),
            "origin_city": g("origin_city"),
            "departure_date": g("departure_date"),
            "return_date": g("return_date"),
            "board": g("board") or "unknown",
            "num_adults": gi("num_adults"),
            "num_children": gi("num_children"),
            "num_rooms": gi("num_rooms"),
            "raw_message": g("notes"),
        }
        ages = [int(a) for a in re.split(r"[,\s]+", form.get("children_ages", "") or "")
                if a.strip().isdigit()]
        if ages:
            d["passengers"] = [{"age": a} for a in ages]
        amt = gf("price_amount")
        if amt is not None:
            d["price_seen"] = {"amount": amt, "currency": "CAD",
                               "basis": g("price_basis") or "unknown"}
        try:
            trip = TripRequest.model_validate({k: v for k, v in d.items() if v is not None})
        except Exception:  # noqa: BLE001
            return RedirectResponse("/portail/nouveau-voyage", status_code=303)
        rem = trip.remaining_fields()
        case = Case(
            client_id=client.id, channel="portal",
            status="new",                      # a fresh lead the agent must action
            customer_email=client.primary_email, customer_phone=client.primary_phone,
            parse_confidence=1.0, raw_message=g("notes"),
            trip=trip.model_dump(), needs_clarification=rem,
            screenshots=[], messages=[])
        db.add(case)
        db.flush()
        log_activity(db, client.id, "request_created",
                     "Nouvelle demande via espace client", case.id)
        db.commit()
    return RedirectResponse("/portail?new=1", status_code=303)


@router.get("/portail/avis", response_class=HTMLResponse)
def portal_avis(request: Request, ok: int = 0):
    cid, gate = _gate(request)
    if gate:
        return gate
    with SessionLocal() as db:
        client = db.get(Client, cid)
        cases = list(client.requests)
        flash = "Merci pour ton avis ✓" if ok else ""
        body = _avis_page(client, cases, flash=flash)
        bell = _unread(client)
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
        body = (("<div class='note ok'>Message envoyé ✓ — on te répond bientôt.</div>"
                 if sent else "")
                + "<div class='hello'><h2>Demande de service</h2>"
                "<p class='lede'>Une question, un changement, un pépin ? Écris-nous, "
                "un conseiller te répond.</p></div>"
                + _service_form(cases) + _service_threads(cases))
        bell = _unread(client)
    resp = HTMLResponse(_shell("Aide", body, logged_in=True, nav=_nav("aide"), bell=bell))
    _set_session_cookie(resp, cid)
    return resp


@router.post("/portail/service")
async def portal_service_save(request: Request):
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


@router.get("/portail/logout")
def portal_logout():
    resp = HTMLResponse(_info_page(
        "À bientôt 👋",
        "Tu es déconnecté de ton espace client.", logged_in=False))
    resp.delete_cookie(PORTAL_COOKIE, path=_COOKIE_PATH)
    return resp


# --------------------------------------------------------------------------- #
# HTML shell template (standalone, client-facing brand — no admin nav)
# --------------------------------------------------------------------------- #
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
 .top{{display:flex;align-items:center;justify-content:space-between;gap:12px;
   padding:14px 18px;padding-top:max(14px,env(safe-area-inset-top));
   border-bottom:1px solid var(--line);
   background:linear-gradient(180deg, rgba(8,33,47,.72), rgba(8,33,47,.35));
   backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);
   position:sticky;top:0;z-index:5}}
 .brand{{display:flex;align-items:center;gap:11px;min-width:0}}
 .brand img{{width:40px;height:40px;border-radius:50%;box-shadow:0 0 0 1px var(--line);flex:none}}
 .brand b{{font-family:"Bricolage Grotesque",sans-serif;font-weight:800;font-size:17px;display:block;line-height:1.1}}
 .brand span{{font-family:"Space Grotesk",monospace;text-transform:uppercase;letter-spacing:.2em;
   font-size:10px;color:var(--pacific);display:block;margin-top:2px}}
 .logout{{font-size:13px;color:var(--mist);text-decoration:none;white-space:nowrap;
   padding:8px 10px;border-radius:10px}}
 .logout:hover,.logout:active{{color:var(--foam)}}
 .topact{{display:flex;align-items:center;gap:6px}}
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
 /* Pill nav */
 .pnav{{display:flex;gap:8px;overflow-x:auto;padding:12px 18px 0;max-width:980px;margin:0 auto;
   -webkit-overflow-scrolling:touch;scrollbar-width:none}}
 .pnav::-webkit-scrollbar{{display:none}}
 .pill{{flex:none;font-size:14px;font-weight:600;text-decoration:none;color:var(--mist);
   padding:9px 16px;border-radius:999px;border:1px solid var(--line);
   background:rgba(8,33,47,.4)}}
 .pill.on{{color:#02161c;background:linear-gradient(120deg,var(--pacific),var(--lagoon));border-color:transparent}}
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
 .hint{{font-size:12px;color:var(--mist);margin:6px 0 0}}
 /* Progress */
 .prog{{height:8px;border-radius:999px;background:rgba(8,33,47,.7);overflow:hidden;margin:2px 0 6px}}
 .prog span{{display:block;height:100%;border-radius:999px;
   background:linear-gradient(90deg,var(--pacific),var(--lagoon));transition:width .4s ease}}
 .note{{border-radius:12px;padding:11px 14px;font-size:14px;margin:0 0 16px}}
 .note.ok{{background:rgba(61,240,197,.12);border:1px solid rgba(61,240,197,.4);color:var(--lagoon)}}
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
</style></head><body>
 <div class="top">
   <div class="brand"><img src="/static/logo.png" alt="Du Voyageur">
     <div><b>Du Voyageur</b><span>Espace client</span></div></div>
   {logout}
 </div>
 {nav}
 <div class="wrap">{body}
   <div class="foot">Du Voyageur · Permis d'agence 700495</div>
 </div>
</body></html>"""
