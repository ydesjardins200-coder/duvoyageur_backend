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

import secrets
from html import escape

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from config import settings
from db import Case, Client, SessionLocal

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


def _dashboard(client, cases) -> str:
    name = escape(client.display_name or "")
    hello = f"Bonjour {name} 👋" if name else "Bonjour 👋"
    trips = [c for c in cases if (c.kind or "trip") == "trip"]
    if trips:
        cards = "".join(_trip_card(c) for c in trips)
    else:
        cards = ("<div class='tcard empty'>Aucune demande pour l'instant. "
                 "Écris-nous sur Messenger pour trouver ton prochain voyage à rabais ! 🌴</div>")
    return (
        f"<div class='hello'><h2>{hello}</h2>"
        "<p class='lede'>Voici tes demandes de voyage et leurs soumissions.</p></div>"
        f"<div class='tgrid'>{cards}</div>")


# --------------------------------------------------------------------------- #
# Shell
# --------------------------------------------------------------------------- #
def _shell(title: str, body: str, logged_in: bool = False) -> str:
    logout = ("<a class='logout' href='/portail/logout'>Déconnexion</a>"
              if logged_in else "")
    return _PORTAL_PAGE.format(title=escape(title), body=body, logout=logout)


def _info_page(title: str, message: str, logged_in: bool = False) -> str:
    body = (f"<div class='infobox'><h2>{escape(title)}</h2>"
            f"<p>{message}</p></div>")
    return _shell(title, body, logged_in)


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@router.get("/portail/login", response_class=HTMLResponse)
def portal_login(request: Request, token: str = ""):
    data = _read_login_token(token) if token else None
    if not data:
        return HTMLResponse(_info_page(
            "Lien expiré",
            "Ce lien n'est plus valide (il a peut-être expiré ou déjà été "
            "utilisé). Écris-nous sur Messenger et on t'en renvoie un tout neuf. 🙂"))
    cid, nonce = data.get("cid"), data.get("n")
    with SessionLocal() as db:
        client = db.get(Client, cid) if cid else None
        if not client or not client.portal_nonce or not secrets.compare_digest(
                client.portal_nonce, nonce or ""):
            return HTMLResponse(_info_page(
                "Lien invalide",
                "Ce lien n'est plus valide. Écris-nous sur Messenger pour en "
                "recevoir un nouveau. 🙂"))
        client.portal_nonce = None          # single use — burn it
        db.commit()
    resp = RedirectResponse("/portail", status_code=303)
    resp.set_cookie(
        PORTAL_COOKIE, _make_session_value(cid),
        max_age=settings.PORTAL_SESSION_MAX_AGE, path=_COOKIE_PATH,
        httponly=True, samesite="lax", secure=settings.SECURE_COOKIES)
    return resp


@router.get("/portail", response_class=HTMLResponse)
def portal_home(request: Request):
    cid = current_portal_client_id(request)
    if not cid:
        return HTMLResponse(_info_page(
            "Espace client",
            "Pour accéder à ton espace, ouvre le lien personnel qu'on t'a "
            "envoyé sur Messenger ou par courriel. 🔐"))
    with SessionLocal() as db:
        client = db.get(Client, cid)
        if not client:
            return HTMLResponse(_info_page(
                "Espace client",
                "On n'a pas retrouvé ton dossier. Écris-nous sur Messenger. 🙂"))
        cases = list(client.requests)
        body = _dashboard(client, cases)
    return HTMLResponse(_shell("Mon espace", body, logged_in=True))


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
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Du Voyageur — {title}</title>
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
 body{{margin:0;min-height:100vh;font-family:"Inter",system-ui,sans-serif;color:var(--foam);
   background:
     radial-gradient(80% 55% at 80% -5%, rgba(25,211,230,.18), transparent 60%),
     linear-gradient(180deg, rgba(3,18,27,.92), rgba(3,18,27,.97)),
     url("/static/login-bg.webp") center/cover fixed no-repeat;}}
 a{{color:var(--pacific)}}
 .top{{display:flex;align-items:center;justify-content:space-between;gap:12px;
   padding:16px 22px;border-bottom:1px solid var(--line);
   background:linear-gradient(180deg, rgba(8,33,47,.6), rgba(8,33,47,.25));
   backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);
   position:sticky;top:0;z-index:5}}
 .brand{{display:flex;align-items:center;gap:12px}}
 .brand img{{width:42px;height:42px;border-radius:50%;box-shadow:0 0 0 1px var(--line)}}
 .brand b{{font-family:"Bricolage Grotesque",sans-serif;font-weight:800;font-size:18px;letter-spacing:-.02em}}
 .brand span{{font-family:"Space Grotesk",monospace;text-transform:uppercase;letter-spacing:.2em;
   font-size:10px;color:var(--pacific);display:block;margin-top:2px}}
 .logout{{font-size:13px;color:var(--mist);text-decoration:none}}
 .logout:hover{{color:var(--foam)}}
 .wrap{{max-width:820px;margin:0 auto;padding:26px 20px 60px}}
 .hello h2{{font-family:"Bricolage Grotesque",sans-serif;font-weight:800;font-size:26px;
   letter-spacing:-.02em;margin:6px 0 2px}}
 .lede{{color:var(--mist);margin:0 0 22px;font-size:14px}}
 .tgrid{{display:grid;gap:16px}}
 .tcard{{background:linear-gradient(180deg, rgba(20,62,82,.5), rgba(8,33,47,.6));
   border:1px solid var(--line);border-radius:18px;padding:20px 22px;
   box-shadow:0 24px 60px -28px rgba(0,0,0,.8)}}
 .tcard.empty{{color:var(--mist);text-align:center}}
 .tchdr{{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:10px;flex-wrap:wrap}}
 .tchdr h3{{font-family:"Bricolage Grotesque",sans-serif;font-weight:700;font-size:19px;margin:0}}
 .badge{{font-family:"Space Grotesk",monospace;text-transform:uppercase;letter-spacing:.08em;
   font-size:11px;padding:5px 11px;border-radius:999px;white-space:nowrap}}
 .badge.prep{{background:rgba(148,184,198,.16);color:var(--surf)}}
 .badge.quote{{background:rgba(255,210,63,.16);color:var(--gold)}}
 .badge.booked{{background:rgba(61,240,197,.16);color:var(--lagoon)}}
 .badge.done{{background:rgba(148,184,198,.12);color:var(--mist)}}
 .row{{display:flex;justify-content:space-between;gap:14px;padding:7px 0;border-top:1px solid var(--line);font-size:14px}}
 .row .k{{color:var(--mist)}}
 .row .v{{font-weight:600;text-align:right}}
 .eco{{margin:12px 0 4px;font-size:15px}}
 .muted{{color:var(--mist);font-size:13px;margin-top:10px}}
 .btn{{display:inline-block;margin-top:14px;font-family:"Bricolage Grotesque",sans-serif;font-weight:700;
   font-size:14px;padding:11px 18px;border-radius:999px;text-decoration:none;color:#02161c;
   background:linear-gradient(120deg,var(--pacific),var(--lagoon));
   box-shadow:0 12px 30px -12px var(--glow)}}
 .btn:hover{{transform:translateY(-1px)}}
 .infobox{{max-width:480px;margin:60px auto;text-align:center;
   background:linear-gradient(180deg, rgba(20,62,82,.5), rgba(8,33,47,.6));
   border:1px solid var(--line);border-radius:20px;padding:34px 28px}}
 .infobox h2{{font-family:"Bricolage Grotesque",sans-serif;font-weight:800;margin:0 0 10px}}
 .infobox p{{color:var(--mist);font-size:15px;line-height:1.55;margin:0}}
 .foot{{text-align:center;color:var(--mist);opacity:.7;font-size:11px;margin-top:34px}}
</style></head><body>
 <div class="top">
   <div class="brand"><img src="/static/logo.png" alt="Du Voyageur">
     <div><b>Du Voyageur</b><span>Espace client</span></div></div>
   {logout}
 </div>
 <div class="wrap">{body}
   <div class="foot">Du Voyageur · Permis d'agence 700495</div>
 </div>
</body></html>"""
